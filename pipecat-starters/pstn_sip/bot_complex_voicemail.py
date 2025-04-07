#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import functools
import os
import sys

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndTaskFrame,
    StopTaskFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecatcloud.agent import DailySessionArguments

load_dotenv(override=True)

# --- Logging Configuration ---
logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "DEBUG"))
# --- End Logging Configuration ---


# Function call for the bot to terminate the call.
async def terminate_call(
    call_flow_state,
    function_name,
    tool_call_id,
    args,
    llm: LLMService,
    context,
    result_callback,
):
    """Function the bot can call to terminate the call."""
    logger.info(f"Function call: {function_name} triggered. Terminating call.")

    # Send EndTaskFrame upstream to signal the LLM to stop processing *further* tasks
    # The transport or GeneralEventHandler should handle the actual leaving based on state.
    await llm.queue_frame(EndTaskFrame(), FrameDirection.UPSTREAM)


class CallFlowState:
    """State to track if human was detected"""

    def __init__(self):
        self.human_detected = False

    def set_human_detected(self):
        """Set the state to indicate a human was detected."""
        self.human_detected = True


class DialInHandler:
    """Handles all dial-in related functionality and event handling.

    This class encapsulates the logic for incoming calls and handling
    all dial-in related events from the Daily platform.
    """

    def __init__(self, transport, task, context_aggregator):
        """Initialize the DialInHandler.

        Args:
            transport: The Daily transport instance
            task: The PipelineTask instance
            context_aggregator: The context aggregator for the LLM
            call_flow_state: Handles call states
        """
        self.transport = transport
        self.task = task
        self.context_aggregator = context_aggregator
        self._register_handlers()

    def _register_handlers(self):
        """Register all event handlers related to dial-in functionality."""

        @self.transport.event_handler("on_dialin_ready")
        async def on_dialin_ready(transport, data):
            """Handler for when the dial-in is ready (SIP addresses registered with the SIP network)."""
            # For Twilio, Telnyx, etc. You need to update the state of the call
            # and forward it to the sip_uri.
            logger.debug(f"Dial-in ready: {data}")

        @self.transport.event_handler("on_dialin_connected")
        async def on_dialin_connected(transport, data):
            """Handler for when a dial-in call is connected."""
            logger.debug(f"Dial-in connected: {data}")

        @self.transport.event_handler("on_dialin_stopped")
        async def on_dialin_stopped(transport, data):
            """Handler for when a dial-in call is stopped."""
            logger.debug(f"Dial-in stopped: {data}")

        @self.transport.event_handler("on_dialin_error")
        async def on_dialin_error(transport, data):
            """Handler for dial-in errors."""
            logger.error(f"Dial-in error: {data}")
            # The bot should leave the call if there is an error
            await self.task.cancel()

        @self.transport.event_handler("on_dialin_warning")
        async def on_dialin_warning(transport, data):
            """Handler for dial-in warnings."""
            logger.warning(f"Dial-in warning: {data}")

        @self.transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            """Handler for when the first participant joins the call."""
            logger.info(f"First participant joined: {participant['id']}")
            # Capture the participant's transcription
            await transport.capture_participant_transcription(participant["id"])

            # For the dial-in case, we want the bot to greet the user.
            # We can prompt the bot to speak by putting the context into the pipeline.
            await self.task.queue_frames(
                [self.context_aggregator.user().get_context_frame()]
            )


