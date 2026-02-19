# Security Checklist

## Access Control
- Set exact owner identity:
  - `ALLOWED_USER_ID`
  - `ALLOWED_CHAT_ID`
- Use `COMMAND_POLICY_PATH` for per-command restrictions.
- Keep at least one admin rule with `allow: ["*"]`.

## Transport Security
- Prefer webhook with:
  - HTTPS public URL
  - `TELEGRAM_WEBHOOK_SECRET_TOKEN`
- Do not run webhook mode without secret token validation.
- For polling mode, ensure outbound-only networking policy is acceptable.

## Runtime Safety
- `/run`, `/autopilot`, `/codex` require nonce confirmation.
- Execution uses argv-only subprocesses (no shell interpolation).
- Constrain `WORKSPACE_ROOT` to the minimum directory scope.
- Use absolute `CODEX_COMMAND` path in service environments.

## Secret Management
- Never commit `.env`.
- Rotate `TELEGRAM_BOT_TOKEN` if exposed.
- Do not embed long-lived signing keys in repository.
- Use CI secrets for release signing key material.

## Logging and Audit
- Monitor `.data/audit.jsonl` and `/logs` output.
- Investigate repeated:
  - `auth_denied`
  - `command_policy_denied`
  - `poll_error`
  - `webhook_error`

## Release Integrity
- Generate SBOM (`scripts/generate-sbom.sh`).
- Sign artifacts (`scripts/sign-artifacts.sh`).
- Publish checksums (`SHA256SUMS.txt`) with each release.
- CI tag release enforces signature generation with `RELEASE_PRIVATE_KEY_PEM`.

## Incident Response
1. Revoke Telegram token (BotFather).
2. Rotate external API keys used by Codex jobs.
3. Update policy to lock down dangerous commands.
4. Redeploy service and verify with `/status`, `/logs`.
