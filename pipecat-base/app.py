#!/usr/bin/env python

import base64
import json
import sys
from os import environ
from typing import Annotated, List, Optional

import aiohttp
from bot import bot
from fastapi import BackgroundTasks, Header, HTTPException, Query, WebSocket
from fastapi.websockets import WebSocketState
from loguru import logger
from pipecatcloud.agent import (
    DailySessionArguments,
    PipecatSessionArguments,
    SessionArguments,
    WebSocketSessionArguments,
)
from pipecatcloud_system import app
from waiting_server import Config, WaitingServer

try:
    from pipecatcloud.agent import SmallWebRTCSessionArguments

    SMALL_WEBRTC_AVAILABLE = True
    logger.info("SmallWebRTCSessionArguments available.")
except ImportError:
    SMALL_WEBRTC_AVAILABLE = False
    logger.warning("SmallWebRTCSessionArguments not available. Requires pipecatcloud>=0.2.5.")

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
    ws: WebSocket,
    x_daily_session_id: Annotated[str | None, Header()] = None,
    body: str = Query(None),
):
    await ws.accept()

    decoded_body = None
    if body:
        try:
            # Decode base64 and parse as JSON to a dict
            decoded_bytes = base64.b64decode(body)
            decoded_string = decoded_bytes.decode("utf-8")
            decoded_body = json.loads(decoded_string)
        except (base64.binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.error(f"Failed to decode body parameter: {e}")

    args = WebSocketSessionArguments(
        session_id=x_daily_session_id,
        websocket=ws,
        body=decoded_body,
    )

    await run_bot(args)

    if ws.state == WebSocketState.CONNECTED:
        await ws.close()


# ------------------------------------------------------------
# Optional: SmallWebRTC route only if pipecat is available
# ------------------------------------------------------------
if SMALL_WEBRTC_AVAILABLE:
    try:
        from pipecat.transports.smallwebrtc.connection import IceServer
        from pipecat.transports.smallwebrtc.request_handler import (
            ConnectionMode,
            SmallWebRTCRequest,
            SmallWebRTCRequestHandler,
        )

        ESP32_ENABLED = environ.get("ESP32_ENABLED", "False").lower() == "true"
        ESP32_HOST = environ.get("ESP32_HOST", None)
        ICE_CONFIG_URL = environ.get("ICE_CONFIG_URL", "http://localhost:9090/ice-servers")

        logger.debug(f"ESP32_ENABLED: {ESP32_ENABLED}")
        small_webrtc_handler = SmallWebRTCRequestHandler(
            connection_mode=ConnectionMode.SINGLE,
            esp32_mode=ESP32_ENABLED,
            host=ESP32_HOST,
        )

        async def get_ice_config() -> Optional[List[IceServer]]:
            """
            Retrieves ICE configuration from the configured endpoint.

            Returns:
                Optional[List[IceServer]]: Optional list containing ice_servers
            """

            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as session:
                    async with session.request(
                        "GET",
                        ICE_CONFIG_URL,
                        headers=None,
                        data=None,
                    ) as resp:
                        if resp.status != 200:
                            raise HTTPException(
                                status_code=500, detail="Failed to fetch ICE configuration."
                            )

                        response_body = await resp.read()
                        data = json.loads(response_body.decode("utf-8"))

                        ice_config_data = data.get("iceConfig", {})
                        ice_servers_data = ice_config_data.get("iceServers", [])

                        ice_servers = []
                        for server_data in ice_servers_data:
                            ice_server = IceServer(
                                urls=server_data.get("urls", []),
                                username=server_data.get("username", ""),
                                credential=server_data.get("credential", ""),
                            )
                            ice_servers.append(ice_server)

                        return ice_servers

            except Exception as e:
                logger.error(f"Failed to fetch ICE configuration from {ICE_CONFIG_URL}: {e}")
                return [
                    IceServer(
                        urls="stun:stun.l.google.com:19302",
                    )
                ]

        @app.post("/api/offer")
        async def offer(
            request: SmallWebRTCRequest,
            background_tasks: BackgroundTasks,
            x_daily_session_id: Annotated[str | None, Header()] = None,
        ):
            """Handle WebRTC offer requests via SmallWebRTCRequestHandler."""
            # Updating the ice servers
            ice_servers = await get_ice_config()
            small_webrtc_handler._ice_servers = ice_servers

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
        IceServer = None
        logger.warning("pipecat-ai not available: WebRTC route disabled.")


# ------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------
if __name__ == "__main__":
    try:
        server.run()
    except KeyboardInterrupt:
        pass