class DialOutHandler:
    """Handles all dial-out related functionality including retries and event handling.

    This class encapsulates the logic for making outbound calls, managing retry
    attempts, and handling all dial-out related events from the Daily platform.
    """

    def __init__(
        self,
        transport,
        task,
        dialout_settings,
        max_attempts=5,
    ):
        """Initialize the DialOutHandler.

        Args:
            transport: The Daily transport instance
            task: The PipelineTask instance
            dialout_settings: Configuration for the outbound call
            max_attempts: Maximum number of dial-out attempts
        """
        self.transport = transport
        self.task = task
        self.dialout_settings = dialout_settings
        self.max_attempts = max_attempts
        self.dialout_attempt_count = 0
        self._register_handlers()
        logger.info(f"Initialized DialOutHandler with settings: {dialout_settings}")

    async def start_dialout(self):
        """Initiates an outbound call using the configured dial-out settings.

        This method will attempt to make an outbound call and will track the
        number of attempts made, giving up after reaching the maximum number
        of attempts configured.
        """
        self.dialout_attempt_count += 1

        if self.dialout_attempt_count > self.max_attempts:
            logger.error(
                f"Max dialout attempts ({self.max_attempts}) reached, giving up."
            )
            return
        logger.info(f"Dialout attempt {self.dialout_attempt_count}/{self.max_attempts}")

        for dialout_setting in self.dialout_settings:
            if "phoneNumber" in dialout_setting:
                logger.info(f"Dialing number: {dialout_setting['phoneNumber']}")
                if "callerId" in dialout_setting:
                    logger.info(f"with callerId: {dialout_setting['callerId']}")
                    await self.transport.start_dialout(
                        {
                            "phoneNumber": dialout_setting["phoneNumber"],
                            "callerId": dialout_setting["callerId"],
                        }
                    )
                else:
                    logger.info("with no callerId")
                    await self.transport.start_dialout(
                        {"phoneNumber": dialout_setting["phoneNumber"]}
                    )
            elif "sipUri" in dialout_setting:
                logger.info(f"Dialing sipUri: {dialout_setting['sipUri']}")
                await self.transport.start_dialout(
                    {"sipUri": dialout_setting["sipUri"]}
                )

    def _register_handlers(self):
        """Register all event handlers related to dial-out functionality."""

        @self.transport.event_handler("on_dialout_connected")
        async def on_dialout_connected(transport, data):
            """Handler for when a dial-out call is connected (starts ringing)."""
            logger.debug(f"Dial-out connected: {data}")

        @self.transport.event_handler("on_dialout_answered")
        async def on_dialout_answered(transport, data):
            """Handler for when a dial-out call is answered (off hook)."""
            logger.debug(f"Dial-out answered: {data}")
            session_id = data.get("sessionId")
            await transport.capture_participant_transcription(session_id)

        @self.transport.event_handler("on_dialout_stopped")
        async def on_dialout_stopped(transport, data):
            """Handler for when a dial-out call is stopped."""
            logger.debug(f"Dial-out stopped: {data}")

        @self.transport.event_handler("on_dialout_error")
        async def on_dialout_error(transport, data):
            """Handler for dial-out errors. Will retry the call up to max_attempts."""
            logger.error(f"Dial-out error: {data}. Retrying...")
            await self.start_dialout()

        @self.transport.event_handler("on_dialout_warning")
        async def on_dialout_warning(transport, data):
            """Handler for dial-out warnings."""
            logger.warning(f"Dial-out warning: {data}")


class GeneralEventHandler:
    """Handles general call state events."""

    def __init__(
        self,
        transport,
        pipelines,
        call_flow_state=None,
        dialout_handler=None,
        using_voicemail_detection=False,
    ):
        self.transport = transport
        self.pipelines = pipelines
        self.call_flow_state = call_flow_state
        self.dialout_handler = dialout_handler
        self.using_voicemail_detection = using_voicemail_detection
        self._register_handlers()
        logger.info("GeneralEventHandler initialized.")

    def _register_handlers(self):
        @self.transport.event_handler("on_call_state_updated")
        async def on_call_state_updated(transport, state):
            """Handler for the call state update event. Normally fired if the bot joins or leaves the call."""
            logger.info(f"Call state updated: {state}")
            ## -- Call state handling -- ##
            if state == "joined" and self.dialout_handler:
                logger.info("Call state 'joined', initiating dialout.")
                await self.dialout_handler.start_dialout()
            if state == "left":
                logger.info("Call state 'left'. Ensuring all pipelines are stopped.")
                for name, pipeline_handler in self.pipelines.items():
                    if (
                        pipeline_handler and pipeline_handler._is_running
                    ):  # Check if handler exists and is running
                        logger.debug(
                            f"Stopping pipeline '{name}' due to call state left"
                        )
                        await pipeline_handler.stop()

        @self.transport.event_handler("on_joined")
        async def on_joined(transport, data):
            """Handler for when the bot joins the call."""
            session_id = data["meetingSession"]["id"]
            bot_id = data["participants"]["local"]["id"]
            logger.info(f"Bot joined call. Session ID: {session_id}, Bot ID: {bot_id}")

        @self.transport.event_handler("on_participant_left")
        async def on_participant_left(transport, participant, reason):
            """Handler for when a participant leaves the call."""
            logger.debug(f"Participant left: {participant}, reason: {reason}")
            logger.info("Participant left, stopping all active pipelines.")
            for name, pipeline_handler in self.pipelines.items():
                if (
                    pipeline_handler and pipeline_handler._is_running
                ):  # Check if handler exists and is running
                    logger.debug(f"Stopping pipeline '{name}' due to participant left")
                    await pipeline_handler.stop()


