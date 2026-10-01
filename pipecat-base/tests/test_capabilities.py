"""The capability document the image serves at ``/pcc/capabilities``.

The platform reads it once per deployment and rejects a document that breaks
its limits, so the properties pinned here are:

* **The entries follow what the image found at startup** and the modules
  installed, with the reason of the first thing a capability lacks.
* **What is served always passes the limits**: every reason is cleaned, and a
  document that would break them is never served.
* **The path belongs to the image**: a route or middleware the bot module adds
  to the shared app never answers it.
"""

import json
import os
import subprocess
import sys
import textwrap
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pcc_capabilities
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from feature_manager import FeatureInfo, FeatureKeys, FeatureManager, FeatureStatus

# app.py does `from bot import bot` at import time; the real module is supplied
# by the customer's image.
if "bot" not in sys.modules:
    _stub = types.ModuleType("bot")

    async def _noop_bot(args):
        return None

    _stub.bot = _noop_bot
    sys.modules["bot"] = _stub

import app  # noqa: E402

NAMES = {"transport.daily", "transport.websocket", "transport.webrtc", "whatsapp"}


def _features(overrides=None, missing=()):
    """A FeatureManager holding exactly these states, whatever is installed:
    every feature enabled but ``overrides`` (key: (status, message)) and
    ``missing``."""
    features = FeatureManager.__new__(FeatureManager)
    features._unavailable = {}
    # feature_manager gives each feature a display name ("WhatsApp
    # Integration") that is not its key; so does this, so a reason that
    # names one or the other shows which.
    features.features = {
        key: FeatureInfo(name=f"Display {key.value}", status=FeatureStatus.ENABLED)
        for key in FeatureKeys
        if key not in missing
    }
    for key, (status, message) in (overrides or {}).items():
        features.features[key] = FeatureInfo(
            name=f"Display {key.value}", status=status, error_message=message
        )
    return features


def _document(capabilities=None, **top):
    document = {
        "version": 1,
        "capabilities": capabilities
        if capabilities is not None
        else {"transport.daily": {"available": True}},
    }
    document.update(top)
    return document


