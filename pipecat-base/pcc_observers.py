#!/usr/bin/env python

"""Pipecat Cloud observability observers.

This file is loaded via the PIPECAT_SETUP_FILES mechanism in PipelineTask.
It injects StartupTimingObserver and UserBotLatencyObserver into every
PipelineTask so that startup timing and user-bot latency data is logged
automatically for Pipecat Cloud observability.
"""

import json

from loguru import logger


async def setup_pipeline_worker(worker):
    """Called by PipelineWorker._load_setup_files() for each worker instance."""
    await _setup_startup_timing_observer(worker)
    await _setup_user_bot_latency_observer(worker)


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

    worker.add_observer(observer)


async def _setup_user_bot_latency_observer(worker):
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

    worker.add_observer(observer)
