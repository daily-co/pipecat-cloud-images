"""Without PCC_LOG_DIR the module must be inert — cloud behavior unchanged."""

import pcc_structured_logs
from loguru import logger


def test_everything_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(pcc_structured_logs, "_log_dir", None)
    assert pcc_structured_logs.enabled() is False
    assert pcc_structured_logs.console_stream() is None
    assert pcc_structured_logs.add_file_sink(logger, "DEBUG") is None
    before = pcc_structured_logs._installed
    pcc_structured_logs.install()  # must not capture, must not touch loguru
    assert pcc_structured_logs._installed == before is False
