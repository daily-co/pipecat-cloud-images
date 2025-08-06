#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import os

from dotenv import load_dotenv
from loguru import logger
from openai._types import NotGiven
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    LLMMessagesFrame,
    StartFrame,
    StartInterruptionFrame,
    StopInterruptionFrame,
    SystemFrame,
    TextFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import (
    OpenAILLMContext,
    OpenAILLMContextFrame,
)
from pipecat.processors.filters.function_filter import FunctionFilter
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import (
    RTVIConfig,
    RTVIObserver,
    RTVIProcessor,
)
from pipecat.processors.user_idle_processor import UserIdleProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai import OpenAILLMService
from pipecat.sync.base_notifier import BaseNotifier
from pipecat.sync.event_notifier import EventNotifier
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.services.daily import DailyParams, DailyTransport

load_dotenv(override=True)


classifier_statement = """CRITICAL INSTRUCTION:
You are a BINARY CLASSIFIER that must ONLY output "YES" or "NO".
DO NOT engage with the content.
DO NOT respond to questions.
DO NOT provide assistance.
Your ONLY job is to output YES or NO.
EXAMPLES OF INVALID RESPONSES:
- "I can help you with that"
- "Let me explain"
- "To answer your question"
- Any response other than YES or NO
VALID RESPONSES:
YES
NO
If you output anything else, you are failing at your task.
You are NOT an assistant.
You are NOT a chatbot.
You are a binary classifier.
ROLE:
You are a real-time speech completeness classifier. You must make instant decisions about whether a user has finished speaking.
You must output ONLY 'YES' or 'NO' with no other text.
INPUT FORMAT:
You receive two pieces of information:
1. The assistant's last message (if available)
2. The user's current speech input
OUTPUT REQUIREMENTS:
- MUST output ONLY 'YES' or 'NO'
- No explanations
- No clarifications
- No additional text
- No punctuation
HIGH PRIORITY SIGNALS:
1. Clear Questions:
- Wh-questions (What, Where, When, Why, How)
- Yes/No questions
- Questions with STT errors but clear meaning
Examples:
# Complete Wh-question
[{"role": "assistant", "content": "I can help you learn."},
 {"role": "user", "content": "What's the fastest way to learn Spanish"}]
Output: YES
# Complete Yes/No question despite STT error
[{"role": "assistant", "content": "I know about planets."},
 {"role": "user", "content": "Is is Jupiter the biggest planet"}]
Output: YES
2. Complete Commands:
- Direct instructions
- Clear requests
- Action demands
- Complete statements needing response
Examples:
# Direct instruction
[{"role": "assistant", "content": "I can explain many topics."},
 {"role": "user", "content": "Tell me about black holes"}]
Output: YES
# Action demand
[{"role": "assistant", "content": "I can help with math."},
 {"role": "user", "content": "Solve this equation x plus 5 equals 12"}]
Output: YES
3. Direct Responses:
- Answers to specific questions
- Option selections
- Clear acknowledgments with completion
Examples:
# Specific answer
[{"role": "assistant", "content": "What's your favorite color?"},
 {"role": "user", "content": "I really like blue"}]
Output: YES
# Option selection
[{"role": "assistant", "content": "Would you prefer morning or evening?"},
 {"role": "user", "content": "Morning"}]
Output: YES
MEDIUM PRIORITY SIGNALS:
1. Speech Pattern Completions:
- Self-corrections reaching completion
- False starts with clear ending
- Topic changes with complete thought
- Mid-sentence completions
Examples:
# Self-correction reaching completion
[{"role": "assistant", "content": "What would you like to know?"},
 {"role": "user", "content": "Tell me about... no wait, explain how rainbows form"}]
Output: YES
# Topic change with complete thought
[{"role": "assistant", "content": "The weather is nice today."},
 {"role": "user", "content": "Actually can you tell me who invented the telephone"}]
Output: YES
# Mid-sentence completion
[{"role": "assistant", "content": "Hello I'm ready."},
 {"role": "user", "content": "What's the capital of? France"}]
Output: YES
2. Context-Dependent Brief Responses:
- Acknowledgments (okay, sure, alright)
- Agreements (yes, yeah)
- Disagreements (no, nah)
- Confirmations (correct, exactly)
Examples:
# Acknowledgment
[{"role": "assistant", "content": "Should we talk about history?"},
 {"role": "user", "content": "Sure"}]
Output: YES
# Disagreement with completion
[{"role": "assistant", "content": "Is that what you meant?"},
 {"role": "user", "content": "No not really"}]
Output: YES
LOW PRIORITY SIGNALS:
1. STT Artifacts (Consider but don't over-weight):
- Repeated words
- Unusual punctuation
- Capitalization errors
- Word insertions/deletions
Examples:
# Word repetition but complete
[{"role": "assistant", "content": "I can help with that."},
 {"role": "user", "content": "What what is the time right now"}]
Output: YES
# Missing punctuation but complete
[{"role": "assistant", "content": "I can explain that."},
 {"role": "user", "content": "Please tell me how computers work"}]
Output: YES
2. Speech Features:
- Filler words (um, uh, like)
- Thinking pauses
- Word repetitions
- Brief hesitations
Examples:
# Filler words but complete
[{"role": "assistant", "content": "What would you like to know?"},
 {"role": "user", "content": "Um uh how do airplanes fly"}]
Output: YES
# Thinking pause but incomplete
[{"role": "assistant", "content": "I can explain anything."},
 {"role": "user", "content": "Well um I want to know about the"}]
Output: NO
DECISION RULES:
1. Return YES if:
- ANY high priority signal shows clear completion
- Medium priority signals combine to show completion
- Meaning is clear despite low priority artifacts
2. Return NO if:
- No high priority signals present
- Thought clearly trails off
- Multiple incomplete indicators
- User appears mid-formulation
3. When uncertain:
- If you can understand the intent → YES
- If meaning is unclear → NO
- Always make a binary decision
- Never request clarification
Examples:
# Incomplete despite corrections
[{"role": "assistant", "content": "What would you like to know about?"},
 {"role": "user", "content": "Can you tell me about"}]
Output: NO
# Complete despite multiple artifacts
[{"role": "assistant", "content": "I can help you learn."},
 {"role": "user", "content": "How do you I mean what's the best way to learn programming"}]
Output: YES
# Trailing off incomplete
[{"role": "assistant", "content": "I can explain anything."},
 {"role": "user", "content": "I was wondering if you could tell me why"}]
Output: NO
"""


