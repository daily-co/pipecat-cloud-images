#!/usr/bin/env python

"""Pipecat Cloud observability observers.

This file is loaded via the PIPECAT_SETUP_FILES mechanism in PipelineWorker.
It attaches the observers Pipecat Cloud reports on to every worker, so a
session leaves behind its startup timing, the latency of each turn and what
that latency was spent on, what each service cost and consumed, who was
speaking and when, the function calls the bot made, and the errors it hit. A
deployment that opts in also leaves behind its whole conversation.

Each record is published to the event pipeline by pcc_events.

Every observer is imported separately and skipped when the import fails, so a
bot pinned to an older Pipecat reports whatever its version has rather than
failing to start.
"""

import asyncio
import hashlib
import json
import time
import uuid
from os import environ

import pcc_events
import pcc_structured_logs
from loguru import logger


def _content_excluded() -> bool:
    """Whether this deployment keeps conversation content out of what it publishes.

    The platform sets `PCC_EXCLUDE_CONTENT`, and only an explicit "false" publishes,
    so an agent the platform never told either way withholds. A developer can make
    that stricter with `PCC_WITHHOLD_CONTENT`, but never looser. Any value but an
    empty one or "false" withholds, so a mistyped value doesn't publish.
    """
    if pcc_structured_logs.platform_exclude_content().strip().lower() != "false":
        return True
    return environ.get("PCC_WITHHOLD_CONTENT", "").strip().lower() not in ("", "false")


_exclude_content = _content_excluded()

if _exclude_content:
    logger.info(
        "[pcc-observability] transcripts are not published "
        f"(PCC_EXCLUDE_CONTENT={pcc_structured_logs.platform_exclude_content()!r}, "
        f"PCC_WITHHOLD_CONTENT={environ.get('PCC_WITHHOLD_CONTENT')!r})"
    )


# The fields each record publishes. A bot installs its own Pipecat, so what a
# record contains is not ours to review; naming fields here keeps one added
# upstream from travelling further than the bot until someone adds it.
#
# Free text stays behind — a tool's arguments and result, the exception its
# handler raised, a processor's error message — since any of it can quote what
# was said. `_CONTENT_FIELDS` below names the free text that travels where the
# transcript does; `push_error_frame` still logs an error's message for the
# bot's own session either way.
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

# Free text that can quote the conversation, published only where the
# transcript is. An error's message is what the failing processor said went
# wrong, and a provider's error can echo the text it was asked to handle.
_CONTENT_FIELDS: dict[str, dict] = {
    "error": {"message": True},
}

# The publisher refuses a record whose properties pass 10 KB, losing the whole
# record, and a provider's error can quote the request that failed in full.
_MAX_CONTENT_CHARS = 2000

# A conversation can pass that limit, so it travels as JSON text cut into parts.
# json.dumps leaves the text ASCII, and publishing it as a string escapes at most
# every character, which keeps a part inside the limit. That relies on the path to
# the publisher leaving `<`, `>` and `&` alone, as JSON encoders do unless they
# escape for HTML, as Go's does by default.
_CONVERSATION_PART_CHARS = 4000

# What one session may send while it shuts down: about 2 MB of conversation.
_MAX_CONVERSATION_PARTS = 500

# A session can't end until its conversation is published, so this is the longest
# publishing can hold one up. Even the longest conversation normally takes a
# fraction of a second. An endpoint that is merely slow never reaches the failure
# cutoff in pcc_events, because each success restarts its count.
_CONVERSATION_PUBLISH_SECS = 10


def _conversation_opted_in() -> bool:
    """Whether this deployment publishes whole conversations.

    A developer opts in with `PCC_EVAL_CONVERSATIONS=true`. It can't override a
    deployment excluding content: the conversation holds what the transcript does,
    and a tool's arguments as well.
    """
    if _exclude_content:
        return False
    return environ.get("PCC_EVAL_CONVERSATIONS", "").strip().lower() == "true"


