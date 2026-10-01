"""Which kinds of session this image rules out, reported to the platform.

The platform knows an agent's base image version from its image label, but not
the pipecat-ai, pipecatcloud or extras the agent image installed on top of it.
At startup the image works out which session types it can route to the bot
(feature_manager, pcc_pipecat_compat) and whether the modules each transport
needs are installed (checked here). This module reports that at
``GET /pcc/capabilities``, where the platform reads it once per deployment.

The document can only rule sessions out. ``available: false`` means a session
of that kind cannot work on this image, so the platform can refuse it before it
starts. ``available: true`` means only that it is not ruled out: the image
cannot see which sessions the bot's own ``bot()`` handles.

The document::

    {"version": 1,
     "capabilities": {
       "transport.daily": {"available": true},
       "transport.webrtc": {"available": false, "reason": "..."}}}

- ``available`` is required; ``reason`` (why not) and ``value`` (for a fact
  that is not yes or no) are optional strings.
- Names are lowercase letters and digits, in parts joined by ``.``, ``_`` or
  ``-``, up to 63 characters.
- Up to 64 entries; ``reason`` and ``value`` up to 256 characters each, on one
  line, printable, with no square brackets (the platform's CLI reads those as
  markup); the whole body up to 16 KiB.

The format and these limits are the platform's, and its check is the one that
decides: it rejects a document that breaks them, which then counts for nothing.
They are copied here so that this image checks its own document before serving
it, and cleans every reason it quotes from an error; changing one is a change
on both sides. Its own check is the stricter one: reasons and values in
printable ASCII, which any reading of "printable" accepts, whatever Unicode
version the reader's is.

The document is built once, at startup, and served from memory. It is served
in front of the application, not by it: the bot module shares app.py's FastAPI
app and is imported before app.py adds its routes, so a route or middleware it
registered on this path would otherwise be reached first. Only HTTP requests
are answered there; a websocket on the path still reaches the application.
"""

import importlib.util
import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from feature_manager import FeatureKeys, FeatureManager, FeatureStatus

# The startup refusal's cleanup: one line, no square brackets.
from pcc_pipecat_compat import _one_line

PATH = "/pcc/capabilities"

# The document format. A new one needs a platform release that reads it.
VERSION = 1

MAX_ENTRIES = 64
MAX_NAME_CHARS = 63
MAX_TEXT_CHARS = 256
MAX_BODY_BYTES = 16 * 1024

_NAME = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")
_ENTRY_FIELDS = {"available", "reason", "value"}

# Each capability, the features the image needs enabled to route it to the
# bot, and the modules the bot needs installed to run it. The first missing
# gives the reason.
#
# The modules are the ones the image does not import itself. Daily's route
# needs nothing beyond pipecat-ai, but pipecat's DailyTransport needs
# daily-python (pipecat-ai's `daily` extra). The WebSocket transport needs only
# FastAPI, which the image ships, and the feature manager already imports
# SmallWebRTC's modules, since the image's own /api/offer uses them.
_CAPABILITIES = (
    ("transport.daily", (FeatureKeys.DAILY_TRANSPORT,), ("daily",)),
    ("transport.websocket", (FeatureKeys.WEBSOCKET_TRANSPORT,), ()),
    (
        "transport.webrtc",
        (FeatureKeys.SMALL_WEBRTC_SESSION, FeatureKeys.SMALLWEBRTC_TRANSPORT),
        (),
    ),
    ("whatsapp", (FeatureKeys.WHATSAPP,), ()),
)

