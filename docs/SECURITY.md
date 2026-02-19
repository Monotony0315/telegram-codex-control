# Security Checklist

## Required
- Keep `.env` out of version control.
- Use a dedicated Telegram bot for this service.
- Restrict access with exact `ALLOWED_USER_ID` and `ALLOWED_CHAT_ID`.
- Set `WORKSPACE_ROOT` to the minimum required path.
- Use absolute `CODEX_COMMAND` path in production.

## Recommended Hardening
- Run on a dedicated user account.
- Use OS firewall rules to limit outbound network where possible.
- Rotate `TELEGRAM_BOT_TOKEN` after suspected exposure.
- Review `logs/` and `.data/audit.jsonl` regularly.
- Keep Python dependencies and Codex CLI updated.

## Runtime Behavior
- Command execution is argv-only (`create_subprocess_exec`) and never shell interpolation.
- `/run` and `/autopilot` require explicit nonce confirmation.
- Secret-like strings are redacted in outbound text and persisted logs.

## Incident Response
1. Revoke Telegram bot token in @BotFather.
2. Update `.env` and restart service.
3. Verify no unauthorized `auth_denied`/`command_received` events in DB logs.
4. Rotate any external API keys used by jobs.
