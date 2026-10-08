"""Publishing records to the Pipecat Cloud event pipeline.

Each record is posted to ``PIPECAT_EVENT_PUBLISHER_ENDPOINT`` when it is set,
and is otherwise dropped.

Records come from two places: app.py, about the session it is starting, and
pcc_observers, about what the session's pipeline reports. Pipecat loads
pcc_observers from its path rather than importing it, so the publisher lives
here, in a module both import, where they share one connection and one count
of failures.
"""

import uuid
from datetime import datetime, timezone
from os import environ

# aiohttp is not declared by pipecat-base; it arrives with pipecatcloud, which
# is.
import aiohttp
import pcc_structured_logs
from loguru import logger

# The full URL records are posted to, route included: the publisher serves
# POST /events and 404s anything else.
_events_endpoint = environ.get("PIPECAT_EVENT_PUBLISHER_ENDPOINT")

_http_session: aiohttp.ClientSession | None = None


# An unreachable publisher makes every record wait out the timeout, which is
# what this caps. Counting per session means one that comes back is picked up
# without restarting the pod.
_MAX_CONSECUTIVE_FAILURES = 10

_consecutive_failures = 0
_counting_for_session: str | None = None


def _get_http_session() -> aiohttp.ClientSession:
    """The session records are posted through, opened on first use.

    It lives as long as the process, which keeps the connection to the
    publisher open across a pod's sessions. Nothing closes it: the interpreter
    is on its way out by the time it could, and aiohttp's parting complaint
    about an unclosed session is the price of not carrying a shutdown hook for
    a socket the kernel is about to reclaim anyway.
    """
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
        )
    return _http_session


def _record_publish_result(succeeded: bool):
    """Count failures in a row, and say once when a session gives up."""
    global _consecutive_failures
    if succeeded:
        _consecutive_failures = 0
        return
    _consecutive_failures += 1
    if _consecutive_failures == _MAX_CONSECUTIVE_FAILURES:
        logger.warning(
            f"[pcc-observability] {_MAX_CONSECUTIVE_FAILURES} publishes failed in a row;"
            " not publishing again this session"
        )


async def publish_event(event_name: str, event_properties: dict | None = None):
    """Publish an event to the event publisher endpoint if configured.

    Every record belongs to a session, so a record with no session to name is
    one nothing can be joined to. It goes no further.
    """
    if not _events_endpoint:
        return

    session_id = pcc_structured_logs.current_session()
    if not session_id:
        return

    global _consecutive_failures, _counting_for_session
    if session_id != _counting_for_session:
        _counting_for_session = session_id
        _consecutive_failures = 0
    if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
        return

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "session_id": session_id,
        "event_name": event_name,
        "event_uuid": str(uuid.uuid4()),
    }
    if event_properties:
        payload["event_properties"] = event_properties

    try:
        session = _get_http_session()
        async with session.post(_events_endpoint, json=payload) as resp:
            if resp.status >= 400:
                logger.warning(f"[pcc-observability] Event publish failed: {resp.status}")
            _record_publish_result(resp.status < 400)
    except Exception as e:
        logger.warning(f"[pcc-observability] Event publish error: {e}")
        _record_publish_result(False)
