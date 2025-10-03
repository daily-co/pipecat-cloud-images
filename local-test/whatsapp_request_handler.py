#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""WhatsApp Request Handler Module.

This module handles webhook requests from WhatsApp.
"""

from enum import Enum
from typing import Dict, Optional, Union

from loguru import logger
from pydantic import BaseModel, Field


# ----------------------------
# Essential Pydantic Models for WhatsApp Event Processing
# ----------------------------
class WhatsAppSession(BaseModel):
    """WebRTC session information for WhatsApp calls."""

    sdp: str
    sdp_type: str


class WhatsAppBaseCall(BaseModel):
    """Base call model shared across WhatsApp call events."""

    id: str
    from_: str = Field(..., alias="from")
    to: str
    event: str  # "connect" | "terminate"
    timestamp: str
    direction: Optional[str] = None


class WhatsAppConnectCall(WhatsAppBaseCall):
    """Incoming call connection event data."""

    session: WhatsAppSession


class WhatsAppTerminateCall(WhatsAppBaseCall):
    """Call termination event data."""

    # Keep these only if useful, else remove
    biz_opaque_callback_data: Optional[str] = None
    status: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration: Optional[int] = None


class WhatsAppCallValue(BaseModel):
    """Webhook payload for call events."""

    calls: list[Union[WhatsAppConnectCall, WhatsAppTerminateCall]]


class WhatsAppChange(BaseModel):
    """Webhook change event wrapper."""

    value: WhatsAppCallValue
    field: str


class WhatsAppEntry(BaseModel):
    """Webhook entry containing one or more changes."""

    id: str
    changes: list[WhatsAppChange]


class WhatsAppWebhookRequest(BaseModel):
    """Complete webhook request from WhatsApp."""

    object: str
    entry: list[WhatsAppEntry]


# ----------------------------
# Handler result
# ----------------------------


class WhatsAppCallEventType(Enum):
    """Enum for WhatsApp call event types."""

    NEW_CALL = "NEW_CALL"
    TERMINATE_CALL = "TERMINATE_CALL"


class WhatsAppCallEvent(BaseModel):
    """Represents a WhatsApp call event."""

    call_id: str
    event_type: WhatsAppCallEventType


# ----------------------------
# Handler
# ----------------------------
class WhatsAppRequestHandler:
    """Static handler for WhatsApp webhook requests."""

    @staticmethod
    def handle_verify_webhook_request(params: Dict[str, str], expected_token: str) -> int:
        """Handle a verify webhook request from WhatsApp."""
        mode = params.get("hub.mode")
        challenge = params.get("hub.challenge")
        token = params.get("hub.verify_token")

        if mode != "subscribe" or not challenge or token != expected_token:
            raise ValueError("Webhook verification failed")

        return int(challenge)

    @staticmethod
    async def handle_webhook_request(request: WhatsAppWebhookRequest) -> WhatsAppCallEvent:
        """Handle a webhook request from WhatsApp."""
        for entry in request.entry:
            for change in entry.changes:
                for call in change.value.calls:
                    if call.event == "connect":
                        logger.debug(f"Processing connect event for call {call.id}")
                        return WhatsAppCallEvent(
                            call_id=call.id,
                            event_type=WhatsAppCallEventType.NEW_CALL,
                        )
                    if call.event == "terminate":
                        logger.debug(f"Processing terminate event for call {call.id}")
                        return WhatsAppCallEvent(
                            call_id=call.id,
                            event_type=WhatsAppCallEventType.TERMINATE_CALL,
                        )

        logger.warning(f"No supported event found in webhook request: {request}")
        raise ValueError("No supported event found in webhook request")
