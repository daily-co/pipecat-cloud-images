from contextlib import asynccontextmanager
from os import environ
from typing import Optional

import aiohttp
from fastapi import FastAPI
from loguru import logger

# Try to import WhatsApp components
WhatsAppClient = None
try:
    from pipecat.transports.smallwebrtc.connection import IceServer
    from pipecat.transports.whatsapp.client import WhatsAppClient

    WHATSAPP_AVAILABLE = True
    logger.info("pipecat-ai WhatsApp components available")
except ImportError:
    WHATSAPP_AVAILABLE = False
    logger.warning("pipecat-ai WhatsApp components not available")

WHATSAPP_TOKEN = environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = environ.get("WHATSAPP_PHONE_NUMBER_ID")

# Global WhatsApp client instance
whatsapp_client: Optional[WhatsAppClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan and WhatsApp client resources."""
    global whatsapp_client
    async with aiohttp.ClientSession() as session:
        # Initialize WhatsApp client if available and credentials are provided
        if WHATSAPP_AVAILABLE and WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID:
            ice_servers = [
                IceServer(
                    urls="stun:stun.l.google.com:19302",
                )
            ]
            whatsapp_client = WhatsAppClient(
                whatsapp_token=WHATSAPP_TOKEN,
                phone_number_id=WHATSAPP_PHONE_NUMBER_ID,
                session=session,
                ice_servers=ice_servers,
            )
            logger.info("WhatsApp client initialized successfully")
        elif WHATSAPP_AVAILABLE:
            logger.warning("WhatsApp credentials not found - WhatsApp client not initialized")
        else:
            logger.info("WhatsApp not available - skipping WhatsApp client initialization")

        try:
            yield  # Run the application
        finally:
            # Cleanup WhatsApp client resources
            if whatsapp_client:
                logger.info("Cleaning up WhatsApp client resources...")
                try:
                    await whatsapp_client.terminate_all_calls()
                    logger.info("WhatsApp client cleanup completed")
                except Exception as e:
                    logger.error(f"Error during WhatsApp client cleanup: {e}")


def get_whatsapp_client() -> Optional[WhatsAppClient]:
    """Get the current WhatsApp client instance."""
    return whatsapp_client


# Always create FastAPI app with lifespan
app = FastAPI(lifespan=lifespan)
