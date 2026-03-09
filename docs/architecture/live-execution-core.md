# Live Execution Core

`telegram-codex-control` now treats interactive execution as a live event stream instead of a single final reply.

## Runtime Contract

- Codex subprocess output is normalized into `ExecutionEvent` values.
- Supported event kinds currently include:
  - `session`
  - `status`
  - `text_delta`
  - `text_done`
  - `tool_call`
  - `tool_result`
  - `done`
  - `log`
- `/chat` creates a live placeholder message immediately, updates it with `editMessageText`, and keeps Telegram `typing` active while the turn is running.
- Job-style commands (`/run`, `/autopilot`, `/codex`, `/skill`, `/prompt`, `/report`) can attach to the same live renderer path through runner notifications.

## Telegram Rendering Rules

- Start with one placeholder `sendMessage`.
- Prefer `editMessageText` for incremental progress and final status.
- Use `sendChatAction(type=typing)` as a heartbeat while no visible text update is available.
- Keep the rendered text compact: status summary, current text fragment, or latest runner log line.

## Current Concurrency Model

- Jobs are keyed by `owner_key`, currently derived from `chat_id` in the Telegram bot.
- The daemon still enforces one active execution per owner, but different chats can now execute concurrently.
- The live renderer layer is isolated from command parsing so future per-chat actors or a Rust execution core can extend concurrency without rewriting Telegram rendering.

## Compatibility Boundary

- Existing command syntax and confirmation flows remain unchanged.
- Legacy commands that do not participate in the live path still fall back to ordinary `sendMessage`.
- If `CODEX_LIVE_CORE_COMMAND` is configured, `/chat` uses the Rust helper to normalize Codex output before Python consumes it.
- The event and renderer modules remain small so helper output and direct Codex JSONL can share the same Telegram UX behavior.
