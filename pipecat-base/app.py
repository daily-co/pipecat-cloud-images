#!/usr/bin/env python

import json
import sys
from os import environ
from typing import Annotated

from bot import bot
from fastapi import FastAPI, Header, WebSocket
from fastapi.websockets import WebSocketState
from pipecatcloud_system import app
from loguru import logger
from pipecatcloud.agent import (
    DailySessionArguments,
    PipecatSessionArguments,
    SessionArguments,
    WebSocketSessionArguments,
)
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


if __name__ == "__main__":
    try:
        server.run()
    except KeyboardInterrupt:
        pass
