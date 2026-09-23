#!/usr/bin/env python

"""Pipecat Cloud observability observers.

This file is loaded via the PIPECAT_SETUP_FILES mechanism in PipelineWorker.
It attaches the observers Pipecat Cloud reports on to every worker, so a
session leaves behind its startup timing, the latency of each turn and what
that latency was spent on, what each service cost and consumed, who was
speaking and when, the function calls the bot made, and the errors it hit.

Each record is published to the event pipeline when
``PIPECAT_EVENT_PUBLISHER_ENDPOINT`` is set, and is otherwise dropped.

Every observer is imported separately and skipped when the import fails, so a
bot pinned to an older Pipecat reports whatever its version has rather than
failing to start.
"""

import uuid
from datetime import datetime, timezone
from os import environ
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from loguru import logger
from shared_state import GLOBALS

_event_publisher_endpoint = environ.get("PIPECAT_EVENT_PUBLISHER_ENDPOINT")


def _events_url(endpoint: str | None) -> str | None:
    """Resolve the endpoint to the publisher's events route.

    The publisher serves POST /events and 404s anything else, so an endpoint
    configured as a bare host and port is completed here rather than dropping
    every record.
    """
    if not endpoint:
        return None
    parts = urlsplit(endpoint)
    if parts.path.strip("/"):
        return endpoint
    return urlunsplit((parts.scheme, parts.netloc, "/events", "", ""))


_events_endpoint = _events_url(_event_publisher_endpoint)
_http_session: aiohttp.ClientSession | None = None


def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
        )
    return _http_session


async def _publish_event(event_name: str, event_properties: dict | None = None):
    """Publish an event to the event publisher endpoint if configured."""
    if not _events_endpoint:
        return

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "session_id": GLOBALS.get("current_session_id", "NONE"),
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
    except Exception as e:
        logger.warning(f"[pcc-observability] Event publish error: {e}")


# The fields each record publishes. A bot installs its own Pipecat, so what a
# record contains is not ours to review; naming fields here keeps one added
# upstream from travelling further than the bot until someone adds it.
#
# Free text stays behind — a tool's arguments and result, the exception its
# handler raised, a processor's error message — since any of it can quote what
# was said; `push_error_frame` still logs the message for the bot's own
# session. Nested timings travel whole: processor names, keys and durations.
_PUBLISHED_FIELDS: dict[str, dict] = {
    "startup_timing": {
        "start_time": True,
        "total_duration_secs": True,
        "setup_phase_secs": True,
        "start_phase_secs": True,
        "processor_timings": True,
        "warmup": True,
    },
    "transport_timing": {
        "start_time": True,
        "bot_connected_secs": True,
        "client_connected_secs": True,
    },
    "latency_breakdown": {
        "measured_from": True,
        "total_secs": True,
        "user_turn_start_time": True,
        "user_turn_secs": True,
        "contributions": True,
        "ttfb": True,
        "text_aggregation": True,
        "function_calls": True,
    },
    "service_latency": {
        "kind": True,
        "processor": True,
        "model": True,
        "timestamp": True,
        "seconds": True,
        "ttfb_secs": True,
        "leading_silence_secs": True,
        "thinking_time_secs": True,
    },
    "service_usage": {
        "kind": True,
        "processor": True,
        "model": True,
        "timestamp": True,
        "audio_seconds": True,
        "characters": True,
        "prompt_tokens": True,
        "completion_tokens": True,
        "total_tokens": True,
        "cache_read_input_tokens": True,
        "cache_creation_input_tokens": True,
        "reasoning_tokens": True,
        "input_audio_tokens": True,
        "output_audio_tokens": True,
        "cache_read_input_audio_tokens": True,
    },
    "speech_event": {"kind": True, "timestamp": True, "started_at": True},
    "function_call_event": {
        "kind": True,
        "function_name": True,
        "tool_call_id": True,
        "group_id": True,
        "blocking": True,
        "timestamp": True,
        "started_at": True,
        "in_progress_at": True,
    },
    "error": {
        "category": True,
        "exception_type": True,
        "processor": True,
        "processor_usable": True,
        "timestamp": True,
    },
}


