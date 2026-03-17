#!/usr/bin/env python

"""Pipecat Cloud observability observers.

This file is loaded via the PIPECAT_SETUP_FILES mechanism in PipelineTask.
It injects StartupTimingObserver and UserBotLatencyObserver into every
PipelineTask so that startup timing and user-bot latency data is logged
automatically for Pipecat Cloud observability.
"""

import json
import uuid
from datetime import datetime, timezone
from os import environ

import aiohttp
from loguru import logger
from shared_state import GLOBALS

_event_publisher_endpoint = environ.get("PIPECAT_EVENT_PUBLISHER_ENDPOINT")
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
    if not _event_publisher_endpoint:
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
        async with session.post(_event_publisher_endpoint, json=payload) as resp:
            if resp.status >= 400:
                logger.warning(
                    f"[pcc-observability] Event publish failed: {resp.status}"
                )
    except Exception as e:
        logger.warning(f"[pcc-observability] Event publish error: {e}")


async def setup_pipeline_task(task):
    """Called by PipelineTask._load_setup_files() for each task instance."""
    await _setup_startup_timing_observer(task)
    await _setup_user_bot_latency_observer(task)


async def _setup_startup_timing_observer(task):
    try:
        from pipecat.observers.startup_timing_observer import StartupTimingObserver
    except ImportError:
        return

    observer = StartupTimingObserver()

    @observer.event_handler("on_startup_timing_report")
    async def on_startup_timing_report(observer, report):
        processors = [
            {
                "name": t.processor_name,
                "offset": round(t.start_offset_secs, 3),
                "duration": round(t.duration_secs, 3),
            }
            for t in report.processor_timings
        ]
        logger.info(
            f"[pcc-observability] Startup timing"
            f" | start_time={report.start_time:.3f}"
            f" | total={report.total_duration_secs:.3f}s"
            f" | processors: {json.dumps(processors)}"
        )
        await _publish_event("startup_timing", {
            "start_time": round(report.start_time, 3),
            "total_duration_secs": round(report.total_duration_secs, 3),
            "processors": processors,
        })

    @observer.event_handler("on_transport_timing_report")
    async def on_transport_timing_report(observer, report):
        properties = {}
        properties["start_time"] = round(report.start_time, 3)
        if report.bot_connected_secs is not None:
            properties["bot_connected_secs"] = round(report.bot_connected_secs, 3)
        if report.client_connected_secs is not None:
            properties["client_connected_secs"] = round(report.client_connected_secs, 3)

        formatted = " | ".join(f"{k}={v}" for k, v in properties.items())
        logger.info(f"[pcc-observability] Transport timing | {formatted}")
        await _publish_event("transport_timing", properties)

    task.add_observer(observer)


async def _setup_user_bot_latency_observer(task):
    try:
        from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
    except ImportError:
        return

    observer = UserBotLatencyObserver()

    @observer.event_handler("on_latency_measured")
    async def on_latency_measured(observer, latency_seconds):
        logger.info(f"[pcc-observability] User-bot latency | latency={latency_seconds:.3f}s")
        await _publish_event("user_bot_latency", {
            "latency_secs": round(latency_seconds, 3),
        })

    @observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(observer, breakdown):
        events = breakdown.chronological_events()
        start = ""
        if breakdown.user_turn_start_time is not None:
            start = f" start_time={breakdown.user_turn_start_time:.3f} |"
        logger.info(f"[pcc-observability] Latency breakdown |{start} events: {json.dumps(events)}")

        properties = {"events": events}
        if breakdown.user_turn_start_time is not None:
            properties["start_time"] = round(breakdown.user_turn_start_time, 3)
        await _publish_event("latency_breakdown", properties)

    @observer.event_handler("on_first_bot_speech_latency")
    async def on_first_bot_speech_latency(observer, latency_seconds):
        logger.info(f"[pcc-observability] First bot speech | latency={latency_seconds:.3f}s")
        await _publish_event("first_bot_speech_latency", {
            "latency_secs": round(latency_seconds, 3),
        })

    task.add_observer(observer)
