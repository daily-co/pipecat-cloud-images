#!/usr/bin/env python
import asyncio
import multiprocessing

from concurrent.futures import ProcessPoolExecutor
from os import environ
from typing import Annotated, Any

from fastapi import FastAPI, Body, Header, WebSocket
from fastapi.websockets import WebSocketState

from waiting_server import WaitingServer, Config

from bot import bot


app = FastAPI()
server_config = Config(environ.get("SHUTDOWN_TIMEOUT", 7200), app, host="0.0.0.0", port=int(environ.get("PORT", 8080)))
server = WaitingServer(server_config)

executor = ProcessPoolExecutor(
    max_workers=int(environ.get("CONCURRENCY_LIMIT", 10)),
    mp_context=multiprocessing.get_context("fork")
)

@app.post("/bot")
async def handle_bot_request(body: Any = Body(None), x_daily_room_url: Annotated[str | None, Header()] = None, x_daily_room_token: Annotated[str | None, Header()] = None):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, run_default_bot, body, x_daily_room_url, x_daily_room_token)
    return {}

def run_default_bot(body, x_daily_room_url, x_daily_room_token):
    loop = asyncio.new_event_loop()
    response = loop.run_until_complete(bot(body, x_daily_room_url, x_daily_room_token))
    loop.close()
    return response

@app.websocket("/ws")
@app.websocket("/ws/twilio")
async def handle_websocket(ws: WebSocket):
    await ws.accept()
    await bot(ws)
    if ws.state == WebSocketState.CONNECTED:
        await ws.close()

if __name__ == "__main__":
    try:
        server.run()
    except KeyboardInterrupt:
        pass
