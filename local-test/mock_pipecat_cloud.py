import asyncio
import os
import uuid
from typing import Any, Dict

import aiohttp
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from whatsapp_request_handler import (
    WhatsAppCallEventType,
    WhatsAppRequestHandler,
    WhatsAppWebhookRequest,
)

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

# Mapping between WhatsApp call_id and session_id
whatsapp_call_sessions: Dict[str, str] = {}

# Read environment variables
LOCAL_POD_IP = os.getenv("LOCAL_POD_IP")
LOCAL_POD_PORT = os.getenv("LOCAL_POD_PORT")
DAILY_ROOM_URL = os.getenv("DAILY_SAMPLE_ROOM_URL")
DAILY_TOKEN = os.getenv("DAILY_SAMPLE_TOKEN")
# This will be the PCC public API key
WHATSAPP_WEBHOOK_VERIFICATION_TOKEN = os.getenv("WHATSAPP_WEBHOOK_VERIFICATION_TOKEN")


async def call_bot_and_store(agent_name: str, session_id: str, body: dict):
    """
    Background task to call the /bot endpoint of the local bot server
    and store session metadata in memory.
    """
    headers = {"x-daily-session-id": session_id}
    if DAILY_ROOM_URL and DAILY_TOKEN:
        headers["x-daily-room-url"] = DAILY_ROOM_URL
        headers["x-daily-room-token"] = DAILY_TOKEN

    logger.info(f"Starting bot with headers {headers}")

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
            logger.info(f"Bot with session_id {session_id} has finished executing")
            del active_sessions[session_id]


async def wait_for_bot_start(
    session_id: str, max_wait_time: int = 2, check_interval: float = 0.5
) -> bool:
    """
    Wait for a bot session to be stored in active_sessions.
    """
    waited_time = 0

    while session_id not in active_sessions and waited_time < max_wait_time:
        logger.debug("waiting for session_id to be stored in active_sessions...")
        await asyncio.sleep(check_interval)
        waited_time += check_interval

    if session_id in active_sessions:
        logger.info(f"Session {session_id} confirmed in active_sessions after {waited_time:.1f}s")
        return True
    else:
        logger.error(f"Timeout waiting for session {session_id} to be stored in active_sessions")
        return False


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

    return {"sessionId": session_id}


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


## Mocking the ice-servers endpoint which will be provided by the sidecar.
@app.get("/ice-servers")
async def get_ice_config():
    """
    Endpoint to get ice servers configuration.
    """
    ice_config_result = {
        "iceConfig": {
            "iceServers": [
                {
                    "urls": [
                        "stun:stun.cloudflare.com:3478",
                        "turn:turn.cloudflare.com:3478?transport=udp",
                        "turn:turn.cloudflare.com:3478?transport=tcp",
                        "turns:turn.cloudflare.com:5349?transport=tcp",
                    ],
                    "username": "mock",
                    "credential": "mock",
                }
            ]
        }
    }

    return ice_config_result


# ---------------- WhatsApp specific routes ----------------
@app.get(
    "/whatsapp",
    summary="Verify WhatsApp webhook",
    description="Handles WhatsApp webhook verification requests from Meta",
)
async def verify_webhook(request: Request):
    """Verify WhatsApp webhook endpoint.

    This endpoint is called by Meta's WhatsApp Business API to verify
    the webhook URL during setup. It validates the verification token
    and returns the challenge parameter if successful.
    """
    params = dict(request.query_params)
    logger.debug(f"Webhook verification request received with params: {list(params.keys())}")

    try:
        result = WhatsAppRequestHandler.handle_verify_webhook_request(
            params=params, expected_token=WHATSAPP_WEBHOOK_VERIFICATION_TOKEN
        )
        logger.info("Webhook verification successful")
        return result
    except ValueError as e:
        logger.warning(f"Webhook verification failed: {e}")
        raise HTTPException(status_code=403, detail="Verification failed")


@app.post(
    "/whatsapp",
    summary="Handle WhatsApp webhook events",
    description="Processes incoming WhatsApp messages and call events",
)
async def whatsapp_webhook(body: WhatsAppWebhookRequest, request: Request):
    """Handle incoming WhatsApp webhook events."""
    logger.debug(f"Incoming WhatsApp webhook: {body.model_dump()}")

    original_body = await request.body()

    try:
        call_event = await WhatsAppRequestHandler.handle_webhook_request(body)

        # --- Handle new call ---
        if call_event.event_type == WhatsAppCallEventType.NEW_CALL:
            session_id = str(uuid.uuid4())
            agent_name = f"whatsapp_{call_event.call_id}"

            whatsapp_call_sessions[call_event.call_id] = session_id
            logger.info(f"Mapped call_id {call_event.call_id} → session_id {session_id}")

            asyncio.create_task(call_bot_and_store(agent_name, session_id, {}))

            if not await wait_for_bot_start(session_id):
                raise RuntimeError(f"Bot startup timeout for session {session_id}")

        # --- Lookup active session ---
        session_id = whatsapp_call_sessions.get(call_event.call_id)
        if not session_id:
            raise HTTPException(status_code=404, detail="No session found for this call")

        active_session = active_sessions.get(session_id)
        if not active_session:
            raise HTTPException(status_code=404, detail="Invalid or not-yet-ready session_id")

        # --- Handle call termination ---
        if call_event.event_type == WhatsAppCallEventType.TERMINATE_CALL:
            removed = whatsapp_call_sessions.pop(call_event.call_id, None)
            if removed:
                logger.info(f"Terminated call {call_event.call_id}, cleaned up session {removed}")
            else:
                logger.warning(f"Call {call_event.call_id} not found during termination")

        # --- Forward webhook to bot ---
        target_url = (
            f"http://{active_session['pod_ip_address']}:{active_session['pod_ip_port']}/whatsapp"
        )
        logger.debug(f"Forwarding webhook to {target_url}")

        headers = dict(request.headers)
        headers["x-daily-session-id"] = session_id
        # if "content-type" not in headers:
        #    headers["content-type"] = "application/json"

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.post(target_url, headers=headers, data=original_body) as resp:
                response_body = await resp.read()
                return Response(
                    content=response_body, status_code=resp.status, headers=dict(resp.headers)
                )

    except ValueError as ve:
        logger.warning(f"Invalid webhook request: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except aiohttp.ClientError as ce:
        logger.error(f"Error forwarding to bot: {ce}")
        raise HTTPException(status_code=502, detail="Error forwarding request to bot")


# ---------------- END WhatsApp specific routes ----------------


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=7860)
