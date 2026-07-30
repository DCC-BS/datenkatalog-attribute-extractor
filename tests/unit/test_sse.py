"""Tests for server-sent event parsing and formatting."""

import json

from datenkatalog_attribute_extractor.utils.sse import format_sse, parse_sse


def test_parse_sse_reads_a_single_event() -> None:
    lines = ["event: progress", 'data: {"page": 1, "total_pages": 7}', ""]

    assert list(parse_sse(lines)) == [("progress", {"page": 1, "total_pages": 7})]


def test_parse_sse_reads_consecutive_events() -> None:
    lines = [
        "event: progress",
        'data: {"page": 1}',
        "",
        "event: progress",
        'data: {"page": 2}',
        "",
        "event: result",
        'data: {"fields": []}',
        "",
    ]

    assert list(parse_sse(lines)) == [
        ("progress", {"page": 1}),
        ("progress", {"page": 2}),
        ("result", {"fields": []}),
    ]


def test_parse_sse_yields_a_trailing_event_without_a_final_blank_line() -> None:
    lines = ["event: result", 'data: {"ok": true}']

    assert list(parse_sse(lines)) == [("result", {"ok": True})]


def test_parse_sse_joins_multiline_data_payloads() -> None:
    lines = ["event: result", 'data: {"a":', "data: 1}", ""]

    assert list(parse_sse(lines)) == [("result", {"a": 1})]


def test_parse_sse_defaults_the_event_name_when_absent() -> None:
    assert list(parse_sse(['data: {"x": 1}', ""])) == [("message", {"x": 1})]


def test_parse_sse_with_no_data_yields_nothing() -> None:
    assert list(parse_sse(["", "", ":comment"])) == []


def test_format_sse_round_trips_through_the_parser() -> None:
    payload = {"page": 3, "total_pages": 7, "fields_found": 12}
    frame = format_sse("progress", json.dumps(payload))

    assert list(parse_sse(frame.split("\n"))) == [("progress", payload)]
