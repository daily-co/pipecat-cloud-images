#!/usr/bin/env python

import json
import sys
from os import environ
from typing import Annotated

from bot import bot
from fastapi import BackgroundTasks, Header, HTTPException, WebSocket
from fastapi.websockets import WebSocketState
from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer
from pipecatcloud.agent import (
    DailySessionArguments,
    PipecatSessionArguments,
    SessionArguments,
    SmallWebRTCSessionArguments,
    WebSocketSessionArguments,
)
from pipecatcloud_system import app, get_whatsapp_client
from waiting_server import Config, WaitingServer

server_config = Config(
    environ.get("SHUTDOWN_TIMEOUT", 7200),
    app,
    host="0.0.0.0",
    port=int(environ.get("PORT", 8080)),
)
server = WaitingServer(server_config)

session_logger_format = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level: <8} | "
    "{name}:{function}:{line} | "
    "{extra[session_id]} - {message}"
)
logger.remove()
logger.add(sys.stderr, format=session_logger_format)
logger.configure(extra={"session_id": "NONE"})

image_version = environ.get("IMAGE_VERSION", "unknown")


async def run_bot(args: SessionArguments):
    metadata = {
        "session_id": args.session_id,
        "image_version": image_version,
    }

    with logger.contextualize(session_id=args.session_id):
        logger.info(f"Starting bot session with metadata: {json.dumps(metadata)}")
        try:
            await bot(args)
        except Exception as e:
            logger.error(f"Exception running bot(): {e}")
        logger.info(f"Stopping bot session with metadata: {json.dumps(metadata)}")


@app.post("/bot")
async def handle_bot_request(
    body: dict,
    x_daily_room_url: Annotated[str | None, Header()] = None,
    x_daily_room_token: Annotated[str | None, Header()] = None,
    x_daily_session_id: Annotated[str | None, Header()] = None,
):
    if x_daily_room_url and x_daily_room_token:
        args = DailySessionArguments(
            session_id=x_daily_session_id,
            room_url=x_daily_room_url,
            token=x_daily_room_token,
            body=body,
        )
    else:
        args = PipecatSessionArguments(
            session_id=x_daily_session_id,
            body=body,
        )

    await run_bot(args)

    return {}


@app.websocket("/ws")
async def handle_websocket(
    ws: WebSocket, x_daily_session_id: Annotated[str | None, Header()] = None
):
    await ws.accept()

    args = WebSocketSessionArguments(
        session_id=x_daily_session_id,
        websocket=ws,
    )

    await run_bot(args)

    if ws.state == WebSocketState.CONNECTED:
        await ws.close()


# ------------------------------------------------------------
# Optional: SmallWebRTC route only if pipecat is available
# ------------------------------------------------------------
try:
    from pipecat.transports.smallwebrtc.request_handler import (
        ConnectionMode,
        SmallWebRTCRequest,
        SmallWebRTCRequestHandler,
    )

    ice_servers = [
        IceServer(
            urls="stun:stun.l.google.com:19302",
        )
    ]
    small_webrtc_handler = SmallWebRTCRequestHandler(
        connection_mode=ConnectionMode.SINGLE, ice_servers=ice_servers
    )

    @app.post("/api/offer")
    async def offer(
        request: SmallWebRTCRequest,
        background_tasks: BackgroundTasks,
        x_daily_session_id: Annotated[str | None, Header()] = None,
    ):
        """Handle WebRTC offer requests via SmallWebRTCRequestHandler."""

        async def webrtc_connection_callback(connection):
            runner_args = SmallWebRTCSessionArguments(
                session_id=x_daily_session_id, webrtc_connection=connection
            )
            background_tasks.add_task(run_bot, runner_args)

        # Delegate handling to SmallWebRTCRequestHandler
        answer = await small_webrtc_handler.handle_web_request(
            request=request,
            webrtc_connection_callback=webrtc_connection_callback,
        )
        return answer

    logger.info("pipecat-ai available: WebRTC route enabled.")

except ImportError:
    ConnectionMode = None
    SmallWebRTCRequest = None
    SmallWebRTCRequestHandler = None
    logger.warning("pipecat-ai not available: WebRTC route disabled.")


# ------------------------------------------------------------
# Optional: WhatsApp routes only if pipecat is available
# ------------------------------------------------------------
try:
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    from pipecat.transports.whatsapp.api import WhatsAppWebhookRequest

    @app.post(
        "/whatsapp",
        summary="Handle WhatsApp webhook events",
        description="Processes incoming WhatsApp messages and call events",
    )
    async def whatsapp_webhook(
        body: WhatsAppWebhookRequest,
        background_tasks: BackgroundTasks,
        x_daily_session_id: Annotated[str | None, Header()] = None,
    ):
        """Handle incoming WhatsApp webhook events.

        For call events, establishes WebRTC connections and spawns bot instances
        in the background to handle real-time communication.

        Args:
            x_daily_session_id: Header parameter containing session ID
            body: Parsed WhatsApp webhook request body
            background_tasks: FastAPI background tasks manager

        Returns:
            dict: Success response with processing status

        Raises:
            HTTPException:
                400 for invalid request format or object type
                500 for internal processing errors
        """
        # Validate webhook object type
        if body.object != "whatsapp_business_account":
            logger.warning(f"Invalid webhook object type: {body.object}")
            raise HTTPException(status_code=400, detail="Invalid object type")

        logger.debug(f"Processing WhatsApp webhook: {body}")

        async def connection_callback(connection: SmallWebRTCConnection):
            runner_args = SmallWebRTCSessionArguments(
                session_id=x_daily_session_id, webrtc_connection=connection
            )
            background_tasks.add_task(run_bot, runner_args)

        try:
            # Process the webhook request
            result = await get_whatsapp_client().handle_webhook_request(body, connection_callback)
            logger.debug(f"Webhook processed successfully: {result}")
            return {"status": "success", "message": "Webhook processed successfully"}
        except ValueError as ve:
            logger.warning(f"Invalid webhook request format: {ve}")
            raise HTTPException(status_code=400, detail=f"Invalid request: {str(ve)}")
        except Exception as e:
            logger.error(f"Internal error processing webhook: {e}")
            raise HTTPException(status_code=500, detail="Internal server error processing webhook")

    logger.info("pipecat-ai available: WhatsApp route enabled.")

except ImportError:
    WhatsAppWebhookRequest = None
    WhatsAppClient = None
    logger.warning("pipecat-ai not available or using old version: WhatsApp route disabled.")


# ------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------
if __name__ == "__main__":
    try:
        server.run()
    except KeyboardInterrupt:
        pass
