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


@pytest.fixture
def retry_environment(monkeypatch):
    clock = [100.0]
    console = io.StringIO()
    monkeypatch.setattr(pcc_structured_logs.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pcc_structured_logs, "_WRITE_RETRY_SECONDS", 5.0)
    monkeypatch.setattr(pcc_structured_logs, "_console_stream", console)
    return clock, console


def test_write_failure_waits_then_reopens_and_recovers_app_and_capture_output(
    writer, retry_environment, monkeypatch, capsys
):
    writer, path = writer
    clock, console = retry_environment
    failed_stream = writer.stream
    attempts = []
    opens = []
    open_file = writer._open

    def fail_write(data):
        attempts.append(data)
        raise OSError("simulated disk full")

    def open_again():
        opens.append(clock[0])
        return open_file()

    monkeypatch.setattr(failed_stream, "write", fail_write)
    monkeypatch.setattr(writer, "_open", open_again)
    _write_raw(writer, "first failed write")
    assert console.getvalue().count("simulated disk full") == 1
    assert len(console.getvalue().splitlines()) == 1

    clock[0] = 104.999
    _write_raw(writer, "capture dropped during cooldown")
    assert len(attempts) == 1
    assert opens == []
    assert _read_records(path) == []
    assert len(console.getvalue().splitlines()) == 1

    # Retry the next arriving record at the deadline, with no timer or replay.
    clock[0] = 105.0
    sink_id = pcc_structured_logs.add_file_sink(logger, "INFO")
    assert sink_id is not None
    try:
        logger.bind(session_id="recovered-session").info("framework after recovery")
        assert opens == [105.0]
        assert writer.stream is not failed_stream
        assert failed_stream.closed
        assert len(console.getvalue().splitlines()) == 2
        assert all("pcc_structured_logs" in line for line in console.getvalue().splitlines())
        recovered_diagnostic = console.getvalue()
        _write_raw(writer, "capture after recovery", "stderr")
        assert console.getvalue() == recovered_diagnostic
        records = _read_records(path)  # succeeds without close()/manual flush()
        assert [record["line"] for record in records] == [
            "framework after recovery",
            "capture after recovery",
        ]
        assert records[0]["stream"] == "app"
        assert records[0]["level"] == "INFO"
        assert records[0]["session_id"] == "recovered-session"
        assert records[1] == _raw_record("capture after recovery", "stderr")
    finally:
        logger.remove(sink_id)
    # The framework logger's ordinary console handler is separate from writer
    # diagnostics: each diagnostic must go only to the saved real console.
    assert "pcc_structured_logs:" not in capsys.readouterr().err


@pytest.mark.parametrize("failure_stage", ["open", "write"])
def test_retry_failure_reschedules_cooldown_until_the_next_successful_record(
    writer, retry_environment, monkeypatch, capsys, failure_stage
):
    writer, path = writer
    clock, console = retry_environment
    opens = []
    open_file = writer._open

    def fail_write(data):
        raise OSError("initial write failed")

    def retry_write_failure(data):
        raise OSError("simulated retry failure")

    def fail_retry_twice():
        opens.append(clock[0])
        if len(opens) <= 2 and failure_stage == "open":
            raise OSError("simulated retry failure")
        stream = open_file()
        if len(opens) <= 2:
            monkeypatch.setattr(stream, "write", retry_write_failure)
        return stream

    monkeypatch.setattr(writer.stream, "write", fail_write)
    monkeypatch.setattr(writer, "_open", fail_retry_twice)
    _write_raw(writer, "initial dropped record")
    assert len(console.getvalue().splitlines()) == 1

    for attempt, deadline in enumerate((105.0, 110.0), start=1):
        clock[0] = deadline - 0.001
        _write_raw(writer, "dropped before retry deadline")
        assert len(opens) == attempt - 1
        assert len(console.getvalue().splitlines()) == attempt
        clock[0] = deadline
        _write_raw(writer, "dropped on failed retry")
        assert opens == [105.0, 110.0][:attempt]
        assert console.getvalue().count("simulated retry failure") == attempt
        assert len(console.getvalue().splitlines()) == attempt + 1

    assert _read_records(path) == []
    clock[0] = 114.999
    _write_raw(writer, "last cooldown record is dropped")
    assert opens == [105.0, 110.0]
    clock[0] = 115.0
    _write_raw(writer, "first record after storage recovery")
    assert opens == [105.0, 110.0, 115.0]
    assert _read_records(path) == [_raw_record("first record after storage recovery")]
    assert len(console.getvalue().splitlines()) == 4
    diagnostic = console.getvalue()
    _write_raw(writer, "normal output resumes")
    assert console.getvalue() == diagnostic
    assert [record["line"] for record in _read_records(path)] == [
        "first record after storage recovery",
        "normal output resumes",
    ]
    assert capsys.readouterr().err == ""