class TestEntries:
    @pytest.fixture(autouse=True)
    def _everything_installed(self, monkeypatch):
        # Whatever this environment installed: each test says what is missing.
        monkeypatch.setattr(pcc_capabilities, "_installed", lambda module: True)

    def test_daily_needs_daily_python(self, monkeypatch):
        monkeypatch.setattr(pcc_capabilities, "_installed", lambda module: module != "daily")
        found = pcc_capabilities.entries(_features())
        assert found["transport.daily"] == {
            "available": False,
            "reason": "No module named 'daily'",
        }
        assert all(found[name]["available"] for name in NAMES - {"transport.daily"})

    def test_only_modules_the_image_does_not_import_are_looked_for(self, monkeypatch):
        asked = []
        monkeypatch.setattr(
            pcc_capabilities, "_installed", lambda module: asked.append(module) or True
        )
        pcc_capabilities.entries(_features())
        assert asked == ["daily"]

    def test_a_route_that_is_not_set_up_comes_before_a_missing_module(self, monkeypatch):
        monkeypatch.setattr(pcc_capabilities, "_installed", lambda module: False)
        features = _features({FeatureKeys.DAILY_TRANSPORT: (FeatureStatus.DISABLED, "no route")})
        assert pcc_capabilities.entries(features)["transport.daily"]["reason"] == "no route"

    def test_everything_found_is_available(self):
        assert pcc_capabilities.entries(_features()) == {
            name: {"available": True} for name in NAMES
        }

    def test_webrtc_needs_the_session_arguments(self):
        features = _features(
            {FeatureKeys.SMALL_WEBRTC_SESSION: (FeatureStatus.DISABLED, "no type")}
        )
        assert pcc_capabilities.entries(features)["transport.webrtc"] == {
            "available": False,
            "reason": "no type",
        }

    def test_webrtc_needs_the_transport(self):
        features = _features(
            {FeatureKeys.SMALLWEBRTC_TRANSPORT: (FeatureStatus.ERROR, "No module named 'aiortc'")}
        )
        assert pcc_capabilities.entries(features)["transport.webrtc"] == {
            "available": False,
            "reason": "No module named 'aiortc'",
        }

    def test_the_first_missing_feature_gives_the_reason(self):
        features = _features(
            {
                FeatureKeys.SMALL_WEBRTC_SESSION: (FeatureStatus.DISABLED, "first"),
                FeatureKeys.SMALLWEBRTC_TRANSPORT: (FeatureStatus.DISABLED, "second"),
            }
        )
        assert pcc_capabilities.entries(features)["transport.webrtc"]["reason"] == "first"

    def test_whatsapp_missing_its_configuration_is_unavailable(self):
        reason = "Missing environment variables: WHATSAPP_TOKEN"
        features = _features({FeatureKeys.WHATSAPP: (FeatureStatus.MISSING_CONFIG, reason)})
        assert pcc_capabilities.entries(features)["whatsapp"] == {
            "available": False,
            "reason": reason,
        }

    def test_a_reason_without_a_message_names_the_state(self):
        features = _features({FeatureKeys.WHATSAPP: (FeatureStatus.DISABLED, "")})
        assert pcc_capabilities.entries(features)["whatsapp"]["reason"] == "whatsapp is disabled"

    def test_a_message_that_cleans_to_nothing_names_the_state(self):
        features = _features({FeatureKeys.WHATSAPP: (FeatureStatus.ERROR, " \n\t ")})
        assert pcc_capabilities.entries(features)["whatsapp"]["reason"] == "whatsapp is error"

    def test_a_feature_never_detected_is_unavailable(self):
        features = _features(missing=(FeatureKeys.WHATSAPP,))
        assert pcc_capabilities.entries(features)["whatsapp"] == {
            "available": False,
            "reason": "whatsapp was not detected",
        }

    def test_reasons_are_cleaned(self):
        features = _features(
            {FeatureKeys.WHATSAPP: (FeatureStatus.ERROR, "bad [thing]\n\tin é\x07")}
        )
        assert pcc_capabilities.entries(features)["whatsapp"]["reason"] == ("bad (thing) in \\xe9?")

    def test_smallwebrtc_types_that_do_not_build_turn_webrtc_off(self):
        # app.py hands the feature manager the reason SmallWebRTC's session
        # arguments did not build.
        features = FeatureManager(unavailable={FeatureKeys.SMALL_WEBRTC_SESSION: "cannot build it"})
        entries = pcc_capabilities.entries(features)
        assert entries["transport.webrtc"] == {"available": False, "reason": "cannot build it"}
        assert entries["transport.daily"] == {"available": True}

    def test_what_this_environment_finds_renders(self):
        body = pcc_capabilities.render(pcc_capabilities.entries(FeatureManager()))
        assert set(json.loads(body)["capabilities"]) == NAMES


class TestInstalled:
    @pytest.mark.parametrize(
        "module, installed",
        [
            ("json", True),
            ("pcc_capabilities_surely_not_installed", False),
            ("pcc_capabilities_surely_not_installed.child", False),
            ("", False),
        ],
    )
    def test_found_without_importing(self, module, installed):
        assert pcc_capabilities._installed(module) is installed


class TestText:
    @pytest.mark.parametrize(
        "raw, cleaned",
        [
            ("one\ntwo\r\n\tthree", "one two three"),
            ("a [b] c", "a (b) c"),
            ("bell\x07 and nul\x00", "bell? and nul?"),
            ("café", "caf\\xe9"),
            ("lone \ud800 surrogate", "lone \\ud800 surrogate"),
            ("  padded  ", "padded"),
        ],
    )
    def test_cleaned(self, raw, cleaned):
        assert pcc_capabilities.text(raw) == cleaned

    def test_the_limit_itself_is_kept_whole(self):
        assert pcc_capabilities.text("x" * 256) == "x" * 256

    def test_cut_to_the_limit(self):
        line = pcc_capabilities.text("x" * 1000)
        assert len(line) == pcc_capabilities.MAX_TEXT_CHARS
        assert line.endswith("...")

    @pytest.mark.parametrize(
        "raw", ["[/bold]", "\u2028line\u2029sep", "\x1b[31mred", "é" * 300, "\ud800" * 100]
    )
    def test_always_passes_the_limits(self, raw):
        entry = {"available": False, "reason": pcc_capabilities.text(raw)}
        assert pcc_capabilities.problem(_document({"x": entry})) is None


