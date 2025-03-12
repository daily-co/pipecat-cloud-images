#!/usr/bin/env python

import asyncio
import sys
from multiprocessing import Process
from os import environ
from typing import Annotated

from fastapi import FastAPI, Header, WebSocket
from fastapi.websockets import WebSocketState
from loguru import logger

from bot import bot
from waiting_server import Config, WaitingServer

app = FastAPI()
server_config = Config(
    environ.get("SHUTDOWN_TIMEOUT", 7200), app, host="0.0.0.0", port=int(environ.get("PORT", 8080))
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


@app.post("/bot")
async def handle_bot_request(
    body: dict,
    x_daily_room_url: Annotated[str | None, Header()] = None,
    x_daily_room_token: Annotated[str | None, Header()] = None,
    x_daily_session_id: Annotated[str | None, Header()] = None,
):
    launch_default_bot(body, x_daily_room_url, x_daily_room_token, x_daily_session_id)
    return {}


async def default_bot_main(body, x_daily_room_url, x_daily_room_token, x_daily_session_id):
    session_logger = logger.bind(session_id=x_daily_session_id)
    response = await bot(
        body, x_daily_room_url, x_daily_room_token, x_daily_session_id, session_logger
    )
    return response


def default_bot_process(body, x_daily_room_url, x_daily_room_token, x_daily_session_id):
    # This is a different process so we need to configure the logger again.
    logger.remove()
    logger.add(sys.stderr, format=session_logger_format)
    logger.configure(extra={"session_id": "NONE"})

    asyncio.run(default_bot_main(body, x_daily_room_url, x_daily_room_token, x_daily_session_id))


def launch_default_bot(body, x_daily_room_url, x_daily_room_token, x_daily_session_id):
    process = Process(
        target=default_bot_process,
        args=(body, x_daily_room_url, x_daily_room_token, x_daily_session_id),
    )
    process.start()


@app.websocket("/ws")
async def handle_websocket(
    ws: WebSocket, x_daily_session_id: Annotated[str | None, Header()] = None
):
    await ws.accept()
    session_logger = logger.bind(session_id=x_daily_session_id)
    await bot(ws, session_logger)
    if ws.state == WebSocketState.CONNECTED:
        await ws.close()


if __name__ == "__main__":
    try:
        server.run()
    except KeyboardInterrupt:
        pass
