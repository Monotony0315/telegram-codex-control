from __future__ import annotations

from telegram_codex_control.live_events import ExecutionEvent, parse_execution_events


def test_parse_execution_events_emits_text_delta_and_done() -> None:
    events = parse_execution_events(
        [
            '{"type":"response.output_text.delta","delta":"Hello"}',
            '{"type":"response.output_text.delta","delta":" world"}',
            '{"type":"response.output_text.done","text":"Hello world"}',
        ]
    )

    assert events == [
        ExecutionEvent(event_type="text_delta", message="Hello"),
        ExecutionEvent(event_type="text_delta", message=" world"),
        ExecutionEvent(event_type="text_done", message="Hello world"),
    ]


def test_parse_execution_events_emits_tool_and_session_events() -> None:
    events = parse_execution_events(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"agent.updated","status":"running","message":"Thinking"}',
            '{"type":"mcp_tool_call","server":"filesystem","tool":"read_file","status":"started"}',
            '{"type":"mcp_tool_call","server":"filesystem","tool":"read_file","status":"completed"}',
        ]
    )

    assert events == [
        ExecutionEvent(event_type="session", thread_id="thread-123"),
        ExecutionEvent(event_type="status", status="running", message="Thinking"),
        ExecutionEvent(event_type="tool_call", tool_name="filesystem/read_file", status="started"),
        ExecutionEvent(event_type="tool_result", tool_name="filesystem/read_file", status="completed"),
    ]


def test_parse_execution_events_preserves_raw_log_lines() -> None:
    events = parse_execution_events(["plain line", '{"type":"turn.completed"}'])

    assert events == [
        ExecutionEvent(event_type="log", message="plain line"),
        ExecutionEvent(event_type="done"),
    ]


def test_parse_execution_events_accepts_normalized_event_payloads() -> None:
    events = parse_execution_events(
        [
            '{"event_type":"session","thread_id":"thread-live"}',
            '{"event_type":"status","status":"running","message":"Thinking"}',
            '{"event_type":"text_delta","message":"Hello"}',
            '{"event_type":"done"}',
        ]
    )

    assert events == [
        ExecutionEvent(event_type="session", thread_id="thread-live"),
        ExecutionEvent(event_type="status", status="running", message="Thinking"),
        ExecutionEvent(event_type="text_delta", message="Hello"),
        ExecutionEvent(event_type="done"),
    ]
