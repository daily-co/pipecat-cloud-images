#!/usr/bin/env python
import asyncio
import multiprocessing

from os import environ
from typing import Annotated, Any
import sys

from fastapi import FastAPI, Body, Header, WebSocket
from fastapi.websockets import WebSocketState

from loguru import logger

from waiting_server import WaitingServer, Config

from bot import bot


app = FastAPI()
server_config = Config(environ.get("SHUTDOWN_TIMEOUT", 7200), app, host="0.0.0.0", port=int(environ.get("PORT", 8080)))
server = WaitingServer(server_config)

session_logger_format = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level: <8} | "
    "{name}:{function}:{line} | "
    "{extra[session_id]} - {message}"
)
logger.remove()
logger.add(sys.stderr, format=session_logger_format)
logger.configure(extra={"session_id":"NONE"})

@app.post("/bot")
async def handle_bot_request(body: Any = Body(None), x_daily_room_url: Annotated[str | None, Header()] = None, x_daily_room_token: Annotated[str | None, Header()] = None, x_daily_session_id: Annotated[str | None, Header()] = None, proxy_connection: Annotated[str | None, Header()] = None):
    await run_default_bot(body, x_daily_room_url, x_daily_room_token, x_daily_session_id)
    return {}

async def run_default_bot(body, x_daily_room_url, x_daily_room_token, x_daily_session_id):
    session_logger = logger.bind(session_id=x_daily_session_id)
    response = await bot(body, x_daily_room_url, x_daily_room_token, x_daily_session_id, session_logger)
    return response

@app.websocket("/ws")
async def handle_websocket(ws: WebSocket, x_daily_session_id: Annotated[str | None, Header()] = None):
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
