#!/usr/bin/env python

"""Pipecat Cloud observability observers.

This file is loaded via the PIPECAT_SETUP_FILES mechanism in PipelineTask.
It injects StartupTimingObserver and UserBotLatencyObserver into every
PipelineTask so that startup timing and user-bot latency data is logged
automatically for Pipecat Cloud observability.
"""

import json

from loguru import logger


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

    @observer.event_handler("on_transport_timing_report")
    async def on_transport_timing_report(observer, report):
        parts = [f"start_time={report.start_time:.3f}"]
        if report.bot_connected_secs is not None:
            parts.append(f"bot_connected={report.bot_connected_secs:.3f}s")
        if report.client_connected_secs is not None:
            parts.append(f"client_connected={report.client_connected_secs:.3f}s")
        logger.info(f"[pcc-observability] Transport timing | {' | '.join(parts)}")

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

    @observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(observer, breakdown):
        events = breakdown.chronological_events()
        start = ""
        if breakdown.user_turn_start_time is not None:
            start = f" start_time={breakdown.user_turn_start_time:.3f} |"
        logger.info(f"[pcc-observability] Latency breakdown |{start} events: {json.dumps(events)}")

    @observer.event_handler("on_first_bot_speech_latency")
    async def on_first_bot_speech_latency(observer, latency_seconds):
        logger.info(f"[pcc-observability] First bot speech | latency={latency_seconds:.3f}s")

    task.add_observer(observer)
