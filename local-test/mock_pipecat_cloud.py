import os
import uuid
from typing import Any, Dict

import aiohttp
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

# Load environment variables from a .env file
load_dotenv()

app = FastAPI()

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store of active sessions: session_id -> session info
active_sessions: Dict[str, Dict[str, Any]] = {}

# Read environment variables
LOCAL_POD_IP = os.getenv("LOCAL_POD_IP")
LOCAL_POD_PORT = os.getenv("LOCAL_POD_PORT")
DAILY_ROOM_URL = os.getenv("DAILY_SAMPLE_ROOM_URL")
DAILY_TOKEN = os.getenv("DAILY_SAMPLE_TOKEN")


async def call_bot_and_store(agent_name: str, session_id: str, body: dict):
    """
    Background task to call the /bot endpoint of the local bot server
    and store session metadata in memory.
    """
    headers = {"x-daily-session-id": session_id}
    if DAILY_ROOM_URL and DAILY_TOKEN:
        headers["x-daily-room-url"] = DAILY_ROOM_URL
        headers["x-daily-room-token"] = DAILY_TOKEN

    print(f"Starting bot with headers {headers}")

    # Store session info immediately in memory
    active_sessions[session_id] = {
        "agent_name": agent_name,
        "pod_ip_address": LOCAL_POD_IP,
        "pod_ip_port": LOCAL_POD_PORT,
    }

    target_host = active_sessions[session_id]["pod_ip_address"]
    target_port = active_sessions[session_id]["pod_ip_port"]
    bot_url = f"http://{target_host}:{target_port}/bot"

    # Long-running request to local bot server
    timeout = aiohttp.ClientTimeout(total=7200)  # 2 hours
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(bot_url, headers=headers, json=body) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=500, detail="Failed to start bot")
            await resp.json()
            print(f"Bot with session_id {session_id} has finished executing")
            del active_sessions[session_id]


@app.post("/v1/public/{agent_name}/start")
async def start_agent(agent_name: str, request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint to start a new agent session.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}  # Default to empty dict if request body is not JSON

    session_id = str(uuid.uuid4())

    # Schedule bot execution in the background
    background_tasks.add_task(call_bot_and_store, agent_name, session_id, body)

    return {"session_id": session_id}


@app.api_route(
    "/v1/public/{agent_name}/sessions/{session_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def proxy_request(agent_name: str, session_id: str, path: str, request: Request):
    """
    Proxy all requests for a specific agent session to the corresponding bot instance.
    """
    active_session = active_sessions.get(session_id)
    if not active_session:
        return Response(content="Invalid or not-yet-ready session_id", status_code=404)

    target_url = f"http://{active_session['pod_ip_address']}:{active_session['pod_ip_port']}/{path}"

    headers = dict(request.headers)
    headers["x-daily-session-id"] = session_id
    body = await request.body()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.request(
                request.method,
                target_url,
                headers=headers,
                data=body if body else None,
        ) as resp:
            response_body = await resp.read()
            return Response(
                content=response_body,
                status_code=resp.status,
                headers=dict(resp.headers),
            )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
