from __future__ import annotations

import json

from telegram_codex_control.command_policy import CommandPolicy


def test_default_policy_allows_owner_all_commands() -> None:
    policy = CommandPolicy.from_path(owner_user_id=1, owner_chat_id=2, policy_path=None)
    assert policy.is_allowed(user_id=1, chat_id=2, command="/status") is True
    assert policy.is_allowed(user_id=1, chat_id=2, command="/codex") is True
    assert policy.additional_identities() == ()


def test_policy_file_applies_allow_and_deny_rules(tmp_path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "default": {"allow": ["/status"]},
                "rules": [
                    {
                        "user_id": 99,
                        "chat_id": 77,
                        "allow": ["/status", "/logs", "/run"],
                        "deny": ["/run"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    policy = CommandPolicy.from_path(owner_user_id=1, owner_chat_id=2, policy_path=path)
    assert policy.is_allowed(user_id=99, chat_id=77, command="/status") is True
    assert policy.is_allowed(user_id=99, chat_id=77, command="/run") is False
    assert policy.is_allowed(user_id=30, chat_id=40, command="/status") is True
    assert policy.is_allowed(user_id=30, chat_id=40, command="/logs") is False
    assert (99, 77) in policy.additional_identities()


def test_owner_is_implicitly_allowed_when_not_in_rules(tmp_path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"default": {"allow": []}, "rules": []}), encoding="utf-8")

    policy = CommandPolicy.from_path(owner_user_id=10, owner_chat_id=20, policy_path=path)
    assert policy.is_allowed(user_id=10, chat_id=20, command="/autopilot") is True
