#!/usr/bin/env python

import asyncio
import sys
from multiprocessing import Process
from os import environ
from typing import Annotated

from fastapi import FastAPI, Header, WebSocket
from fastapi.websockets import WebSocketState
from loguru import logger
from pipecatcloud.agent import (
    DailySessionArguments,
    PipecatSessionArguments,
    SessionArguments,
    WebSocketSessionArguments,
)

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


async def run_bot(args: SessionArguments):
    # We lazy import the bot file since we might be running in a different
    # process (Daily case).
    from bot import bot

    await bot(args)


def daily_bot_process(body, x_daily_room_url, x_daily_room_token, x_daily_session_id):
    # This is a different process so we need to configure the logger again.
    logger.remove()
    logger.add(sys.stderr, format=session_logger_format)
    logger.configure(extra={"session_id": "NONE"})

    logger.bind(session_id=x_daily_session_id)

    args = DailySessionArguments(
        session_id=x_daily_session_id,
        room_url=x_daily_room_url,
        token=x_daily_room_token,
        body=body,
    )

    asyncio.run(run_bot(args))


def launch_daily_bot(body, x_daily_room_url, x_daily_room_token, x_daily_session_id):
    process = Process(
        target=daily_bot_process,
        args=(body, x_daily_room_url, x_daily_room_token, x_daily_session_id),
    )
    process.start()


@app.post("/bot")
async def handle_bot_request(
    body: dict,
    x_daily_room_url: Annotated[str | None, Header()] = None,
    x_daily_room_token: Annotated[str | None, Header()] = None,
    x_daily_session_id: Annotated[str | None, Header()] = None,
):
    if x_daily_room_url and x_daily_room_token:
        launch_daily_bot(body, x_daily_room_url, x_daily_room_token, x_daily_session_id)
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

    logger.bind(session_id=x_daily_session_id)

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