# The fields each message publishes, by role (pipecat.evals.conversation). A bot
# installs its own Pipecat, so a role or field added upstream stays behind until
# it's named here.
_CONVERSATION_FIELDS: dict[str, tuple[str, ...]] = {
    "user": ("role", "content"),
    "assistant": ("role", "content"),
    "tool_call": ("role", "name", "arguments", "cancelled"),
}


def _published_conversation(conversation: list[dict]) -> list[dict]:
    """The messages and fields of a conversation that this image has reviewed."""
    return [
        {field: message[field] for field in _CONVERSATION_FIELDS[role] if field in message}
        for message in conversation
        if (role := message.get("role")) in _CONVERSATION_FIELDS
    ]


def _conversation_text(conversation: list[dict]) -> str | None:
    """The conversation as JSON text, or `None` when it can't travel."""
    try:
        text = json.dumps(conversation, allow_nan=False)
    except (TypeError, ValueError) as e:
        logger.warning(f"[pcc-observability] Could not serialize the conversation: {e}")
        return None
    if len(text) > _MAX_CONVERSATION_PARTS * _CONVERSATION_PART_CHARS:
        logger.warning(f"[pcc-observability] Conversation too long to publish: {len(text)} chars")
        return None
    return text


async def _publish_conversation(conversation: list[dict]) -> None:
    """Publish one worker's conversation, or a record saying it was left out.

    The parts share a `conversation_id`, because each worker in a bot records its
    own. The pipeline can deliver a part twice or lose one, and publishing gives up
    at `_CONVERSATION_PUBLISH_SECS`, so each also carries the `sha256` of the whole
    text. A reader keeps one copy of each `part`, joins them in order, and trusts
    the result only if the hash matches.
    """
    conversation = _published_conversation(conversation)
    if not conversation:
        return
    conversation_id = str(uuid.uuid4())
    text = _conversation_text(conversation)
    if text is None:
        await pcc_events.publish_event(
            "eval_conversation", {"conversation_id": conversation_id, "parts": 0, "omitted": True}
        )
        return
    digest = hashlib.sha256(text.encode()).hexdigest()
    parts = [
        text[start : start + _CONVERSATION_PART_CHARS]
        for start in range(0, len(text), _CONVERSATION_PART_CHARS)
    ]
    # One part at a time, on purpose. The pod's local endpoint answers as soon as a
    # record is buffered, so even the longest conversation takes a fraction of a
    # second, and when that endpoint stalls it stalls concurrent posts too.
    attempted = 0
    try:
        async with asyncio.timeout(_CONVERSATION_PUBLISH_SECS) as limit:
            for index, part in enumerate(parts):
                # aiohttp's own timeout can use up this one's cancel when both fall due
                # together, and publish_event then catches it as a failed post.
                if limit.expired():
                    raise TimeoutError
                attempted = index + 1
                await pcc_events.publish_event(
                    "eval_conversation",
                    {
                        "conversation_id": conversation_id,
                        "part": index,
                        "parts": len(parts),
                        "sha256": digest,
                        "text": part,
                    },
                )
    except TimeoutError:
        logger.warning(
            "[pcc-observability] Stopped publishing the conversation at the"
            f" {_CONVERSATION_PUBLISH_SECS}s limit: {attempted} of {len(parts)} parts attempted"
        )


def _published_fields(event_name: str) -> dict:
    """The fields a record of this name publishes for this deployment."""
    fields = _PUBLISHED_FIELDS[event_name]
    if _exclude_content:
        return fields
    return {**fields, **_CONTENT_FIELDS.get(event_name, {})}


async def _publish_record(event_name: str, record):
    """Publish the fields `_published_fields` names for this record.

    A record that will not serialize is dropped rather than raised into the
    handler that reported it.
    """
    try:
        properties = record.model_dump(
            mode="json", exclude_none=True, include=_published_fields(event_name)
        )
    except Exception as e:
        logger.warning(f"[pcc-observability] Could not serialize {event_name}: {e}")
        return
    for field in _CONTENT_FIELDS.get(event_name, {}):
        text = properties.get(field)
        if isinstance(text, str) and len(text) > _MAX_CONTENT_CHARS:
            properties[field] = text[:_MAX_CONTENT_CHARS] + "…"
    await pcc_events.publish_event(event_name, properties)