class PipelineHandler:
    """Handles the pipeline task, LLM configuration, and manages the pipeline lifecycle."""

    def __init__(
        self,
        name,
        transport,
        stt,
        llm,
        tts,
        system_prompt=None,
        tools=None,
        function_registry=None,
    ):
        self.name = name
        self.transport = transport
        self.stt = stt
        self.llm: OpenAILLMService = llm
        self.tts = tts
        self.task = None
        self.runner = None
        self.context_aggregator = None
        self.tools = tools
        self.function_registry = function_registry or {}
        self.system_prompt = system_prompt
        self._is_running = False
        logger.info(f"PipelineHandler '{self.name}' initialized.")

    def setup_llm_context(self):
        logger.debug(f"[{self.name}] Setting up LLM context.")
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        else:
            logger.warning(f"[{self.name}] No system prompt provided.")

        if self.function_registry:
            for func_name, func in self.function_registry.items():
                self.llm.register_function(func_name, func)
                logger.debug(f"[{self.name}] Registered function: {func_name}")

        context = OpenAILLMContext(messages, self.tools)
        self.context_aggregator = self.llm.create_context_aggregator(context)
        logger.debug(f"[{self.name}] LLM context aggregator created.")

        return self.context_aggregator

    def create_pipeline(self):
        logger.debug(f"[{self.name}] Creating pipeline.")

        pipeline_processors = [
            self.transport.input(),
            self.stt,
            self.context_aggregator.user(),
            self.llm,
            self.tts,
            self.transport.output(),
            self.context_aggregator.assistant(),
        ]
        pipeline = Pipeline(pipeline_processors)
        logger.debug(f"[{self.name}] Pipeline created successfully.")
        return pipeline

    def create_task(self, pipeline=None):
        logger.debug(f"[{self.name}] Creating pipeline task.")

        self.task = PipelineTask(
            pipeline,
            params=PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
        )
        logger.debug(f"[{self.name}] Pipeline task created.")
        return self.task

    async def start(self):
        logger.info(f"[{self.name}] Starting pipeline runner.")
        self.runner = PipelineRunner(handle_sigint=False, force_gc=True)
        try:
            self._is_running = True
            await self.runner.run(self.task)
            logger.info(f"[{self.name}] Pipeline runner finished.")
        except asyncio.CancelledError:
            logger.info(f"[{self.name}] Pipeline task was cancelled.")
        except Exception as e:
            logger.exception(f"[{self.name}] Error running pipeline task: {e}")
        finally:
            self._is_running = False
            self.task = None
            self.runner = None

    async def stop(self):
        if self.task and self._is_running:
            logger.info(f"[{self.name}] Stopping pipeline task.")
            try:
                await self.task.cancel()
                logger.info(f"[{self.name}] Pipeline task cancelled command issued.")
                self._is_running = False  # Mark as not running
            except Exception as e:
                logger.exception(f"[{self.name}] Error cancelling pipeline task: {e}")
        elif not self._is_running and self.task:
            logger.warning(
                f"[{self.name}] Attempted to stop a pipeline task that is not running but exists."
            )
            self.task = None  # Clean up potentially lingering task object
        elif not self.task:
            logger.warning(
                f"[{self.name}] Attempted to stop pipeline, but no task exists."
            )
        else:  # Not running, no task
            logger.debug(
                f"[{self.name}] Stop called but pipeline already stopped/cleaned up."
            )

    def get_context_aggregator(self):
        if not self.context_aggregator:
            self.setup_llm_context()
        return self.context_aggregator

    async def queue_context_frame(self):
        if not self.task or not self.context_aggregator:
            logger.error(
                f"[{self.name}] Cannot queue context frame: Task or context not ready"
            )
            return

        context_frame = self.context_aggregator.user().get_context_frame()
        if context_frame:
            await self.task.queue_frames([context_frame])
            logger.debug(f"[{self.name}] Context frame queued.")
        else:
            logger.warning(
                f"[{self.name}] Failed to get context frame from aggregator."
            )


