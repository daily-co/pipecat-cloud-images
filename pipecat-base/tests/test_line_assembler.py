from pcc_structured_logs import _LineAssembler


def test_splits_complete_lines():
    a = _LineAssembler()
    assert a.feed(b"one\ntwo\n") == ["one", "two"]


def test_holds_partial_line_across_feeds():
    a = _LineAssembler()
    assert a.feed(b"par") == []
    assert a.feed(b"tial\nrest") == ["partial"]
    assert a.feed(b"\n") == ["rest"]


def test_strips_carriage_return():
    a = _LineAssembler()
    assert a.feed(b"crlf\r\n") == ["crlf"]


def test_blank_lines_are_preserved():
    a = _LineAssembler()
    assert a.feed(b"\n\n") == ["", ""]


def test_invalid_utf8_is_replaced_not_fatal():
    a = _LineAssembler()
    [line] = a.feed(b"bad \xff byte\n")
    assert "bad" in line and "byte" in line


def test_overlong_line_truncated_then_discarded_until_newline():
    a = _LineAssembler(max_line_bytes=8)
    # First flush of an overlong line: truncated marker, discard mode on.
    [head] = a.feed(b"0123456789ABCDEF")
    assert head.startswith("01234567")
    assert head.endswith("...[truncated]")
    # More of the same line: still discarded.
    assert a.feed(b"GHIJKL") == []
    # Newline ends discard mode; following line is emitted normally.
    assert a.feed(b"MN\nnext\n") == ["next"]


def test_overlong_complete_line_in_one_feed_is_hard_truncated():
    a = _LineAssembler(max_line_bytes=4)
    [line] = a.feed(b"abcdefgh\n")
    assert line == "abcd"
