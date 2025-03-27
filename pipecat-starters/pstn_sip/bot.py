#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os

from dotenv import load_dotenv
from loguru import logger
from openai.types.chat import ChatCompletionToolParam
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndTaskFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_services import LLMService
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecatcloud.agent import DailySessionArguments

load_dotenv(override=True)


# Function call for the bot to terminate the call.
# Needed in the case of dial-in and dial-out for the bot to hang up
async def terminate_call(
    function_name, tool_call_id, args, llm: LLMService, context, result_callback
):
    """Function the bot can call to terminate the call; e.g. upon completion of a voicemail message."""
    await llm.queue_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
    await result_callback("Goodbye")


async def main(room_url: str, token: str, body: dict):
    logger.debug("Starting bot in room: {}", room_url, body)

    # Dial-in configuration:
    # dialin_settings are received when a call is triggered to
    # Daily via pinless_dialin. This can be a phone number on Daily or a
    # sip interconnect from Twilio or Telnyx.
    dialin_settings = None
    dialled_phonenum = None
    caller_phonenum = None
    if raw_dialin_settings := body.get("dialin_settings"):
        # these fields can capitalize the first letter
        dialled_phonenum = (raw_dialin_settings.get("To") or raw_dialin_settings.get("to"),)
        caller_phonenum = (raw_dialin_settings.get("From") or raw_dialin_settings.get("from"),)
        dialin_settings = {
            # these fields can be received as snake_case or camelCase.
            "call_id": raw_dialin_settings.get("callId") or raw_dialin_settings.get("call_id"),
            "call_domain": raw_dialin_settings.get("callDomain")
            or raw_dialin_settings.get("call_domain"),
        }
        logger.debug(
            f"Dialin settings: To: {dialled_phonenum}, From: {caller_phonenum}, dialin_settings: {dialin_settings}"
        )

    # Dial-out configuration
    dialout_settings = None
    dialout_settings = body.get("dialout_settings")
    logger.debug(f"Dialout settings: {dialout_settings}")
    max_attempts = 5
    dialout_attempt_count = 0

    transport = DailyTransport(
        room_url,
        token,
        "Voice AI Bot",
        DailyParams(
            dialin_settings=dialin_settings,
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
    # or register tools for the LLM to use
    messages = [
        {
            "role": "system",
            "content": """You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself.

            - If the user no longer needs assistance, say: "Okay, thank you! Have a great day!"
            - Then call `terminate_call` immediately.""",
        },
    ]

    # Registering the terminate_call function as a tool
    # This is used to terminate the call when the bot is done
    llm.register_function("terminate_call", terminate_call)
    tools = [
        ChatCompletionToolParam(
            type="function",
            function={
                "name": "terminate_call",
                "description": "Terminate the call",
            },
        )
    ]
    # tools = NotGiven()

    # This sets up the LLM context by providing messages and tools
    context = OpenAILLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    # A core voice AI pipeline
    # Add additional processors to customize the bot's behavior
    pipeline = Pipeline(
        [
            transport.input(),
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
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    # Helper function to start dialout
    # this is called when the call state is updated to "joined"
    # and set_bot_ready() when dialout answered is fired
    async def start_dialout(transport, dialout_settings):
        nonlocal dialout_attempt_count
        dialout_attempt_count += 1

        if dialout_attempt_count > max_attempts:
            logger.error(f"Max dialout attempts ({max_attempts}) reached, giving up")
            return

        logger.debug(f"Dialout attempt {dialout_attempt_count}/{max_attempts}")

        for dialout_setting in dialout_settings:
            # Change from attribute access to dictionary access
            if "phoneNumber" in dialout_setting:
                logger.info(f"Dialing number: {dialout_setting['phoneNumber']}")
                if "callerId" in dialout_setting:
                    logger.info(f"with callerId: {dialout_setting['callerId']}")
                    await transport.start_dialout(
                        {
                            "phoneNumber": dialout_setting["phoneNumber"],
                            "callerId": dialout_setting["callerId"],
                        }
                    )
                else:
                    logger.info("with no callerId")
                    await transport.start_dialout({"phoneNumber": dialout_setting["phoneNumber"]})
            elif "sipUri" in dialout_setting:
                logger.info(f"Dialing sipUri: {dialout_setting['sipUri']}")
                await transport.start_dialout({"sipUri": dialout_setting["sipUri"]})

    # Configure handlers for dialing out
    # dialout connected fires when the call starts ringing
    @transport.event_handler("on_dialout_connected")
    async def on_dialout_connected(transport, data):
        logger.debug(f"Dial-out connected: {data}")

    # dialout answered fires when the ringing call is off hook
    @transport.event_handler("on_dialout_answered")
    async def on_dialout_answered(transport, data):
        logger.debug(f"Dial-out answered: {data} and set_bot_ready")

    @transport.event_handler("on_dialout_stopped")
    async def on_dialout_stopped(transport, data):
        logger.debug(f"Dial-out stopped: {data}")

    @transport.event_handler("on_dialout_error")
    async def on_dialout_error(transport, data):
        logger.error(f"Dial-out error: {data}")
        await start_dialout(transport, dialout_settings)

    @transport.event_handler("on_dialout_warning")
    async def on_dialout_warning(transport, data):
        logger.warning(f"Dial-out warning: {data}")

    # Configuring handlers for dialing in
    # dialin ready fires when the room has REGISTERED
    # the sip addresses with the SIP network
    @transport.event_handler("on_dialin_ready")
    async def on_dialin_ready(transport, data):
        # For Twilio, Telnyx, etc. You need to update the state of the call
        # and forward it to the sip_uri..
        logger.debug(f"Dial-in ready: {data}")

    @transport.event_handler("on_dialin_connected")
    async def on_dialin_connected(transport, data):
        logger.debug(f"Dial-in connected: {data} and set_bot_ready")

    @transport.event_handler("on_dialin_stopped")
    async def on_dialin_stopped(transport, data):
        logger.debug(f"Dial-in stopped: {data}")

    @transport.event_handler("on_dialin_error")
    async def on_dialin_error(transport, data):
        logger.error(f"Dial-in error: {data}")
        # the bot should leave the call if there is an error
        await task.cancel()

    @transport.event_handler("on_dialin_warning")
    async def on_dialin_warning(transport, data):
        logger.warning(f"Dial-in warning: {data}")

    @transport.event_handler("on_call_state_updated")
    async def on_call_state_updated(transport, state):
        logger.info(f"on_call_state_updated, state: {state}")
        if state == "joined" and dialout_settings:
            logger.info(f"on_call_state_updated, dialout_settings: {dialout_settings}")
            await start_dialout(transport, dialout_settings)
        if state == "left":
            await task.cancel()

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info("First participant joined: {}", participant["id"])
        # Capture the participant's transcription
        await transport.capture_participant_transcription(participant["id"])
        # Kick off the conversation
        if dialin_settings:
            # For the dialin case, we want the bot to greet the user.
            # We can prompt the bot to speak by putting the context into the pipeline.
            await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_joined")
    async def on_joined(transport, data):
        session_id = data["meetingSession"]["id"]
        bot_id = data["participants"]["local"]["id"]
        logger.info(f"Session ID: {session_id}, Bot ID: {bot_id}")

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.debug(f"Participant left: {participant}, reason: {reason}")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False, force_gc=True)

    await runner.run(task)


async def bot(args: DailySessionArguments):
    """Main bot entry point compatible with the FastAPI route handler.

    Args:
        room_url: The Daily room URL
        token: The Daily room token
        body: The configuration object from the request body can contain dialin_settings, dialout_settings, voicemail_detection, and call_transfer
        session_id: The session ID for logging
    """
    logger.info(f"Bot process initialized {args.room_url} {args.token}")

    try:
        await main(args.room_url, args.token, args.body)
        logger.info("Bot process completed")
    except Exception as e:
        logger.exception(f"Error in bot process: {str(e)}")
        raise
