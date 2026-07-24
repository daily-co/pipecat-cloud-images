"""WaitingServer.shutdown() must honor should_exit_timeout for the tasks-wait loop.

Regression test for PCC-989. A live bot session keeps a FastAPI BackgroundTask
in server_state.tasks for the whole session, so the tasks-wait loop must give up
at should_exit_deadline. Before the fix it ignored the deadline and blocked until
Kubernetes sent SIGKILL, which looked like SIGTERM being swallowed.
"""

import asyncio
import time

from waiting_server import Config, WaitingServer


async def _noop_app(scope, receive, send):
    pass


class _StubLifespan:
    async def shutdown(self):
        pass


def _make_server(should_exit_timeout):
    config = Config(app=_noop_app, should_exit_timeout=should_exit_timeout)
    server = WaitingServer(config)
    # These attributes are normally set during serve(), which we do not run here.
    server.servers = []
    server.lifespan = _StubLifespan()
    return server


def test_tasks_wait_gives_up_at_deadline():
    server = _make_server(should_exit_timeout=0.3)
    # Simulate an in-flight bot session: a task that never finishes on its own.
    server.server_state.tasks = {object()}

    start = time.monotonic()
    asyncio.run(asyncio.wait_for(server.shutdown(), timeout=5.0))
    elapsed = time.monotonic() - start

    # The loop must exit around the 0.3s deadline, not hang forever.
    assert elapsed < 3.0
    # The task set is left as-is; the deadline is what ends the wait.
    assert server.server_state.tasks


def test_tasks_wait_returns_immediately_when_no_tasks():
    server = _make_server(should_exit_timeout=0.3)
    server.server_state.tasks = set()

    start = time.time()
    asyncio.run(asyncio.wait_for(server.shutdown(), timeout=5.0))
    elapsed = time.time() - start

    assert elapsed < 1.0
