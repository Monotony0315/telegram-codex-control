# Changelog

## 0.3.6 - 2026-02-20
- Improved macOS launch-agent install resiliency by falling back to `launchctl load -w` when `bootstrap` intermittently fails.

## 0.3.5 - 2026-02-20
- Added `CHAT_TURN_TIMEOUT_SECONDS` (default `180`) and applied it to interactive `/chat` turns to prevent long hangs from blocking update processing.
- Updated subprocess HOME behavior to inherit launcher HOME by default and added optional `SUBPROCESS_HOME` override.
- Hardened `/chat` post-processing so session persistence failures return `Chat turn failed: ...` instead of generic internal errors.
- Added/updated tests for chat failure handling, subprocess HOME behavior, and chat timeout config parsing.

## 0.3.4 - 2026-02-20
- Added discovery and execution shortcuts for Codex capabilities:
  - `/skills [filter]`, `/skill <name> <task>`
  - `/prompts [filter]`, `/prompt <name> <task>`
- Extended confirmation flow to support `skill` and `prompt` job types.
- Added subprocess environment passthrough controls for AI/tool integrations:
  - built-in allowlist for common provider/config keys
  - `SUBPROCESS_ENV_ALLOWLIST`, `SUBPROCESS_ENV_PREFIX_ALLOWLIST`
  - `SUBPROCESS_HOME` override (default now inherits launcher HOME for better Codex session stability)
  - explicit blocklist for Telegram bot secrets
- Hardened `/chat` handling so post-response session/log persistence failures return a chat error message instead of a generic internal error.
- Added `CHAT_TURN_TIMEOUT_SECONDS` to cap interactive `/chat` latency and prevent long-running chat turns from stalling update processing.
- Added tests for new command flows and subprocess env passthrough behavior.

## 0.3.3 - 2026-02-19
- Improved chat observability by recording `assistant_len` in `chat_turn` events.
- Added explicit `chat_empty_response` event when assistant output is empty.
- Changed user-facing empty-output message to actionable guidance: retry or `/chat reset`.
- Added tests for chat-turn telemetry and empty-response handling.

## 0.3.2 - 2026-02-19
- Fixed Telegram chat `No response` issue on resumed sessions by parsing Codex JSON events of type `agent_message`.
- Added fallback extraction from direct `item.text` when message content blocks are absent.
- Added regression test for `item.completed` + `agent_message.text` parsing.

## 0.3.1 - 2026-02-19
- Added interactive chat mode for Telegram plain text messages (`/chat` path).
- Fixed non-slash input crash (`Command names must start with '/'`) by normalizing plain text to `/chat`.
- Added persisted chat sessions per `(user_id, chat_id)` in SQLite and `/chat reset` support.
- Added `TELEGRAM_INTERACTIVE_MODE` (default `true`) to control plain-text chat behavior.
- Added/expanded tests for chat routing, policy enforcement, runner chat argv parsing, and store chat session lifecycle.

## 0.3.0 - 2026-02-19
- Added webhook transport mode with Telegram `setWebhook` integration and local HTTP receiver.
- Added command policy engine (`COMMAND_POLICY_PATH`) with per-identity allow/deny controls.
- Added `/codex <raw args...>` command with confirmation flow for broad Codex CLI coverage.
- Added secure release pipeline scripts:
  - `scripts/generate-sbom.sh`
  - `scripts/sign-artifacts.sh`
  - `scripts/release-secure.sh`
- Added GitHub release workflow for tag-based artifact publishing.
- Added command policy example and expanded test coverage for webhook/policy/codex paths.

## 0.2.0 - 2026-02-19
- Added cross-platform service automation:
  - `scripts/install-service.sh`
  - `scripts/status-service.sh`
  - `scripts/uninstall-service.sh`
  - Linux `systemd --user` scripts
- Added bootstrap and release scripts:
  - `scripts/bootstrap.sh`
  - `scripts/release-build.sh`
- Generalized service labels and removed user-specific hardcoding.
- Made `WORKSPACE_ROOT` default portable (`~/Projects` if exists, else `~`).
- Expanded deployment/security/SEO documentation:
  - `docs/DEPLOYMENT.md`
  - `docs/SECURITY.md`
  - `docs/SEO.md`
- Updated package version to `0.2.0`.