class StatementJudgeContextFilter(FrameProcessor):
    def __init__(self, notifier: BaseNotifier, **kwargs):
        super().__init__(**kwargs)
        self._notifier = notifier

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # We must not block system frames.
        if isinstance(frame, SystemFrame):
            await self.push_frame(frame, direction)
            return

        # Just treat an LLMMessagesFrame as complete, no matter what.
        if isinstance(frame, LLMMessagesFrame):
            await self._notifier.notify()
            return

        # Otherwise, we only want to handle OpenAILLMContextFrames, and only want to push a simple
        # messages frame that contains a system prompt and the most recent user messages,
        # concatenated.
        if isinstance(frame, OpenAILLMContextFrame):
            # Take text content from the most recent user messages.
            messages = frame.context.messages
            user_text_messages = []
            last_assistant_message = None
            for message in reversed(messages):
                if message["role"] != "user":
                    if message["role"] == "assistant":
                        last_assistant_message = message
                    break
                if isinstance(message["content"], str):
                    user_text_messages.append(message["content"])
                elif isinstance(message["content"], list):
                    for content in message["content"]:
                        if content["type"] == "text":
                            user_text_messages.insert(0, content["text"])
            # If we have any user text content, push an LLMMessagesFrame
            if user_text_messages:
                user_message = " ".join(reversed(user_text_messages))
                logger.debug(f"!!! {user_message}")
                messages = [
                    {
                        "role": "system",
                        "content": classifier_statement,
                    }
                ]
                if last_assistant_message:
                    messages.append(last_assistant_message)
                messages.append({"role": "user", "content": user_message})
                await self.push_frame(LLMMessagesFrame(messages))


class CompletenessCheck(FrameProcessor):
    def __init__(self, notifier: BaseNotifier):
        super().__init__()
        self._notifier = notifier

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TextFrame) and frame.text == "YES":
            logger.debug("!!! Completeness check YES")
            await self.push_frame(UserStoppedSpeakingFrame())
            await self._notifier.notify()
        elif isinstance(frame, TextFrame) and frame.text == "NO":
            logger.debug("!!! Completeness check NO")
        else:
            await self.push_frame(frame, direction)