class VoicemailDetectionFunctionHandlers:
    """Handlers for the voicemail detection bot functions."""

    def __init__(self, call_flow_state):
        self.call_flow_state = call_flow_state
        logger.info("VoicemailDetectionFunctionHandlers initialized.")

    async def voicemail_response(
        self,
        function_name,
        tool_call_id,
        args,
        llm: LLMService,
        context,
        result_callback,
    ):
        """Function the bot can call to get the voicemail message text."""
        logger.info(
            f"Function call: {function_name} triggered. Providing voicemail message text."
        )
        # This function only returns the text. The LLM that called it
        # is responsible for speaking it and then calling terminate_call.
        message = """You are Chatbot leaving a voicemail message. Say EXACTLY this message and then terminate the call:
        
                    'Hello, this is a message for Pipecat example user. This is Chatbot. Please call back on 123-456-7891. Thank you.'"""
        await result_callback(message)

    async def human_conversation(
        self,
        function_name,
        tool_call_id,
        args,
        llm: LLMService,
        context,
        result_callback,
    ):
        """Function called when bot detects it's talking to a human."""
        logger.info(f"Function call: {function_name} triggered. Human detected.")
        if self.call_flow_state:
            self.call_flow_state.set_human_detected()
            logger.debug("Session state updated: human_detected = True")
        else:
            logger.warning(
                "call flow state not available, cannot update human_detected state."
            )
        # Stop the current LLM task (voicemail detection)
        logger.debug("Sending StopTaskFrame upstream to halt VMD processing.")
        await llm.push_frame(StopTaskFrame(), FrameDirection.UPSTREAM)


