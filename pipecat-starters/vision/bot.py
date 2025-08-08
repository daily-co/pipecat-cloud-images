#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os

from dotenv import load_dotenv
from loguru import logger
from openai._types import NOT_GIVEN, NotGiven
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIObserver, RTVIProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.services.anthropic.llm import AnthropicLLMContext, AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.services.daily import DailyParams, DailyTransport

load_dotenv(override=True)


class AnthropicContextWithVisionTool(AnthropicLLMContext):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_tools(self._tools)
        self._llm = None
        self._rtvi = None
        self._video_participant_id = None

    def set_video_participant_id(self, video_participant_id):
        self._video_participant_id = video_participant_id

    async def get_image(
        self, function_name, tool_call_id, arguments, llm, context, result_callback
    ):
        if self._llm and self._rtvi:
            text_context = arguments["text_context"]
            await self._llm.request_image_frame(
                user_id=self._video_participant_id,
                function_name=function_name,
                tool_call_id=tool_call_id,
                text_content=text_context,
            )
            await self._rtvi.handle_function_call(
                function_name, tool_call_id, arguments, llm, context, None
            )

    def register_get_image_function(self, llm, rtvi):
        self._llm = llm
        self._rtvi = rtvi
        self._llm.register_function("get_image", self.get_image)

    def set_tools(self, tools: dict):
        if isinstance(tools, NotGiven):
            tools = []
        super().set_tools(tools)
        self._add_get_image_tool()

    def _add_get_image_tool(self):
        if self._tools is NOT_GIVEN:
            self._tools = []
        self._tools.append(
            {
                "name": "get_image",
                "description": "Retrieve an image from the available camera or video stream.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text_context": {
                            "type": "string",
                            "description": "Relevant text about the image request. For example, this could be the question that a user is asking about the camera or video stream.",
                        }
                    },
                    "required": ["text_context"],
                },
            }
        )


async def run_bot(transport: BaseTransport):
    """Run your bot with the provided transport.

    Args:
        transport (BaseTransport): The transport to use for communication.
    """
    # Configure your STT, LLM, and TTS services here
    # Swap out different processors or properties to customize your bot
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    llm = AnthropicLLMService(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        model="claude-3-5-sonnet-latest",
        enable_prompt_caching_beta=True,
    )
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",  # British Reading Lady
    )

    # Set up the initial context for the conversation
    # You can specified initial system and assistant messages here
    system_prompt = """You are a helpful assistant who converses with a user and answers questions. Respond concisely to general questions.

    Your response will be turned into speech so use only simple words and punctuation.

    You have access to one tools: get_image.

    You can answer questions about the user's video stream using the get_image tool. Some examples of phrases that indicate you should use the get_image tool are:
    - What do you see?
    - What's in the video?
    - Can you describe the video?
    - Tell me about what you see.
    - Tell me something interesting about what you see.
    - What's happening in the video?

    If you need to use a tool, simply use the tool. Do not tell the user the tool you are using. Be brief and concise."""

    messages = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": system_prompt,
                }
            ],
        },
        {
            "role": "user",
            "content": "Start the conversation by introducing yourself.",
        },
    ]

    # Define and register tools as required
    tools = NotGiven()

    # This sets up the LLM context by providing messages and tools
    context = AnthropicContextWithVisionTool(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    # RTVI events for Pipecat client UI
    rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

    context.register_get_image_function(llm, rtvi)

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
        # Get the participant ID of the user
        video_participant_id = participant["id"]
        # Capture the participant's video
        await transport.capture_participant_video(video_participant_id, framerate=0)
        # Set the video participant ID in the context
        context.set_video_participant_id(video_participant_id)
        # Capture the participant's transcription
        await transport.capture_participant_transcription(participant["id"])
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
