import asyncio
import logging
import socket
import time
from pathlib import Path
from typing import List, Optional

import uvicorn
import uvicorn.server


class Config(uvicorn.Config):
    def __init__(self, should_exit_timeout: Optional[float] = None, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Coerce to float: this often comes from an env var (a string), and the
        # deadline math (time.monotonic() + should_exit_timeout) needs a number.
        self.should_exit_timeout = (
            None if should_exit_timeout is None else float(should_exit_timeout)
        )


class WaitingServer(uvicorn.server.Server):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.__config = config

    def shutdown_sidecar(self):
        logger = logging.getLogger("uvicorn.error")
        try:
            if Path("/var/run/token-refresher").is_dir():
                logger.info("Shutting down token refresher")
                Path("/var/run/token-refresher/shutdown").touch()
        except Exception as e:
            logger.error("Error adding token refresher shutdown file: %s\n" % e)

    async def shutdown(self, sockets: Optional[List[socket.socket]] = None) -> None:
        logger = logging.getLogger("uvicorn.error")
        self.shutdown_sidecar()

        # Use a monotonic clock so the deadline is not skewed by wall-clock
        # (NTP) adjustments while shutdown is in progress.
        should_exit_deadline = None
        if self.__config.should_exit_timeout is not None:
            should_exit_deadline = time.monotonic() + self.__config.should_exit_timeout

        # Stop accepting new connections.
        for server in self.servers:
            server.close()
        for sock in sockets or []:
            sock.close()
        for server in self.servers:
            await server.wait_closed()

        # Wait for existing connections to finish sending responses.
        if self.server_state.connections and not self.force_exit:
            msg = "Waiting for connections to close. (CTRL+C to force quit)"
            logger.info(msg)
            while (
                self.server_state.connections
                and not self.force_exit
                and (should_exit_deadline is None or time.monotonic() < should_exit_deadline)
            ):
                await asyncio.sleep(0.1)

        # Wait for existing tasks to complete.
        if self.server_state.tasks and not self.force_exit:
            msg = "Waiting for background tasks to complete. (CTRL+C to force quit)"
            logger.info(msg)
            while (
                self.server_state.tasks
                and not self.force_exit
                and (should_exit_deadline is None or time.monotonic() < should_exit_deadline)
            ):
                await asyncio.sleep(0.1)

            # The loop above ends when tasks drain, force_exit is set, or the
            # deadline passes. If tasks remain and we did not force quit, the
            # deadline is the only reason we stopped waiting.
            if self.server_state.tasks and not self.force_exit:
                logger.warning(
                    "Shutdown timeout reached with %d background task(s) still running; continuing shutdown.",
                    len(self.server_state.tasks),
                )

        # Send the lifespan shutdown event, and wait for application shutdown.
        # Bound this by the same deadline so a stuck app shutdown handler cannot
        # push total shutdown past SHUTDOWN_TIMEOUT and get the pod SIGKILLed.
        if not self.force_exit:
            if should_exit_deadline is None:
                await self.lifespan.shutdown()
            else:
                remaining = should_exit_deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Shutdown timeout reached before application shutdown; skipping lifespan shutdown."
                    )
                else:
                    try:
                        await asyncio.wait_for(self.lifespan.shutdown(), timeout=remaining)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Application shutdown did not complete within the shutdown timeout; continuing."
                        )
