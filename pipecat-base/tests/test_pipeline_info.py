"""Each pipeline reports the transports and services it runs.

A ``pipeline_info`` record lists a worker's transports and services, so the ones
in use can be counted across sessions. Pinned here:

* Each is named by its Pipecat class, with the model a service was configured
  with and the serializer a transport frames its media with.
* Processors nested in sub-pipelines are found too.
* A developer's own class is reported as the Pipecat class it is built on, and
  marked custom; its name, and the name of any processor of theirs, never
  leaves the pod.
* The model comes from ``settings.model`` (Pipecat 1.x) or ``model_name``
  (earlier releases).
* The component list is capped, so a record stays inside the publisher's limit.
* A pipeline that cannot be described publishes nothing, and does not stop the
  session from starting.
"""

import asyncio
import json
from types import SimpleNamespace

import pcc_events
import pcc_observers
import pcc_structured_logs
import pytest
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transports.base_input import BaseInputTransport


class _Response:
    status = 202

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Stands in for the aiohttp session, recording each record it's handed."""

    closed = False

    def __init__(self):
        self.records = []

    def post(self, url, json=None):
        self.records.append(json)
        return _Response()


@pytest.fixture
def published(monkeypatch):
    session = _Session()
    monkeypatch.setattr(pcc_events, "_get_http_session", lambda: session)
    monkeypatch.setattr(pcc_events, "_events_endpoint", "http://publisher:3000/events")
    monkeypatch.setattr(pcc_events, "_consecutive_failures", {})
    monkeypatch.setattr(pcc_structured_logs, "_current_session_id", "session-abc")
    return session.records


class _Pipeline:
    """A pipeline, as the walker sees one: something holding processors."""

    def __init__(self, *processors):
        self.processors = list(processors)


def _processor(base, name, module, **attrs):
    """An instance of a class named ``name``, defined in ``module``, built on ``base``.

    Built without its constructor, which for a real service would want an API
    key and its provider's SDK. ``processors`` is pinned to empty because older
    Pipecat releases do not define it on a leaf processor.
    """
    cls = type(name, (base,), {"__module__": module, "processors": []})
    return _instance(cls, **attrs)


def _instance(cls, **attrs):
    # The service base classes are abstract; what a service would implement
    # is never called here.
    cls.__abstractmethods__ = frozenset()
    processor = cls.__new__(cls)
    processor.__dict__.update(attrs)
    return processor


def _model(model):
    """What a service holds its model in, for either generation of Pipecat."""
    return {"_settings": SimpleNamespace(model=model), "_model_name": model}


def _serializer(name, module):
    return type(name, (), {"__module__": module})()


def _daily():
    return _processor(
        BaseInputTransport, "DailyInputTransport", "pipecat.transports.daily.transport"
    )


def _deepgram(model="nova-3"):
    return _processor(
        STTService, "DeepgramSTTService", "pipecat.services.deepgram.stt", **_model(model)
    )


def _openai(model="gpt-4.1"):
    return _processor(
        LLMService, "OpenAILLMService", "pipecat.services.openai.llm", **_model(model)
    )


_CARTESIA = type(
    "CartesiaTTSService",
    (TTSService,),
    {"__module__": "pipecat.services.cartesia.tts", "processors": []},
)


def _published_pipeline(published, pipeline):
    async def run():
        pcc_observers._publish_pipeline_info(SimpleNamespace(pipeline=pipeline))
        await asyncio.gather(*pcc_observers._publishing)

    asyncio.run(run())
    records = [r for r in published if r["event_name"] == "pipeline_info"]
    return records


def test_a_pipeline_names_its_transport_and_services(published):
    pipeline = _Pipeline(
        _daily(),
        _deepgram(),
        # A sub-pipeline's processors are found too.
        _Pipeline(_openai(), _instance(_CARTESIA, **_model("sonic-2"))),
    )

    (record,) = _published_pipeline(published, pipeline)

    assert record["session_id"] == "session-abc"
    assert record["event_properties"] == {
        "components": [
            {"kind": "transport", "class": "DailyInputTransport"},
            {"kind": "stt", "class": "DeepgramSTTService", "model": "nova-3"},
            {"kind": "llm", "class": "OpenAILLMService", "model": "gpt-4.1"},
            {"kind": "tts", "class": "CartesiaTTSService", "model": "sonic-2"},
        ]
    }


def test_a_websocket_transport_names_its_serializer(published):
    transport = _processor(
        BaseInputTransport,
        "FastAPIWebsocketInputTransport",
        "pipecat.transports.websocket.fastapi",
        _params=SimpleNamespace(
            serializer=_serializer("TwilioFrameSerializer", "pipecat.serializers.twilio")
        ),
    )

    (record,) = _published_pipeline(published, _Pipeline(transport))

    assert record["event_properties"]["components"] == [
        {
            "kind": "transport",
            "class": "FastAPIWebsocketInputTransport",
            "serializer": "TwilioFrameSerializer",
        }
    ]


def test_a_developers_own_classes_are_never_named(published):
    their_tts = type("AcmeVoice", (_CARTESIA,), {"__module__": "bot"})
    their_processor = _processor(FrameProcessor, "AcmeRedactor", "bot")
    their_serializer = type(
        "AcmeSerializer",
        (type(_serializer("TwilioFrameSerializer", "pipecat.serializers.twilio")),),
        {"__module__": "bot"},
    )()
    transport = _processor(
        BaseInputTransport,
        "FastAPIWebsocketInputTransport",
        "pipecat.transports.websocket.fastapi",
        _params=SimpleNamespace(serializer=their_serializer),
    )
    pipeline = _Pipeline(transport, their_processor, _instance(their_tts, **_model("sonic-2")))

    (record,) = _published_pipeline(published, pipeline)

    assert record["event_properties"]["components"] == [
        {
            "kind": "transport",
            "class": "FastAPIWebsocketInputTransport",
            "serializer": "TwilioFrameSerializer",
        },
        {"kind": "tts", "class": "CartesiaTTSService", "model": "sonic-2", "custom": True},
    ]
    assert "Acme" not in json.dumps(record)


def test_an_older_pipecat_reports_the_model_it_named_model_name(published):
    # Before 1.x a service had no settings, only model_name.
    older = type(
        "DeepgramSTTService",
        (STTService,),
        {"__module__": "pipecat.services.deepgram.stt", "processors": [], "model_name": "nova-2"},
    )

    (record,) = _published_pipeline(published, _Pipeline(_instance(older)))

    assert record["event_properties"]["components"] == [
        {"kind": "stt", "class": "DeepgramSTTService", "model": "nova-2"}
    ]


def test_a_service_without_a_model_leaves_it_out(published):
    (record,) = _published_pipeline(published, _Pipeline(_deepgram(model=None)))

    assert record["event_properties"]["components"] == [
        {"kind": "stt", "class": "DeepgramSTTService"}
    ]


def test_a_pipeline_without_transports_or_services_still_reports(published):
    (record,) = _published_pipeline(published, _Pipeline())

    assert record["event_properties"] == {"components": []}


def test_the_components_listed_are_capped(published):
    pipeline = _Pipeline(*(_openai() for _ in range(pcc_observers._MAX_COMPONENTS + 10)))

    (record,) = _published_pipeline(published, pipeline)

    assert len(record["event_properties"]["components"]) == pcc_observers._MAX_COMPONENTS


def test_a_pipeline_that_cannot_be_described_publishes_nothing(published):
    class _Broken:
        @property
        def processors(self):
            raise RuntimeError("no processors here")

    assert _published_pipeline(published, _Broken()) == []