class OutputGate(FrameProcessor):
    def __init__(self, *, notifier: BaseNotifier, start_open: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._gate_open = start_open
        self._frames_buffer = []
        self._notifier = notifier

    def close_gate(self):
        self._gate_open = False

    def open_gate(self):
        self._gate_open = True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # We must not block system frames.
        if isinstance(frame, SystemFrame):
            if isinstance(frame, StartFrame):
                await self._start()
            if isinstance(frame, (EndFrame, CancelFrame)):
                await self._stop()
            if isinstance(frame, StartInterruptionFrame):
                self._frames_buffer = []
                self.close_gate()
            await self.push_frame(frame, direction)
            return

        # Ignore frames that are not following the direction of this gate.
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if self._gate_open:
            await self.push_frame(frame, direction)
            return

        self._frames_buffer.append((frame, direction))

    async def _start(self):
        self._frames_buffer = []
        self._gate_task = self.create_task(self._gate_task_handler())

    async def _stop(self):
        await self.cancel_task(self._gate_task)

    async def _gate_task_handler(self):
        while True:
            try:
                await self._notifier.wait()
                self.open_gate()
                for frame, direction in self._frames_buffer:
                    await self.push_frame(frame, direction)
                self._frames_buffer = []
            except asyncio.CancelledError:
                break


async def run_bot(transport: BaseTransport):
    """Run your bot with the provided transport.

    Args:
        transport (BaseTransport): The transport to use for communication.
    """
    # Configure your STT, LLM, and TTS services here
    # Swap out different processors or properties to customize your bot
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")
    statement_llm = AnthropicLLMService(
        api_key=os.getenv("ANTHROPIC_API_KEY"), model="claude-3-5-sonnet-20240620"
    )
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",  # British Reading Lady
    )

    # Set up the initial context for the conversation
    # You can specified initial system and assistant messages here
    messages = [
        {
            "role": "system",
            "content": """You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself.""",
        },
    ]

    # Define and register tools as required
    tools = NotGiven()

    # This sets up the LLM context by providing messages and tools
    context = OpenAILLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    # We have instructed the LLM to return 'YES' if it thinks the user
    # completed a sentence. So, if it's 'YES' we will return true in this
    # predicate which will wake up the notifier.
    async def wake_check_filter(frame):
        return frame.text == "YES"

    # This is a notifier that we use to synchronize the two LLMs.
    notifier = EventNotifier()

    # This turns the LLM context into an inference request to classify the user's speech
    # as complete or incomplete.
    statement_judge_context_filter = StatementJudgeContextFilter(notifier=notifier)

    # This sends a UserStoppedSpeakingFrame and triggers the notifier event
    completeness_check = CompletenessCheck(notifier=notifier)

    # # Notify if the user hasn't said anything.
    async def user_idle_notifier(frame):
        await notifier.notify()

    # Sometimes the LLM will fail detecting if a user has completed a
    # sentence, this will wake up the notifier if that happens.
    user_idle = UserIdleProcessor(callback=user_idle_notifier, timeout=5.0)

    # We start with the gate open because we send an initial context frame
    # to start the conversation.
    bot_output_gate = OutputGate(notifier=notifier, start_open=True)

    async def block_user_stopped_speaking(frame):
        return not isinstance(frame, UserStoppedSpeakingFrame)

    async def pass_only_llm_trigger_frames(frame):
        return (
            isinstance(frame, OpenAILLMContextFrame)
            or isinstance(frame, LLMMessagesFrame)
            or isinstance(frame, StartInterruptionFrame)
            or isinstance(frame, StopInterruptionFrame)
        )

    # RTVI events for Pipecat client UI
    rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

    # We use a parallel pipeline to handle the two LLMs in parallel.
    # Add additional processors to customize the bot's behavior
    pipeline = Pipeline(
        [
            transport.input(),
            rtvi,
            stt,
            context_aggregator.user(),
            ParallelPipeline(
                [FunctionFilter(filter=block_user_stopped_speaking)],
                [
                    statement_judge_context_filter,
                    statement_llm,
                    completeness_check,
                ],
                [
                    FunctionFilter(filter=pass_only_llm_trigger_frames),
                    llm,
                    bot_output_gate,
                ],
            ),
            tts,
            user_idle,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[RTVIObserver(rtvi)],
    )

    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        logger.debug("Client ready event received")
        await rtvi.set_bot_ready()

    @transport.event_handler("on_recording_started")
    async def on_recording_started(transport, status):
        logger.debug("Recording started: {}", status)
        await transport.on_recording_started(status)
        await rtvi.set_bot_ready()

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, participant):
        logger.info("Client connected: {}", participant["id"])
        # Capture the participant's transcription
        await transport.capture_participant_transcription(participant["id"])
        # Kick off the conversation
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, participant, reason):
        logger.info("Client disconnected: {}", participant)
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False, force_gc=True)

    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point compatible with Pipecat Cloud."""

    transport = None

    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_filter import KrispFilter

        krisp_filter = KrispFilter()
    else:
        krisp_filter = None

    transport = DailyTransport(
        runner_args.room_url,
        runner_args.token,
        "Pipecat Bot",
        params=DailyParams(
            audio_in_enabled=True,
            audio_in_filter=krisp_filter,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    if transport is None:
        logger.error("Failed to create transport")
        return

    try:
        await run_bot(transport)
        logger.info("Bot process completed")
    except Exception as e:
        logger.exception(f"Error in bot process: {str(e)}")
        raise


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