class TestProblem:
    def test_a_valid_document(self):
        document = _document(
            {
                "transport.daily": {"available": True},
                "transport.webrtc": {"available": False, "reason": "why"},
                "python": {"available": True, "value": "3.12"},
            }
        )
        assert pcc_capabilities.problem(document) is None

    def test_the_limits_themselves_are_allowed(self):
        document = _document(
            {
                f"{'n' * 61}{i:02d}": {"available": False, "reason": "r" * 256}
                for i in range(pcc_capabilities.MAX_ENTRIES)
            }
        )
        assert pcc_capabilities.problem(document) is None

    @pytest.mark.parametrize(
        "document",
        [
            [],
            _document(version=2),
            _document(version=True),
            _document(version="1"),
            {"version": 1},
            _document(capabilities=[]),
            _document(extra=1),
            _document({f"c{i}": {"available": True} for i in range(65)}),
            _document({"Transport.daily": {"available": True}}),
            _document({".daily": {"available": True}}),
            _document({"transport..daily": {"available": True}}),
            _document({"transport.daily.": {"available": True}}),
            _document({"transport daily": {"available": True}}),
            _document({"n" * 64: {"available": True}}),
            # Python's $ matches before a final newline; the platform's does not.
            _document({"transport.daily\n": {"available": True}}),
            _document({"x": True}),
            _document({"x": {}}),
            _document({"x": {"available": 1}}),
            _document({"x": {"available": "true"}}),
            _document({"x": {"available": True, "extra": 1}}),
            _document({"x": {"available": False, "reason": 5}}),
            _document({"x": {"available": False, "reason": "r" * 257}}),
            _document({"x": {"available": False, "reason": "two\nlines"}}),
            _document({"x": {"available": False, "reason": "[markup]"}}),
            _document({"x": {"available": False, "reason": "a[b"}}),
            _document({"x": {"available": False, "reason": "a]b"}}),
            # Printable, but not ASCII: what "printable" means varies by
            # Unicode version, so this image serves ASCII only.
            _document({"x": {"available": False, "reason": "café"}}),
            _document({"x": {"available": True, "value": "Ᲊ"}}),
            _document({"x": {"available": False, "reason": "bell\x07"}}),
            _document({"x": {"available": True, "value": "tab\there"}}),
        ],
    )
    def test_rejected(self, document):
        assert pcc_capabilities.problem(document) is not None


class TestRender:
    def test_compact_and_stable(self):
        body = pcc_capabilities.render({"b": {"available": True}, "a": {"available": False}})
        assert body == (
            b'{"capabilities":{"a":{"available":false},"b":{"available":true}},"version":1}'
        )

    def test_refuses_a_document_the_platform_would_reject(self):
        with pytest.raises(ValueError, match="available"):
            pcc_capabilities.render({"x": {"available": "yes"}})

    def test_refuses_a_body_over_the_limit(self):
        # Each entry within its limits, the whole over 16 KiB.
        capabilities = {
            f"{'n' * 61}{i:02d}": {"available": False, "reason": "r" * 256, "value": "v" * 256}
            for i in range(pcc_capabilities.MAX_ENTRIES)
        }
        with pytest.raises(ValueError, match="bytes"):
            pcc_capabilities.render(capabilities)


