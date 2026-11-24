"""Errors shared by the CLI, guardrails, and daemon request handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class DaemonRequestError(RuntimeError):
    """Raised when the PoB daemon rejects a request due to invalid arguments or build state."""


class BootstrapMissingError(RuntimeError):
    """Raised when no local PoB engine exists and GitHub bootstrap failed or is offline."""

    def __init__(self, message: str, update_status: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.update_status = update_status or {}


class CrossGameHallucinationError(DaemonRequestError):
    """Raised when a PoE2-only input/mechanic is sent to `pob` (or a PoE1-only input to `pob2`).

    Contract:
      - Always exits CLI with code `1` (request/caller error, never `5` daemon crash).
      - Carries `errorKind = "cross_game_hallucination"` across daemon HTTP `/rpc` and `--json` output.
    """

    error_kind: str = "cross_game_hallucination"

    def __init__(
        self,
        *,
        game: str = "poe1",
        detected_game: str = "poe2",
        trigger: str,
        error: str,
        remediation: str,
        equivalent_command: str | None = None,
        rule_id: str | None = None,
    ) -> None:
        self.game = game
        self.detected_game = detected_game
        self.trigger = trigger
        self.error_message = error
        self.remediation = remediation
        self.equivalent_command = equivalent_command
        self.rule_id = rule_id
        super().__init__(self.format_human())

    def _cli_name(self) -> str:
        return "pob2" if self.game == "poe2" else "pob"

    def format_human(self) -> str:
        prefix = f"[{self.game.upper()}-CROSS-GAME-BLOCK]"
        parts = [
            f"{prefix} Detected {self.detected_game.upper()} input in `{self._cli_name()}` "
            f"(trigger: {self.trigger!r}): {self.error_message}",
            f"Remediation: {self.remediation}",
        ]
        if self.equivalent_command:
            parts.append(f"Equivalent command: `{self.equivalent_command}`")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "game": self.game,
            "ok": False,
            "errorKind": self.error_kind,
            "detectedGame": self.detected_game,
            "trigger": self.trigger,
            "error": self.error_message,
            "remediation": self.remediation,
            "equivalentCommand": self.equivalent_command,
        }
        if self.rule_id:
            payload["ruleId"] = self.rule_id
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_game: str = "poe1") -> CrossGameHallucinationError:
        game = str(data.get("game") or default_game)
        detected = str(data.get("detectedGame") or ("poe1" if game == "poe2" else "poe2"))
        return cls(
            game=game,
            detected_game=detected,
            trigger=str(data.get("trigger") or ""),
            error=str(data.get("error") or "Cross-game input blocked."),
            remediation=str(data.get("remediation") or ""),
            equivalent_command=data.get("equivalentCommand"),
            rule_id=data.get("ruleId"),
        )


class LegacyPatchTrapError(DaemonRequestError):
    """Raised when a removed PoE1 legacy gem/mechanic (e.g. 3.23-3.25) is passed to a mutator (`skill add/set`).

    Contract:
      - Exits CLI with code `1` (request/caller error, never `5` daemon crash).
      - Carries `errorKind = "legacy_patch_trap"` across daemon HTTP `/rpc` and `--json` output.
    """

    error_kind: str = "legacy_patch_trap"

    def __init__(
        self,
        *,
        trigger: str,
        patch_version: str,
        error: str,
        remediation: str,
        equivalent_command: str | None = None,
        rule_id: str | None = None,
    ) -> None:
        self.game = "poe1"
        self.trigger = trigger
        self.patch_version = patch_version
        self.error_message = error
        self.remediation = remediation
        self.equivalent_command = equivalent_command
        self.rule_id = rule_id
        super().__init__(self.format_human())

    def format_human(self) -> str:
        parts = [
            f"[POE1-PATCH-BLOCK] Removed in PoE1 {self.patch_version} (trigger: {self.trigger!r}): {self.error_message}",
            f"Remediation: {self.remediation}",
        ]
        if self.equivalent_command:
            parts.append(f"Suggested command: `{self.equivalent_command}`")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "game": self.game,
            "ok": False,
            "errorKind": self.error_kind,
            "patchVersion": self.patch_version,
            "trigger": self.trigger,
            "error": self.error_message,
            "remediation": self.remediation,
            "equivalentCommand": self.equivalent_command,
        }
        if self.rule_id:
            payload["ruleId"] = self.rule_id
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LegacyPatchTrapError:
        return cls(
            trigger=str(data.get("trigger") or ""),
            patch_version=str(data.get("patchVersion") or "3.25"),
            error=str(data.get("error") or "Removed legacy PoE1 gem or mechanic."),
            remediation=str(data.get("remediation") or ""),
            equivalent_command=data.get("equivalentCommand"),
            rule_id=data.get("ruleId"),
        )


@dataclass(frozen=True)
class HallucinationWarning:
    """Non-blocking false-friend, legacy-patch, or mistranslation warning attached to valid queries."""

    rule_id: str
    game: str
    category: str
    trigger: str
    summary: str
    poe1_behavior: str
    poe2_behavior: str
    canonical_term: str | None = None
    patch_version: str | None = None
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ruleId": self.rule_id,
            "category": self.category,
            "trigger": self.trigger,
            "warning": self.summary,
            "poe1Behavior": self.poe1_behavior,
            "poe2Behavior": self.poe2_behavior,
        }
        if self.canonical_term:
            out["canonicalTerm"] = self.canonical_term
        if self.patch_version:
            out["patchVersion"] = self.patch_version
        if self.remediation:
            out["remediation"] = self.remediation
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_game: str = "poe1") -> HallucinationWarning:
        return cls(
            rule_id=str(data.get("ruleId") or ""),
            game=str(data.get("game") or default_game),
            category=str(data.get("category") or "false_friend"),
            trigger=str(data.get("trigger") or ""),
            summary=str(data.get("warning") or ""),
            poe1_behavior=str(data.get("poe1Behavior") or ""),
            poe2_behavior=str(data.get("poe2Behavior") or ""),
            canonical_term=data.get("canonicalTerm"),
            patch_version=data.get("patchVersion"),
            remediation=data.get("remediation"),
        )

    def format_stderr(self) -> str:
        if self.category == "legacy_patch":
            tag = "[POE1-PATCH-WARN]"
        elif self.game == "poe2":
            tag = "[POE2-TRAP-WARN]"
        else:
            tag = "[POE1-TRAP-WARN]"
        canon = f" (canonical: {self.canonical_term})" if self.canonical_term else ""
        return f"{tag} `{self.trigger}`{canon}: {self.summary}"

    def format_markdown(self) -> str:
        return f"> {self.format_stderr()}"

