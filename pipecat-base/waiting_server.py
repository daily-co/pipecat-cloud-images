import uvicorn
import uvicorn.server
from typing import List, Optional
import socket
import logging
import time
import asyncio
from pathlib import Path


class Config(uvicorn.Config):
    def __init__(self, should_exit_timeout: Optional[float] = None, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.should_exit_timeout = should_exit_timeout


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

        should_exit_deadline = None
        if self.__config.should_exit_timeout is not None:
            should_exit_deadline = time.time() + self.__config.should_exit_timeout

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
                and (should_exit_deadline is None or time.time() < should_exit_deadline)
            ):
                await asyncio.sleep(0.1)

        # Wait for existing tasks to complete.
        if self.server_state.tasks and not self.force_exit:
            msg = "Waiting for background tasks to complete. (CTRL+C to force quit)"
            logger.info(msg)
            while self.server_state.tasks and not self.force_exit:
                await asyncio.sleep(0.1)

        # Send the lifespan shutdown event, and wait for application shutdown.
        if not self.force_exit:
            await self.lifespan.shutdown()