async def _publish_record(event_name: str, record):
    """Publish the fields `_PUBLISHED_FIELDS` names for this record.

    A record that will not serialize is dropped rather than raised into the
    handler that reported it.
    """
    try:
        properties = record.model_dump(
            mode="json", exclude_none=True, include=_PUBLISHED_FIELDS[event_name]
        )
    except Exception as e:
        logger.warning(f"[pcc-observability] Could not serialize {event_name}: {e}")
        return
    await _publish_event(event_name, properties)


async def setup_pipeline_worker(worker):
    """Called by PipelineWorker._load_setup_files() for each worker instance."""
    await _setup_startup_timing_observer(worker)
    await _setup_user_bot_latency_observer(worker)
    await _setup_service_metrics_observer(worker)
    await _setup_speaking_observer(worker)
    await _setup_function_call_observer(worker)
    await _setup_error_observer(worker)


# Backwards compatibility: Pipecat < 1.4.0 looks for ``setup_pipeline_task``.
# 1.4.0+ prefers ``setup_pipeline_worker`` (and checks it first), falling back
# to the old name only with a DeprecationWarning, so defining both keeps us
# warning-free across versions. Drop this alias once the minimum supported
# Pipecat is >= 1.4.0.
setup_pipeline_task = setup_pipeline_worker


async def _setup_startup_timing_observer(worker):
    try:
        from pipecat.observers.startup_timing_observer import StartupTimingObserver
    except ImportError:
        return

    observer = StartupTimingObserver()

    @observer.event_handler("on_startup_timing_report")
    async def on_startup_timing_report(observer, report):
        await _publish_record("startup_timing", report)

    @observer.event_handler("on_transport_timing_report")
    async def on_transport_timing_report(observer, report):
        await _publish_record("transport_timing", report)

    worker.add_observer(observer)


async def _setup_user_bot_latency_observer(worker):
    try:
        from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
    except ImportError:
        return

    observer = UserBotLatencyObserver()

    @observer.event_handler("on_latency_measured")
    async def on_latency_measured(observer, latency_seconds):
        await _publish_event("user_bot_latency", {"latency_secs": round(latency_seconds, 3)})

    @observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(observer, breakdown):
        await _publish_record("latency_breakdown", breakdown)

    @observer.event_handler("on_first_bot_speech_latency")
    async def on_first_bot_speech_latency(observer, latency_seconds):
        await _publish_event(
            "first_bot_speech_latency", {"latency_secs": round(latency_seconds, 3)}
        )

    worker.add_observer(observer)


async def _setup_service_metrics_observer(worker):
    try:
        from pipecat.observers.service_metrics_observer import ServiceMetricsObserver
    except ImportError:
        return

    observer = ServiceMetricsObserver()

    @observer.event_handler("on_service_latency")
    async def on_service_latency(observer, record):
        await _publish_record("service_latency", record)

    @observer.event_handler("on_service_usage")
    async def on_service_usage(observer, record):
        await _publish_record("service_usage", record)

    worker.add_observer(observer)


async def _setup_speaking_observer(worker):
    try:
        from pipecat.observers.speaking_observer import SpeakingObserver
    except ImportError:
        return

    observer = SpeakingObserver()

    @observer.event_handler("on_speech_event")
    async def on_speech_event(observer, event):
        await _publish_record("speech_event", event)

    worker.add_observer(observer)


async def _setup_function_call_observer(worker):
    try:
        from pipecat.observers.function_call_observer import FunctionCallObserver
    except ImportError:
        return

    # A call's arguments and result carry whatever the conversation was about,
    # so neither travels: what a bot's tools were asked and answered belongs to
    # the bot, not to the platform running it.
    observer = FunctionCallObserver(include_arguments=False, include_results=False)

    @observer.event_handler("on_function_call_event")
    async def on_function_call_event(observer, event):
        await _publish_record("function_call_event", event)

    worker.add_observer(observer)


async def _setup_error_observer(worker):
    try:
        from pipecat.observers.error_observer import ErrorObserver
    except ImportError:
        return

    observer = ErrorObserver()

    @observer.event_handler("on_error")
    async def on_error(observer, event):
        await _publish_record("error", event)

    worker.add_observer(observer)
