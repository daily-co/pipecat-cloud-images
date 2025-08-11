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
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIObserver, RTVIProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.services.daily import DailyParams, DailyTransport

load_dotenv(override=True)


async def run_bot(transport: BaseTransport):
    """Run your bot with the provided transport.

    Args:
        transport (BaseTransport): The transport to use for communication.
    """
    # Configure your STT, LLM, and TTS services here
    # Swap out different processors or properties to customize your bot
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",  # British Reading Lady
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
        # Kick off the conversation
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, participant):
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
