from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

app = FastAPI()


def add_lifespan_to_app(new_lifespan):
    """Add a new lifespan context manager to the app, combining with existing if present.

    Args:
        new_lifespan: The new lifespan context manager to add
    """
    logger.info("Adding lifespan to app")
    if hasattr(app.router, "lifespan_context") and app.router.lifespan_context is not None:
        # If there's already a lifespan context, combine them
        existing_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def combined_lifespan(app: FastAPI):
            async with existing_lifespan(app):
                async with new_lifespan(app):
                    yield

        app.router.lifespan_context = combined_lifespan
    else:
        # No existing lifespan, use the new one
        app.router.lifespan_context = new_lifespan
