from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    event_type: str
    message: str | None = None
    status: str | None = None
    thread_id: str | None = None
    tool_name: str | None = None


def parse_execution_events(lines: Iterable[str]) -> list[ExecutionEvent]:
    events: list[ExecutionEvent] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            events.append(ExecutionEvent(event_type="log", message=line))
            continue

        if not isinstance(payload, dict):
            events.append(ExecutionEvent(event_type="log", message=line))
            continue

        normalized_event_type = payload.get("event_type")
        if isinstance(normalized_event_type, str) and normalized_event_type.strip():
            events.append(
                ExecutionEvent(
                    event_type=normalized_event_type.strip(),
                    message=payload.get("message") if isinstance(payload.get("message"), str) else None,
                    status=payload.get("status") if isinstance(payload.get("status"), str) else None,
                    thread_id=payload.get("thread_id") if isinstance(payload.get("thread_id"), str) else None,
                    tool_name=payload.get("tool_name") if isinstance(payload.get("tool_name"), str) else None,
                )
            )
            continue

        event_type = str(payload.get("type", "")).strip()

        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                events.append(ExecutionEvent(event_type="text_delta", message=delta))
            continue

        if event_type == "response.output_text.done":
            text = payload.get("text")
            if isinstance(text, str) and text:
                events.append(ExecutionEvent(event_type="text_done", message=text))
            continue

        if event_type == "thread.started":
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                events.append(ExecutionEvent(event_type="session", thread_id=thread_id))
            continue

        if event_type == "agent.updated":
            status = payload.get("status")
            message = payload.get("message")
            events.append(
                ExecutionEvent(
                    event_type="status",
                    status=status if isinstance(status, str) else None,
                    message=message if isinstance(message, str) else None,
                )
            )
            continue

        if event_type == "mcp_tool_call":
            server = payload.get("server")
            tool = payload.get("tool")
            status = payload.get("status")
            if isinstance(server, str) and isinstance(tool, str):
                normalized_tool = f"{server}/{tool}"
            else:
                normalized_tool = None
            normalized_status = status if isinstance(status, str) else None
            mapped_type = "tool_result" if normalized_status == "completed" else "tool_call"
            events.append(
                ExecutionEvent(
                    event_type=mapped_type,
                    tool_name=normalized_tool,
                    status=normalized_status,
                )
            )
            continue

        if event_type == "turn.completed":
            events.append(ExecutionEvent(event_type="done"))
            continue

        events.append(ExecutionEvent(event_type="log", message=line))

    return events