async def setup_pipeline_worker(worker):
    """Called by PipelineWorker._load_setup_files() for each worker instance."""
    await _setup_transcripts(worker)
    await _setup_startup_timing_observer(worker)
    await _setup_user_bot_latency_observer(worker)
    await _setup_service_metrics_observer(worker)
    await _setup_speaking_observer(worker)
    await _setup_function_call_observer(worker)
    await _setup_error_observer(worker)
    await _setup_conversation_observer(worker)


# Backwards compatibility: Pipecat < 1.3.0 looks for ``setup_pipeline_task``.
# 1.3.0+ prefers ``setup_pipeline_worker`` (and checks it first), falling back
# to the old name only with a DeprecationWarning, so defining both keeps us
# warning-free across versions. Drop this alias once the minimum supported
# Pipecat is >= 1.3.0.
setup_pipeline_task = setup_pipeline_worker


async def setup_worker_runner(runner):
    """Called by WorkerRunner._load_setup_files() for each runner instance.

    Every observer here is attached per worker, so the runner has nothing to
    do. Pipecat 1.3.0+ offers each PIPECAT_SETUP_FILES entry to the runner as
    well, and logs a warning for a file without this hook, once per session.
    """


def _every_processor(processor):
    """Walk a pipeline, including the processors nested inside it."""
    for child in processor.processors:
        yield child
        yield from _every_processor(child)


async def _setup_transcripts(worker):
    """Publish what was said, from the aggregators that assemble it.

    The transcript carries the conversation itself, rather than facts about it.
    Most records here name a service, a duration or a kind; this names what a
    person said, so a deployment excluding content doesn't publish it.

    The text arrives from the aggregators rather than from a frame, because
    they hold the turn: the words a turn ended up with, after the corrections
    and the interruptions that a stream of frames still has to be assembled
    into.
    """
    if _exclude_content:
        return

    try:
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMAssistantAggregator,
            LLMUserAggregator,
        )
    except ImportError:
        return

    for processor in _every_processor(worker.pipeline):
        if isinstance(processor, LLMUserAggregator):

            @processor.event_handler("on_user_turn_message_added")
            async def on_user_turn_message_added(processor, message):
                if not message.content:
                    return
                await pcc_events.publish_event(
                    "transcript",
                    {
                        "role": "user",
                        "text": message.content,
                        "turn_started_at": message.timestamp,
                        "timestamp": time.time(),
                    },
                )

        elif isinstance(processor, LLMAssistantAggregator):

            @processor.event_handler("on_assistant_turn_stopped")
            async def on_assistant_turn_stopped(processor, message):
                if not message.content:
                    return
                await pcc_events.publish_event(
                    "transcript",
                    {
                        "role": "assistant",
                        "text": message.content,
                        "interrupted": message.interrupted,
                        "turn_started_at": message.timestamp,
                        "timestamp": time.time(),
                    },
                )


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
        await pcc_events.publish_event(
            "user_bot_latency",
            {"latency_secs": round(latency_seconds, 3), "timestamp": time.time()},
        )

    @observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(observer, breakdown):
        await _publish_record("latency_breakdown", breakdown)

    @observer.event_handler("on_first_bot_speech_latency")
    async def on_first_bot_speech_latency(observer, latency_seconds):
        await pcc_events.publish_event(
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
    # so neither travels here: what a bot's tools were asked and answered belongs
    # to the bot, unless its developer opts in to publishing whole conversations.
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


async def _setup_conversation_observer(worker):
    if not _conversation_opted_in():
        return
    try:
        from pipecat.evals.conversation_observer import EvalConversationObserver
    except ImportError:
        # Said for every worker, not once per process: logs are read by session, and
        # a session on a reused pod needs to say why it has no conversation too.
        logger.info(
            "[pcc-observability] conversation not published: needs pipecat-ai 1.13 or later"
        )
        return

    observer = EvalConversationObserver()

    @observer.event_handler("on_conversation_ended")
    async def on_conversation_ended(observer, message):
        await _publish_conversation(message.conversation)

    worker.add_observer(observer)
