#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from openai._types import NotGiven
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIObserver, RTVIProcessor
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport

load_dotenv(override=True)


async def main(room_url: str, token: str, session_logger=None):
    # Use the provided session logger if available, otherwise use the default logger
    log = session_logger or logger
    log.debug("Starting bot in room: {}", room_url)

    async with aiohttp.ClientSession() as session:
        transport = DailyTransport(
            room_url,
            token,
            "Voice AI Bot",
            DailyParams(
                audio_out_enabled=True,
                vad_enabled=True,
                vad_analyzer=SileroVADAnalyzer(),
                vad_audio_passthrough=True,
            ),
        )

        # Configure your STT, LLM, and TTS services here
        # Swap out different processors or properties to customize your bot
        stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
        llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")
        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id="79a125e8-cd45-4c13-8a67-188112f4dd22",
        )

        # Set up the initial context for the conversation
        # You can specified initial system and assistant messages here
        messages = [
            {
                "role": "system",
                "content": "You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself.",
            },
        ]

        # Define and register tools as required
        tools = NotGiven()

        # This sets up the LLM context by providing messages and tools
        context = OpenAILLMContext(messages, tools)
        context_aggregator = llm.create_context_aggregator(context)

        # RTVI events for Pipecat client UI
        rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

        # A core voice AI pipeline
        # Add additional processors to customize the bot's behavior
        pipeline = Pipeline(
            [
                transport.input(),
                rtvi,
                stt,
                context_aggregator.user(),
                llm,
                tts,
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
            log.debug("Client ready event received")
            await rtvi.set_bot_ready()

        @transport.event_handler("on_recording_started")
        async def on_recording_started(transport, status):
            log.debug("Recording started: {}", status)
            await transport.on_recording_started(status)
            await rtvi.set_bot_ready()

        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            log.info("First participant joined: {}", participant["id"])
            # Capture the participant's transcription
            await transport.capture_participant_transcription(participant["id"])
            # Kick off the conversation
            await task.queue_frames([context_aggregator.user().get_context_frame()])

        @transport.event_handler("on_participant_left")
        async def on_participant_left(transport, participant, reason):
            log.info("Participant left: {}", participant)
            await task.cancel()

        runner = PipelineRunner(handle_sigint=False, force_gc=True)

        await runner.run(task)


async def bot(config, room_url: str, token: str, session_id=None, session_logger=None):
    """Main bot entry point compatible with the FastAPI route handler.

    Args:
        config: The configuration object from the request body
        room_url: The Daily room URL
        token: The Daily room token
        session_id: The session ID for logging
        session_logger: The session-specific logger
    """
    log = session_logger or logger
    log.info(f"Bot process initialized {room_url} {token}")

    try:
        await main(room_url, token, session_logger)
        log.info("Bot process completed")
    except Exception as e:
        log.exception(f"Error in bot process: {str(e)}")
        raise
