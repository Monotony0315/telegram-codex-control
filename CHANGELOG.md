# Changelog

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