def test_retry_flush_failure_reschedules_before_reporting_recovery(
    writer, retry_environment, monkeypatch, capsys
):
    writer, path = writer
    clock, console = retry_environment
    open_file = writer._open
    opens = []

    def fail_write(data):
        raise OSError("initial write failure")

    def fail_flush_on_first_reopen():
        opens.append(clock[0])
        stream = open_file()
        if len(opens) == 1:
            flush_to_file = stream.flush
            write_to_file = stream.write
            wrote_record = []

            def persist_write(data):
                result = write_to_file(data)
                wrote_record.append(data)
                return result

            def persist_then_fail_flush():
                flush_to_file()
                # tell()/seek() may flush before the record is written as part
                # of the rotation check. Fail the actual post-write flush.
                if wrote_record:
                    raise OSError("simulated flush failure")

            monkeypatch.setattr(stream, "write", persist_write)
            monkeypatch.setattr(stream, "flush", persist_then_fail_flush)
        return stream

    monkeypatch.setattr(writer.stream, "write", fail_write)
    monkeypatch.setattr(writer, "_open", fail_flush_on_first_reopen)
    _write_raw(writer, "dropped initial record")
    clock[0] = 105.0
    _write_raw(writer, "bytes persisted before flush reported failure")
    assert opens == [105.0]
    assert console.getvalue().count("simulated flush failure") == 1
    assert len(console.getvalue().splitlines()) == 2  # no recovery yet
    clock[0] = 109.999
    _write_raw(writer, "dropped after flush failure")
    assert opens == [105.0]
    assert len(console.getvalue().splitlines()) == 2
    clock[0] = 110.0
    _write_raw(writer, "first successfully flushed retry")
    assert opens == [105.0, 110.0]
    assert len(console.getvalue().splitlines()) == 3
    assert [record["line"] for record in _read_records(path)] == [
        "bytes persisted before flush reported failure",
        "first successfully flushed retry",
    ]
    assert capsys.readouterr().err == ""


def test_rotation_failure_recovers_with_valid_retained_and_active_files(
    tmp_path, retry_environment, monkeypatch, capsys
):
    clock, console = retry_environment
    monkeypatch.setattr(pcc_structured_logs, "_ROTATION_BYTES", 300)
    path = tmp_path / "bot.jsonl"
    writer = pcc_structured_logs._JsonlWriter(path)
    rollover = writer.doRollover
    attempts = []

    def fail_first_rotation():
        attempts.append(clock[0])
        if len(attempts) == 1:
            raise OSError("simulated rename failure")
        rollover()

    monkeypatch.setattr(writer, "doRollover", fail_first_rotation)
    try:
        before = "retained before rotation-" + "x" * 50
        after = "written after retry-" + "x" * 50
        _write_raw(writer, before)
        _write_raw(writer, "dropped failed rotation-" + "x" * 50)
        assert attempts == [100.0]
        assert _read_records(path) == [_raw_record(before)]
        assert console.getvalue().count("simulated rename failure") == 1
        clock[0] = 104.999
        _write_raw(writer, "dropped rotation cooldown")
        assert attempts == [100.0]
        clock[0] = 105.0
        _write_raw(writer, after)
        assert attempts == [100.0, 105.0]
        assert _read_records(Path(f"{path}.1")) == [_raw_record(before)]
        assert _read_records(path) == [_raw_record(after)]
        assert len(console.getvalue().splitlines()) == 2
        assert capsys.readouterr().err == ""
    finally:
        writer.close()


@pytest.mark.parametrize("partial", [True, False], ids=["partial-line", "complete-line"])
def test_retry_separates_a_damaged_tail_without_replaying_or_overwriting_it(
    writer, retry_environment, monkeypatch, capsys, partial
):
    writer, path = writer
    clock, console = retry_environment
    failed_stream = writer.stream
    write_to_file = failed_stream.write
    written = []

    def persist_then_fail(data):
        prefix = data[: len(data) // 2] if partial else data
        write_to_file(prefix)
        failed_stream.flush()
        written.append(prefix.encode())
        raise OSError("simulated failure after writing bytes")

    monkeypatch.setattr(failed_stream, "write", persist_then_fail)
    _write_raw(writer, "record interrupted by the storage error")
    assert len(written) == 1
    assert path.read_bytes() == written[0]
    clock[0] = 104.999
    _write_raw(writer, "dropped during the cooldown")
    assert path.read_bytes() == written[0]
    clock[0] = 105.0
    recovered = _raw_record("first valid record after recovery")
    _write_raw(writer, recovered["line"])
    boundary = b"\n" if partial else b""
    expected = written[0] + boundary + (json.dumps(recovered, ensure_ascii=False) + "\n").encode()
    assert path.read_bytes() == expected
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1]) == recovered
    assert len(console.getvalue().splitlines()) == 2
    assert capsys.readouterr().err == ""


def test_retry_continues_when_the_errored_stream_also_fails_to_close(
    writer, retry_environment, monkeypatch, capsys
):
    writer, path = writer
    clock, console = retry_environment
    failed_stream = writer.stream
    close_stream = failed_stream.close
    close_attempts = []

    def fail_write(data):
        raise OSError("simulated write failure")

    def fail_close():
        close_attempts.append(clock[0])
        raise OSError("simulated close failure")

    monkeypatch.setattr(failed_stream, "write", fail_write)
    monkeypatch.setattr(failed_stream, "close", fail_close)
    try:
        _write_raw(writer, "dropped before stream reset")
        clock[0] = 105.0
        _write_raw(writer, "written through a fresh stream")
        assert len(close_attempts) == 1
        assert writer.stream is not failed_stream
        assert _read_records(path) == [_raw_record("written through a fresh stream")]
        assert len(console.getvalue().splitlines()) == 2
        assert capsys.readouterr().err == ""
    finally:
        monkeypatch.setattr(failed_stream, "close", close_stream)
        close_stream()
