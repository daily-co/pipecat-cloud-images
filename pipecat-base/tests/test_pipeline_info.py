"""Each pipeline reports the transports and services it runs.

A ``pipeline_info`` record lists a worker's transports and services, so the ones
in use can be counted across sessions. Pinned here:

* Each is named by its Pipecat class, with the model a service was configured
  with and the serializer a transport frames its media with.
* Processors nested in sub-pipelines are found too.
* A developer's own class is reported as the Pipecat class it is built on, and
  marked custom; its name, and the name of any processor of theirs, never
  leaves the pod.
* The model is read where Pipecat keeps it, from 0.0.104 (the first release
  that loads the observers) on; a long one is cut short.
* A record stays inside the publisher's limit, however large the pipeline:
  components past it are dropped, and the record says so.
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
from pipecat.services.ai_service import AIService
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
    """Where a service holds its model, from Pipecat 0.0.104 on."""
    return {"_settings": SimpleNamespace(model=model)}


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


def test_a_service_without_a_model_leaves_it_out(published):
    (record,) = _published_pipeline(published, _Pipeline(_deepgram(model=None)))

    assert record["event_properties"]["components"] == [
        {"kind": "stt", "class": "DeepgramSTTService"}
    ]


def test_a_pipeline_without_transports_or_services_still_reports(published):
    (record,) = _published_pipeline(published, _Pipeline())

    assert record["event_properties"] == {"components": []}


# What the publisher allows a record's properties, in the publisher's own terms.
MAX_EVENT_PROPERTIES_LENGTH = 10240


def test_a_long_model_is_cut_short(published):
    (record,) = _published_pipeline(published, _Pipeline(_openai(model="m" * 175)))

    (component,) = record["event_properties"]["components"]
    assert component["model"] == "m" * pcc_observers._MAX_MODEL_CHARS + "…"


def test_a_pipeline_too_large_for_the_publisher_still_reports_what_fits(published):
    pipeline = _Pipeline(*(_openai(model="m" * 175) for _ in range(200)))

    (record,) = _published_pipeline(published, pipeline)

    properties = record["event_properties"]
    assert len(json.dumps(properties)) <= MAX_EVENT_PROPERTIES_LENGTH
    assert properties["truncated"] is True
    assert 0 < len(properties["components"]) < 200


def test_a_pipeline_that_fits_is_not_marked_truncated(published):
    pipeline = _Pipeline(*(_openai() for _ in range(20)))

    (record,) = _published_pipeline(published, pipeline)

    assert "truncated" not in record["event_properties"]
    assert len(record["event_properties"]["components"]) == 20


def test_another_ai_service_is_reported_as_a_service(published):
    vision = _processor(
        AIService, "MoondreamService", "pipecat.services.moondream.vision", **_model("moondream2")
    )

    (record,) = _published_pipeline(published, _Pipeline(vision))

    assert record["event_properties"]["components"] == [
        {"kind": "service", "class": "MoondreamService", "model": "moondream2"}
    ]


def test_a_serializer_pipecat_did_not_define_is_left_out(published):
    transport = _processor(
        BaseInputTransport,
        "FastAPIWebsocketInputTransport",
        "pipecat.transports.websocket.fastapi",
        _params=SimpleNamespace(serializer=_serializer("AcmeWireFormat", "bot")),
    )

    (record,) = _published_pipeline(published, _Pipeline(transport))

    assert record["event_properties"]["components"] == [
        {"kind": "transport", "class": "FastAPIWebsocketInputTransport"}
    ]


def _has_service_settings():
    try:
        from pipecat.services.settings import LLMSettings  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _has_service_settings(),
    reason="this pipecat-ai has no service settings, and loads no observers",
)
def test_a_real_service_reports_the_model_it_was_built_with(published):
    """Through Pipecat's own constructor, so a move of the model fails here."""
    from pipecat.services.settings import LLMSettings

    cls = type(
        "OpenAILLMService",
        (LLMService,),
        {"__module__": "pipecat.services.openai.llm", "__abstractmethods__": frozenset()},
    )
    service = cls(settings=LLMSettings(model="gpt-4.1"))

    (record,) = _published_pipeline(published, _Pipeline(service))

    assert record["event_properties"]["components"] == [
        {"kind": "llm", "class": "OpenAILLMService", "model": "gpt-4.1"}
    ]


def test_a_pipeline_that_cannot_be_described_publishes_nothing(published):
    class _Broken:
        @property
        def processors(self):
            raise RuntimeError("no processors here")

    assert _published_pipeline(published, _Broken()) == []
