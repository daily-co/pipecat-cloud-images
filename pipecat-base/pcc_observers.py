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

import time
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


async def _publish_event(event_name: str, event_properties: dict | None = None):
    """Publish an event to the event publisher endpoint if configured.

    Every observer is attached to a worker inside a session, so a record with
    no session to name is one nothing can be joined to. It goes no further.
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


# The fields each record publishes. A bot installs its own Pipecat, so what a
# record contains is not ours to review; naming fields here keeps one added
# upstream from travelling further than the bot until someone adds it.
#
# Free text stays behind — a tool's arguments and result, the exception its
# handler raised, a processor's error message — since any of it can quote what
# was said; `push_error_frame` still logs the message for the bot's own
# session.
_PUBLISHED_FIELDS: dict[str, dict] = {
    "startup_timing": {
        "start_time": True,
        "total_duration_secs": True,
        "setup_phase_secs": True,
        "start_phase_secs": True,
        "processor_timings": {
            "__all__": {
                "processor_name",
                "start_offset_secs",
                "duration_secs",
                "setup_duration_secs",
                "start_duration_secs",
            }
        },
        "warmup": {"duration_secs", "blocking_duration_secs"},
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
        "contributions": {
            "__all__": {"key", "label", "owner", "owner_kind", "start_time", "duration_secs"}
        },
        "ttfb": {"__all__": {"processor", "model", "start_time", "duration_secs"}},
        "text_aggregation": {"processor", "start_time", "duration_secs"},
        "function_calls": {"__all__": {"function_name", "start_time", "duration_secs"}},
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
        await _publish_event(
            "user_bot_latency",
            {"latency_secs": round(latency_seconds, 3), "timestamp": time.time()},
        )

    @observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(observer, breakdown):
        await _publish_record("latency_breakdown", breakdown)

    @observer.event_handler("on_first_bot_speech_latency")
    async def on_first_bot_speech_latency(observer, latency_seconds):
        await _publish_event(
            "first_bot_speech_latency",
            {"latency_secs": round(latency_seconds, 3), "timestamp": time.time()},
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