async def main(room_url: str, token: str, body: dict):
    logger.debug(f"Starting bot in room: {room_url}")
    call_flow_state = CallFlowState()

    # Dial-in configuration:
    # dialin_settings are received when a call is triggered to
    # Daily via pinless_dialin. This can be a phone number on Daily or a
    # sip interconnect from Twilio or Telnyx.
    dialin_settings = None
    dialled_phonenum = None
    caller_phonenum = None
    if raw_dialin_settings := body.get("dialin_settings"):
        # these fields can capitalize the first letter
        dialled_phonenum = raw_dialin_settings.get("To") or raw_dialin_settings.get(
            "to"
        )
        caller_phonenum = raw_dialin_settings.get("From") or raw_dialin_settings.get(
            "from"
        )
        dialin_settings = {
            # these fields can be received as snake_case or camelCase.
            "call_id": raw_dialin_settings.get("callId")
            or raw_dialin_settings.get("call_id"),
            "call_domain": raw_dialin_settings.get("callDomain")
            or raw_dialin_settings.get("call_domain"),
        }
        logger.debug(
            f"Dialin settings: To: {dialled_phonenum}, From: {caller_phonenum}, dialin_settings: {dialin_settings}"
        )

    dialout_settings = body.get("dialout_settings")
    logger.debug(f"Dialout settings: {dialout_settings}")

    voicemail_detection = body.get("voicemail_detection")
    using_voicemail_detection = bool(voicemail_detection and dialout_settings)
    logger.debug(f"Using voicemail detection: {using_voicemail_detection}")

    # --- Service Initialization ---

    transport = DailyTransport(
        room_url,
        token,
        "Voice AI Bot",
        DailyParams(
            api_key=os.getenv("DAILY_API_KEY"),
            dialin_settings=dialin_settings,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            vad_audio_passthrough=True,
            transcription_enabled=True,  # Ensure transcription is enabled for STT
        ),
    )
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",
    )

    # --- Prompt, Tools, and Function Configuration ---
    system_prompt = ""
    tools = None
    functions = {}
    pipelines = {}
    handlers = {}
    handlers["call_flow_state"] = call_flow_state
    terminate_call_function = FunctionSchema(
        name="terminate_call",
        description="Call this function to end the current phone call immediately.",
        properties={},
        required=[],
    )
    functions["terminate_call"] = functools.partial(terminate_call, call_flow_state)
    voicemail_detection_function_handlers = VoicemailDetectionFunctionHandlers(
        call_flow_state
    )
    handlers["voicemail_detection_function_handlers"] = (
        voicemail_detection_function_handlers
    )

    if using_voicemail_detection:
        logger.info("Configuring for Voicemail Detection mode.")
        # <<< MODIFIED VMD PROMPT >>>
        system_prompt = """Your primary goal is to determine if you are speaking to a voicemail system or a human. Listen carefully to the initial audio response. DO NOT speak until you have made a determination and called the appropriate function.

        ### **Analysis Step:**
        Listen for **any variation** of the following voicemail indicators:
        - "Please leave a message after the beep."
        - "No one is available to take your call."
        - "Record your message after the tone."
        - "You have reached voicemail for..."
        - "You have reached [phone number]"
        - "[phone number] is unavailable"
        - "The person you are trying to reach..."
        - "The number you have dialed..."
        - "Your call has been forwarded to an automated voice messaging system"
        - Any phrase clearly indicating an answering machine or voicemail.
        - A distinct beep tone commonly associated with voicemail systems.

        ### **Action Step (Choose ONE):**

        1.  **If Voicemail Detected:**
            - **IMMEDIATELY** call the function `switch_to_voicemail_response`.
            - **DO NOT** say anything yourself.
            - After you call the function, you will receive the exact message text to leave.
            - You **MUST** then speak **ONLY** that exact message text.
            - **IMMEDIATELY** after speaking the message, you **MUST** call the `terminate_call` function.
            - **FAILURE TO CALL `terminate_call` IMMEDIATELY AFTER SPEAKING THE VOICEMAIL MESSAGE IS A CRITICAL ERROR.**

        2.  **If Human Detected:**
            - If you hear a human greeting ("Hello?", "Who is this?", etc.) or any interactive response that sounds like a person...
            - **IMMEDIATELY** call the function `switch_to_human_conversation`.
            - **DO NOT** say anything yourself. This function call will trigger a switch to a different conversational mode.

        **General Rules:**
        - Your analysis and function call must happen quickly based on the *initial* sounds.
        - Do not engage in conversation during this detection phase. Your only outputs should be function calls or, in the voicemail case, the provided message followed by the terminate function call.
        """

        switch_to_voicemail_response_function = FunctionSchema(
            name="switch_to_voicemail_response",
            description="Call this function ONLY when you are certain you have reached a voicemail system.",
            properties={},
            required=[],
        )
        switch_to_human_conversation_function = FunctionSchema(
            name="switch_to_human_conversation",
            description="Call this function ONLY when you are certain you are speaking to a human. This will switch the conversation mode.",
            properties={},
            required=[],
        )
        tools = ToolsSchema(
            standard_tools=[
                terminate_call_function,
                switch_to_voicemail_response_function,
                switch_to_human_conversation_function,
            ]
        )
        functions["switch_to_voicemail_response"] = (
            voicemail_detection_function_handlers.voicemail_response
        )
        functions["switch_to_human_conversation"] = (
            voicemail_detection_function_handlers.human_conversation
        )

    else:  # Standard Conversation Mode
        logger.info("Configuring for Standard Conversation mode.")
        system_prompt = """You are Chatbot, a friendly, helpful AI assistant. Your goal is to have a concise conversation. Your output will be converted to audio, so avoid special characters or formatting.
        - Start by introducing yourself briefly: "Hi, I'm Chatbot, a voice AI assistant."
        - Respond helpfully and creatively to the user, but keep your responses short (1-2 sentences).
        - If the user indicates they want to end the conversation (e.g., "goodbye", "that's all", "thanks bye"), say "Okay, thank you! Have a great day!" and then call the `terminate_call` function immediately."""
        tools = ToolsSchema(standard_tools=[terminate_call_function])

    # --- Main Pipeline Setup ---
    logger.info("Setting up main pipeline handler.")
    main_pipeline_handler = PipelineHandler(
        name="main",
        transport=transport,
        stt=stt,
        llm=llm,
        tts=tts,
        system_prompt=system_prompt,
        tools=tools,
        function_registry=functions,
    )
    pipelines["main"] = main_pipeline_handler
    main_task_object = main_pipeline_handler.create_task()  # Changed variable name
    if not main_task_object:
        logger.error("Failed to create main pipeline task. Exiting.")
        return

    # --- Event Handler Initialization ---
    logger.info("Initializing event handlers based on configuration.")
    context_aggregator = main_pipeline_handler.get_context_aggregator()

    if dialin_settings:
        handlers["dialin"] = DialInHandler(
            transport, main_task_object, context_aggregator
        )
    if dialout_settings:
        handlers["dialout"] = DialOutHandler(
            transport,
            main_task_object,
            dialout_settings,
        )

    handlers["general_event_handler"] = GeneralEventHandler(
        transport,
        pipelines,
        call_flow_state,
        handlers.get("dialout"),
        using_voicemail_detection=using_voicemail_detection,
    )

    # --- Start Main Pipeline ---
    logger.info("Starting the main pipeline.")
    main_pipeline_run_task = asyncio.create_task(main_pipeline_handler.start())

    # --- Post-Start Logic (Voicemail Detection Flow) ---
    if using_voicemail_detection:
        await main_pipeline_run_task

        logger.info(
            f"Main (VMD) pipeline finished. Checking session state: Human Detected={call_flow_state.human_detected}"
        )

        if call_flow_state.human_detected:
            logger.info("Human detected. Switching to human conversation pipeline.")

            human_conversation_prompt = """
            - You are Chatbot, a friendly, helpful AI assistant. Your goal is to have a concise conversation. Your output will be converted to audio, so avoid special characters or formatting.
            Follow these steps **EXACTLY**.
            Step 1: Start by introducing yourself briefly: "Hi, I'm Chatbot, a voice AI assistant."
            Step 2: If the user indicates they no longer need assistance, or no longer want to speak about the current topic, ask:
                "Would you like to end this call?"
            Step 3: If the user responds with ANY variation of 'yes', 'yeah', 'sure', 'okay', etc., you MUST IMMEDIATELY say:
                "Okay, thank you! Have a great day!"
            Step 4: Call the `terminate_call` function immediately
            """
            human_conversation_tools = ToolsSchema(
                standard_tools=[terminate_call_function]
            )
            human_conversation_functions = {
                "terminate_call": functools.partial(terminate_call, call_flow_state)
            }

            logger.info("Setting up human conversation pipeline handler.")
            human_conversation_pipeline_handler = PipelineHandler(
                name="human_conversation",
                transport=transport,
                stt=stt,
                llm=llm,
                tts=tts,
                system_prompt=human_conversation_prompt,
                tools=human_conversation_tools,
                function_registry=human_conversation_functions,
            )
            pipelines["human_conversation"] = human_conversation_pipeline_handler
            human_conversation_pipeline_handler.create_task()

            await human_conversation_pipeline_handler.queue_context_frame()
            logger.info("Starting the human conversation pipeline.")
            await human_conversation_pipeline_handler.start()
            logger.info("Human conversation pipeline finished.")

        else:  # Log why not proceeding
            logger.info("No human detected. Ending voicemail detection pipeline.")
            await main_pipeline_handler.stop()
            logger.info("Voicemail detection pipeline finished.")
    else:  # Standard conversation mode
        logger.info(
            "Standard conversation mode. Waiting for main pipeline to complete."
        )
        await main_pipeline_run_task
        logger.info("Main pipeline finished.")


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
