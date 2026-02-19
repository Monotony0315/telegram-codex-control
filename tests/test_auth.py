from __future__ import annotations

from telegram_codex_control.auth import Authorizer, extract_message_identity


def test_authorizer_exact_allowlist() -> None:
    authorizer = Authorizer(allowed_user_id=1, allowed_chat_id=2)
    assert authorizer.is_authorized(1, 2) is True
    assert authorizer.is_authorized(3, 2) is False
    assert authorizer.is_authorized(1, 4) is False
    assert authorizer.is_authorized(None, 2) is False


def test_authorizer_supports_extra_identities() -> None:
    authorizer = Authorizer(
        allowed_user_id=1,
        allowed_chat_id=2,
        extra_identities=((3, 4),),
    )
    assert authorizer.is_authorized(3, 4) is True
    assert authorizer.is_authorized(3, 5) is False


def test_authorizer_can_allow_all_authenticated() -> None:
    authorizer = Authorizer(
        allowed_user_id=1,
        allowed_chat_id=2,
        allow_all_authenticated=True,
    )
    assert authorizer.is_authorized(999, 1000) is True
    assert authorizer.is_authorized(None, 1000) is False


def test_extract_message_identity() -> None:
    update = {"message": {"chat": {"id": 22}, "from": {"id": 11}, "text": "/status"}}
    user_id, chat_id = extract_message_identity(update)
    assert user_id == 11
    assert chat_id == 22
