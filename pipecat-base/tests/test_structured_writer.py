"""The capture and Loguru adapters share one durable, rotating JSONL writer."""

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pcc_structured_logs
import pytest
from loguru import logger


def _raw_record(line, stream="stdout"):
    return {
        "@timestamp": "2026-11-15T12:00:00+00:00",
        "stream": stream,
        "session_id": "writer-session",
        "line": line,
    }


def _write_raw(writer, line, stream="stdout"):
    writer.write_line(json.dumps(_raw_record(line, stream), ensure_ascii=False))


def _read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _retained_records(path):
    backup = Path(f"{path}.1")
    return (_read_records(backup) if backup.exists() else []) + _read_records(path)


@pytest.fixture
def writer(tmp_path, monkeypatch):
    path = tmp_path / "bot.jsonl"
    writer = pcc_structured_logs._JsonlWriter(path)
    monkeypatch.setattr(pcc_structured_logs, "_log_dir", str(tmp_path))
    monkeypatch.setattr(pcc_structured_logs, "_writer", writer)
    try:
        yield writer, path
    finally:
        writer.close()


def test_raw_only_output_rotates_and_keeps_the_newest_records(tmp_path, monkeypatch):
    monkeypatch.setattr(pcc_structured_logs, "_ROTATION_BYTES", 600)
    path = tmp_path / "bot.jsonl"
    writer = pcc_structured_logs._JsonlWriter(path)
    messages = [f"raw-{index:02d}-" + "x" * 40 for index in range(30)]
    try:
        for message in messages:
            _write_raw(writer, message)
        records = _retained_records(path)
        assert {file.name for file in tmp_path.iterdir()} == {"bot.jsonl", "bot.jsonl.1"}
        assert 1 < len(records) < len(messages)
        assert [record["line"] for record in records] == messages[-len(records) :]
        assert _read_records(path)[-1]["line"] == messages[-1]
        assert all(record["stream"] == "stdout" and "level" not in record for record in records)
    finally:
        writer.close()


def test_framework_and_raw_output_share_rotation_without_losing_records(tmp_path, monkeypatch):
    monkeypatch.setattr(pcc_structured_logs, "_ROTATION_BYTES", 1400)
    path = tmp_path / "bot.jsonl"
    writer = pcc_structured_logs._JsonlWriter(path)
    monkeypatch.setattr(pcc_structured_logs, "_log_dir", str(tmp_path))
    monkeypatch.setattr(pcc_structured_logs, "_writer", writer)
    sink_id = pcc_structured_logs.add_file_sink(logger, "INFO")
    assert sink_id is not None
    expected = []
    try:
        for index in range(6):
            raw_line = f"raw-{index}-" + "x" * 20
            app_line = f"app-{index}-" + "x" * 20
            expected.extend((raw_line, app_line))
            _write_raw(writer, raw_line, "stderr")
            logger.bind(session_id="writer-session").info(app_line)
        assert Path(f"{path}.1").exists()
        records = _retained_records(path)
        assert [record["line"] for record in records] == expected
        for index, record in enumerate(records):
            assert record["session_id"] == "writer-session"
            assert record["@timestamp"]
            if index % 2:
                assert record["stream"] == "app" and record["level"] == "INFO"
            else:
                assert record["stream"] == "stderr" and "level" not in record
    finally:
        logger.remove(sink_id)
        writer.close()


def test_removing_the_loguru_adapter_does_not_stop_capture_writes(writer):
    writer, path = writer
    sink_id = pcc_structured_logs.add_file_sink(logger, "INFO")
    assert sink_id is not None
    try:
        logger.info("framework before removal")
    finally:
        logger.remove(sink_id)
    _write_raw(writer, "capture after removal")
    assert [record["line"] for record in _read_records(path)] == [
        "framework before removal",
        "capture after removal",
    ]


def test_app_level_filter_does_not_filter_raw_output(writer):
    writer, path = writer
    sink_id = pcc_structured_logs.add_file_sink(logger, "WARNING")
    assert sink_id is not None
    try:
        logger.debug("filtered framework message")
        logger.warning("retained framework message")
        _write_raw(writer, "raw message without a level")
        records = _read_records(path)
        assert [record["line"] for record in records] == [
            "retained framework message",
            "raw message without a level",
        ]
        assert records[0]["level"] == "WARNING"
        assert "level" not in records[1]
    finally:
        logger.remove(sink_id)


def test_unicode_newlines_and_braces_remain_one_json_record(writer):
    writer, path = writer
    line = 'snowman ☃, robot 🤖, {braces}, "quotes"\nand a second line'
    _write_raw(writer, line)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert _read_records(path) == [_raw_record(line)]


def test_concurrent_framework_and_capture_producers_write_complete_records(writer):
    writer, path = writer
    producers = 6
    per_producer = 40
    start = threading.Barrier(producers)
    sink_id = pcc_structured_logs.add_file_sink(logger, "INFO")
    assert sink_id is not None

    def produce(producer):
        start.wait(timeout=10)
        for index in range(per_producer):
            line = f"producer-{producer}-record-{index}-☃"
            if producer % 2:
                logger.bind(session_id=f"session-{producer}").info(line)
            else:
                _write_raw(writer, line, "stdout" if producer % 4 == 0 else "stderr")

    try:
        with ThreadPoolExecutor(max_workers=producers) as pool:
            futures = [pool.submit(produce, producer) for producer in range(producers)]
            for future in futures:
                future.result(timeout=15)
        records = _read_records(path)
        assert len(records) == producers * per_producer
        assert {record["line"] for record in records} == {
            f"producer-{producer}-record-{index}-☃"
            for producer in range(producers)
            for index in range(per_producer)
        }
        for record in records:
            producer = int(record["line"].split("-")[1])
            if producer % 2:
                assert record["stream"] == "app"
                assert record["level"] == "INFO"
                assert record["session_id"] == f"session-{producer}"
            else:
                assert record["stream"] == ("stdout" if producer % 4 == 0 else "stderr")
                assert "level" not in record
    finally:
        logger.remove(sink_id)


def test_write_failure_reports_once_to_the_saved_console_and_disables_writes(
    writer, monkeypatch, capsys
):
    writer, path = writer
    console = io.StringIO()
    monkeypatch.setattr(pcc_structured_logs, "_console_stream", console)
    attempts = []

    def fail_write(data):
        attempts.append(data)
        raise OSError("simulated disk full")

    monkeypatch.setattr(writer.stream, "write", fail_write)
    _write_raw(writer, "first failed write")
    _write_raw(writer, "second write is disabled")
    assert len(attempts) == 1
    assert _read_records(path) == []
    diagnostic = console.getvalue()
    assert diagnostic.count("simulated disk full") == 1
    assert diagnostic.count("pcc_structured_logs") == 1
    assert capsys.readouterr().err == ""
