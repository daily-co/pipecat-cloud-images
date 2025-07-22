#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os

from dotenv import load_dotenv
from loguru import logger
from openai._types import NotGiven
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import (
    OpenAILLMContext,
)
from pipecat.processors.frameworks.rtvi import (
    RTVIConfig,
    RTVIObserver,
    RTVIProcessor,
)
from pipecat.services.openai_realtime_beta import (
    InputAudioTranscription,
    OpenAIRealtimeBetaLLMService,
    SessionProperties,
    TurnDetection,
)
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecatcloud.agent import DailySessionArguments

load_dotenv(override=True)


async def main(room_url: str, token: str):
    logger.debug("starting bot in room: {}", room_url)

    transport = DailyTransport(
        room_url,
        token,
        "Voice AI Bot",
        DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    session_properties = SessionProperties(
        input_audio_transcription=InputAudioTranscription(),
        # Set openai TurnDetection parameters. Not setting this at all will turn it
        # on by default
        turn_detection=TurnDetection(silence_duration_ms=1000),
        # Or set to False to disable openai turn detection and use transport VAD
        # turn_detection=False,
        # tools=tools,
        instructions="""You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself.""",
    )

    llm = OpenAIRealtimeBetaLLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        session_properties=session_properties,
        start_audio_paused=False,
    )

    # Define and register tools as required
    tools = NotGiven()

    # This sets up the LLM context by providing messages and tools
    context = OpenAILLMContext([], tools)
    context_aggregator = llm.create_context_aggregator(context)

    # RTVI events for Pipecat client UI
    rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

    # A core speech-to-speech AI pipeline
    # Add additional processors to customize the bot's behavior
    pipeline = Pipeline(
        [
            transport.input(),
            rtvi,
            context_aggregator.user(),
            llm,
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

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info("First participant joined: {}", participant["id"])
        # Capture the participant's transcription
        await transport.capture_participant_transcription(participant["id"])
        # Kick off the conversation
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.info("Participant left: {}", participant)
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False, force_gc=True)

    await runner.run(task)


async def bot(args: DailySessionArguments):
    """Main bot entry point compatible with the FastAPI route handler.

    Args:
        room_url: The Daily room URL
        token: The Daily room token
        body: The configuration object from the request body
        session_id: The session ID for logging
    """
    logger.info(f"Bot process initialized {args.room_url} {args.token}")

    try:
        await main(args.room_url, args.token)
        logger.info("Bot process completed")
    except Exception as e:
        logger.exception(f"Error in bot process: {str(e)}")
        raise
