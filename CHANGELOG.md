# Changelog

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