class TestServe:
    BODY = pcc_capabilities.render({"transport.daily": {"available": True}})

    def _client(self, inner=None):
        inner = inner or FastAPI()
        return TestClient(pcc_capabilities.serve(inner, self.BODY))

    def test_get(self):
        response = self._client().get(pcc_capabilities.PATH)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
        assert response.content == self.BODY

    def test_head(self):
        response = self._client().head(pcc_capabilities.PATH)
        assert response.status_code == 200
        assert response.headers["content-length"] == str(len(self.BODY))
        assert response.content == b""

    def test_other_methods(self):
        response = self._client().post(pcc_capabilities.PATH)
        assert response.status_code == 405
        assert response.headers["allow"] == "GET, HEAD"

    def test_other_paths_reach_the_app(self):
        inner = FastAPI()

        @inner.get("/other")
        async def other():
            return {"from": "app"}

        client = self._client(inner)
        assert client.get("/other").json() == {"from": "app"}
        # Only the exact path is the image's.
        assert client.get(pcc_capabilities.PATH + "/").status_code == 404

    def test_a_route_on_the_app_does_not_take_the_path_over(self):
        inner = FastAPI()

        @inner.get(pcc_capabilities.PATH)
        async def customer():
            return PlainTextResponse("customer")

        assert self._client(inner).get(pcc_capabilities.PATH).content == self.BODY

    def test_middleware_on_the_app_does_not_take_the_path_over(self):
        inner = FastAPI()

        @inner.middleware("http")
        async def customer(request, call_next):
            return JSONResponse({"from": "middleware"})

        client = self._client(inner)
        assert client.get(pcc_capabilities.PATH).content == self.BODY
        assert client.get("/anything").json() == {"from": "middleware"}

    def test_websockets_and_lifespan_reach_the_app(self):
        started = []

        @asynccontextmanager
        async def lifespan(_app):
            started.append(True)
            yield

        inner = FastAPI(lifespan=lifespan)

        @inner.websocket("/ws")
        async def ws(socket: WebSocket):
            await socket.accept()
            await socket.send_text("hi")
            await socket.close()

        with self._client(inner) as client:
            with client.websocket_connect("/ws") as socket:
                assert socket.receive_text() == "hi"
        assert started == [True]

    def test_a_websocket_on_the_path_reaches_the_app(self):
        inner = FastAPI()

        @inner.websocket(pcc_capabilities.PATH)
        async def ws(socket: WebSocket):
            await socket.accept()
            await socket.send_text("app")
            await socket.close()

        with self._client(inner).websocket_connect(pcc_capabilities.PATH) as socket:
            assert socket.receive_text() == "app"

    def test_nothing_to_report_still_keeps_the_path(self):
        inner = FastAPI()

        @inner.get(pcc_capabilities.PATH)
        async def customer():
            return PlainTextResponse("customer")

        @inner.get("/other")
        async def other():
            return {"from": "app"}

        client = TestClient(pcc_capabilities.serve(inner, None))
        for method in ("GET", "HEAD", "POST"):
            response = client.request(method, pcc_capabilities.PATH)
            assert response.status_code == 404
            assert response.content == b""
        assert client.get("/other").json() == {"from": "app"}


class TestApp:
    def test_the_server_serves_the_document_in_front_of_the_app(self):
        client = TestClient(app.server_config.app)
        response = client.get(pcc_capabilities.PATH)
        assert response.status_code == 200
        assert set(response.json()["capabilities"]) == NAMES
        assert client.get("/livez").status_code == 200

    def test_the_document_is_what_the_features_found(self):
        assert app._capabilities == pcc_capabilities.render(
            pcc_capabilities.entries(app.feature_manager)
        )

    def test_the_image_copies_the_module(self):
        dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
        copy = next(line for line in dockerfile.splitlines() if line.startswith("COPY ./app.py"))
        assert "./pcc_capabilities.py" in copy

    def test_a_fault_building_the_document_serves_without_it(self, tmp_path):
        # A bot module that breaks the entries, serves its own document at the
        # path, and, as a bot module may, removes loguru's handlers at import:
        # the image still starts, answers the path with 404 rather than the
        # bot's document, and says why once logging is set up.
        bot = tmp_path / "bot"
        bot.mkdir()
        (bot / "bot.py").write_text(
            textwrap.dedent(
                """
                import pcc_capabilities
                # pipecatcloud's import replaces loguru's handlers, so once it has
                # run, nothing adds one back until app.py sets logging up.
                import pipecatcloud
                from loguru import logger
                from pipecatcloud_system import app


                def _broken(features):
                    raise RuntimeError("entries broke")


                pcc_capabilities.entries = _broken
                logger.remove()


                @app.get(pcc_capabilities.PATH)
                async def forged():
                    return {"forged": True}


                async def bot(args):
                    pass
                """
            )
        )
        check = textwrap.dedent(
            """
            import app
            from fastapi.testclient import TestClient
            import pcc_capabilities

            response = TestClient(app.server_config.app).get(pcc_capabilities.PATH)
            assert response.status_code == 404, (response.status_code, response.text)
            assert b"forged" not in response.content
            print("SERVING WITHOUT")
            """
        )
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PCC_LOG_DIR")}
        env["PYTHONPATH"] = os.pathsep.join([str(Path(app.__file__).parent), str(bot)])
        proc = subprocess.run(
            [sys.executable, "-c", check],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert "SERVING WITHOUT" in proc.stdout
        assert "Not reporting capabilities: entries broke" in proc.stderr
