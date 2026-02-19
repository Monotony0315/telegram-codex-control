from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


class CommandPolicyError(ValueError):
    """Raised when command policy configuration is invalid."""


@dataclass(frozen=True, slots=True)
class PolicyRule:
    user_id: int
    chat_id: int
    allow: frozenset[str]
    deny: frozenset[str]


class CommandPolicy:
    def __init__(
        self,
        *,
        owner_user_id: int,
        owner_chat_id: int,
        rules: tuple[PolicyRule, ...],
        default_allow: frozenset[str] | None = None,
        default_deny: frozenset[str] | None = None,
    ):
        self.owner_user_id = owner_user_id
        self.owner_chat_id = owner_chat_id
        self.rules = rules
        self.default_allow = frozenset() if default_allow is None else default_allow
        self.default_deny = frozenset() if default_deny is None else default_deny

    @classmethod
    def from_path(
        cls,
        *,
        owner_user_id: int,
        owner_chat_id: int,
        policy_path: Path | None,
    ) -> "CommandPolicy":
        if policy_path is None:
            return cls(
                owner_user_id=owner_user_id,
                owner_chat_id=owner_chat_id,
                rules=(
                    PolicyRule(
                        user_id=owner_user_id,
                        chat_id=owner_chat_id,
                        allow=frozenset({"*"}),
                        deny=frozenset(),
                    ),
                ),
            )

        try:
            content = policy_path.read_text(encoding="utf-8")
            payload = json.loads(content)
        except FileNotFoundError:
            raise CommandPolicyError(f"COMMAND_POLICY_PATH does not exist: {policy_path}") from None
        except json.JSONDecodeError as exc:
            raise CommandPolicyError(f"COMMAND_POLICY_PATH is not valid JSON: {policy_path}") from exc

        if not isinstance(payload, dict):
            raise CommandPolicyError("Command policy must be a JSON object")

        default_block = payload.get("default", {})
        if default_block is None:
            default_block = {}
        if not isinstance(default_block, dict):
            raise CommandPolicyError("'default' must be an object")
        default_allow = _parse_command_set(default_block.get("allow", []), context="default.allow")
        default_deny = _parse_command_set(default_block.get("deny", []), context="default.deny")

        raw_rules = payload.get("rules", [])
        if raw_rules is None:
            raw_rules = []
        if not isinstance(raw_rules, list):
            raise CommandPolicyError("'rules' must be an array")

        rules: list[PolicyRule] = []
        owner_rule_present = False
        for idx, raw_rule in enumerate(raw_rules):
            if not isinstance(raw_rule, dict):
                raise CommandPolicyError(f"rules[{idx}] must be an object")
            try:
                user_id = int(raw_rule["user_id"])
                chat_id = int(raw_rule["chat_id"])
            except KeyError as exc:
                raise CommandPolicyError(f"rules[{idx}] missing required key: {exc.args[0]}") from exc
            except ValueError as exc:
                raise CommandPolicyError(f"rules[{idx}] user_id/chat_id must be integers") from exc
            allow = _parse_command_set(raw_rule.get("allow", []), context=f"rules[{idx}].allow")
            deny = _parse_command_set(raw_rule.get("deny", []), context=f"rules[{idx}].deny")
            if user_id == owner_user_id and chat_id == owner_chat_id:
                owner_rule_present = True
            rules.append(
                PolicyRule(
                    user_id=user_id,
                    chat_id=chat_id,
                    allow=allow,
                    deny=deny,
                )
            )

        if not owner_rule_present:
            # Keep backward compatibility: owner remains fully capable unless explicitly overridden.
            rules.append(
                PolicyRule(
                    user_id=owner_user_id,
                    chat_id=owner_chat_id,
                    allow=frozenset({"*"}),
                    deny=frozenset(),
                )
            )

        return cls(
            owner_user_id=owner_user_id,
            owner_chat_id=owner_chat_id,
            rules=tuple(rules),
            default_allow=default_allow,
            default_deny=default_deny,
        )

    def additional_identities(self) -> tuple[tuple[int, int], ...]:
        pairs: list[tuple[int, int]] = []
        for rule in self.rules:
            pair = (rule.user_id, rule.chat_id)
            if pair == (self.owner_user_id, self.owner_chat_id):
                continue
            if pair not in pairs:
                pairs.append(pair)
        return tuple(pairs)

    def is_allowed(self, *, user_id: int, chat_id: int, command: str) -> bool:
        cmd = _normalize_command(command)
        for rule in self.rules:
            if rule.user_id == user_id and rule.chat_id == chat_id:
                return _matches(allow=rule.allow, deny=rule.deny, command=cmd)
        return _matches(allow=self.default_allow, deny=self.default_deny, command=cmd)


def _normalize_command(command: str) -> str:
    candidate = command.strip().lower()
    if not candidate:
        raise CommandPolicyError("Empty command name is not valid")
    if candidate != "*" and not candidate.startswith("/"):
        raise CommandPolicyError(f"Command names must start with '/': {command}")
    return candidate


def _parse_command_set(value: object, *, context: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise CommandPolicyError(f"{context} must be an array of command strings")
    out: set[str] = set()
    for idx, entry in enumerate(value):
        if not isinstance(entry, str):
            raise CommandPolicyError(f"{context}[{idx}] must be a string")
        out.add(_normalize_command(entry))
    return frozenset(out)


def _matches(*, allow: frozenset[str], deny: frozenset[str], command: str) -> bool:
    if "*" in deny or command in deny:
        return False
    if "*" in allow or command in allow:
        return True
    return False
