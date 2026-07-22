import sys
from pathlib import Path

# Make pipecat-base modules importable without packaging changes.
PIPECAT_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPECAT_BASE_DIR))


import pytest


@pytest.fixture(autouse=True)
def _reset_session_state():
    """The session slot's deferred-clear timer must not bleed across tests."""
    import pcc_structured_logs

    yield
    if pcc_structured_logs._clear_timer is not None:
        pcc_structured_logs._clear_timer.cancel()
        pcc_structured_logs._clear_timer = None
    pcc_structured_logs._current_session_id = None
