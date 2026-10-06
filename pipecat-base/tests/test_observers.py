"""How Pipecat finds the hooks in the observers' setup file.

From 1.3.0, Pipecat offers every PIPECAT_SETUP_FILES entry to the runner as
well as to each worker, and warns about a file without the hook it looks for.
"""

import asyncio
import importlib.util

import pcc_observers
import pytest


def _pipecat_has(module):
    try:
        return importlib.util.find_spec(module) is not None
    except ImportError:
        return False


@pytest.mark.skipif(
    not _pipecat_has("pipecat.utils.startup"),
    reason="this pipecat-ai offers setup files to the pipeline task only",
)
@pytest.mark.parametrize("hook", ["setup_worker_runner", "setup_pipeline_worker"])
def test_pipecat_finds_each_hook_in_the_setup_file(monkeypatch, hook):
    from loguru import logger
    from pipecat.utils.startup import run_setup_hook

    monkeypatch.setenv("PIPECAT_SETUP_FILES", pcc_observers.__file__)
    target = _Worker()
    logged = []
    sink = logger.add(logged.append, level="WARNING", format="{message}")
    try:
        asyncio.run(run_setup_hook(target=target, function_name=hook))
    finally:
        logger.remove(sink)

    assert logged == []


class _Worker:
    """Stands in for the runner or worker a hook is handed."""

    def __init__(self):
        self.observers = []

    def add_observer(self, observer):
        self.observers.append(observer)