Scope = Dict[str, Any]
Receive = Callable[[], Awaitable[Dict[str, Any]]]
Send = Callable[[Dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def text(value: str) -> str:
    """``value`` as a reason or value the platform accepts.

    One line with no square brackets (the startup refusal's cleanup), in
    printable ASCII: other characters are escaped. Cut to MAX_TEXT_CHARS.
    """
    line = _one_line(str(value)).encode("ascii", errors="backslashreplace").decode("ascii")
    line = "".join(c if _printable_ascii(c) else "?" for c in line)
    if len(line) > MAX_TEXT_CHARS:
        line = line[: MAX_TEXT_CHARS - 3] + "..."
    return line


def _printable_ascii(c: str) -> bool:
    return " " <= c <= "~"


def entries(features: FeatureManager) -> Dict[str, Dict[str, Any]]:
    """The capability entries for what ``features`` found at startup and the
    modules installed."""
    result: Dict[str, Dict[str, Any]] = {}
    for name, keys, modules in _CAPABILITIES:
        reason = _first_unavailable(features, keys) or _first_missing(modules)
        if reason is None:
            result[name] = {"available": True}
        else:
            result[name] = {"available": False, "reason": reason}
    return result


def _first_missing(modules) -> Optional[str]:
    """The reason the first of ``modules`` that is not installed gives, worded
    as the import would, or None when all are."""
    for module in modules:
        if not _installed(module):
            return text(f"No module named '{module}'")
    return None


def _installed(module: str) -> bool:
    """Whether ``module`` can be imported, found without importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _first_unavailable(features: FeatureManager, keys) -> Optional[str]:
    """The cleaned reason the first of ``keys`` that is not enabled gives, or
    None when all are. A message that cleans to nothing names the state."""
    for key in keys:
        info = features.features.get(key)
        if info is None:
            return text(f"{key.value} was not detected")
        if info.status != FeatureStatus.ENABLED:
            return text(info.error_message) or text(f"{key.value} is {info.status.value}")
    return None


def problem(document: Any) -> Optional[str]:
    """Why the platform would reject ``document``, or None if it would accept it.

    Covers everything but the body size, which render() checks on the bytes.
    """
    if not isinstance(document, dict):
        return "the document is not an object"
    extra = set(document) - {"version", "capabilities"}
    if extra:
        return f"unknown field {sorted(extra)[0]!r}"
    version = document.get("version")
    if type(version) is not int or version != VERSION:
        return f"version must be {VERSION}"
    capabilities = document.get("capabilities")
    if not isinstance(capabilities, dict):
        return "capabilities must be an object"
    if len(capabilities) > MAX_ENTRIES:
        return f"more than {MAX_ENTRIES} capabilities"
    for name, entry in capabilities.items():
        # fullmatch: the pattern's $ would also match before a final newline.
        if not isinstance(name, str) or len(name) > MAX_NAME_CHARS or not _NAME.fullmatch(name):
            return (
                f"capability name {name!r} is not lowercase letters and digits joined by "
                f"'.', '_' or '-', up to {MAX_NAME_CHARS} characters"
            )
        if not isinstance(entry, dict):
            return f"{name}: the entry is not an object"
        extra = set(entry) - _ENTRY_FIELDS
        if extra:
            return f"{name}: unknown field {sorted(extra)[0]!r}"
        if type(entry.get("available")) is not bool:
            return f"{name}: available must be true or false"
        for field in ("reason", "value"):
            if field in entry and not _text_ok(entry[field]):
                return (
                    f"{name}: {field} must be one printable line of up to {MAX_TEXT_CHARS} "
                    "characters, without square brackets"
                )
    return None


def _text_ok(value: Any) -> bool:
    # Printable ASCII, what text() produces: stricter than the platform's check,
    # so a document this image serves passes it.
    return (
        isinstance(value, str)
        and len(value) <= MAX_TEXT_CHARS
        and all(_printable_ascii(c) for c in value)
        and "[" not in value
        and "]" not in value
    )


def render(capabilities: Dict[str, Dict[str, Any]]) -> bytes:
    """The document for ``capabilities``, as the bytes served.

    Raises ValueError if the platform would reject it, so a fault here stops
    the image serving a document rather than serving one that is rejected.
    """
    document = {"version": VERSION, "capabilities": capabilities}
    reason = problem(document)
    if reason is not None:
        raise ValueError(reason)
    body = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise ValueError(f"the document is {len(body)} bytes, over {MAX_BODY_BYTES}")
    return body


def serve(app: ASGIApp, body: Optional[bytes]) -> ASGIApp:
    """``app``, with HTTP requests for PATH answered in front of it.

    The path is the image's whether or not there is a document. With no
    ``body`` there is nothing to report, and the answer is 404, which the
    platform reads as it reads an older image's: an image that does not
    report. A route the bot module added there is never reached either way.
    """
    ok: List = []
    if body is not None:
        ok = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
    not_allowed: List = [(b"allow", b"GET, HEAD"), (b"content-length", b"0")]
    not_found: List = [(b"content-length", b"0")]

    async def with_capabilities(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != PATH:
            await app(scope, receive, send)
            return
        if body is None:
            status, headers, payload = 404, not_found, b""
        elif scope["method"] in ("GET", "HEAD"):
            status, headers = 200, ok
            payload = body if scope["method"] == "GET" else b""
        else:
            status, headers, payload = 405, not_allowed, b""
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    return with_capabilities
