"""CLI file, JSON, and build-source input resolution with `NO_SECRET_ENV_DUMP` enforcement (L4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click


def enforce_safe_input_path(path_str: str) -> None:
    """Block credential/cookie file reads via CLI file-input flags (`NO_SECRET_ENV_DUMP`)."""
    from .guardrails import check_secret_file_path

    res = check_secret_file_path(path_str)
    if not res.allowed:
        raise click.ClickException(res.reason or f"Blocked by {res.rule_id or 'NO_SECRET_ENV_DUMP'}")


def _read_json_input(file_path: str | None, inline_json: str | None, *, label: str) -> Any:
    if file_path:
        if file_path == "-":
            raw = sys.stdin.read().lstrip("\ufeff")
        else:
            enforce_safe_input_path(file_path)
            p = Path(file_path)
            if not p.is_file():
                raise click.UsageError(f"{label} file not found: {file_path}")
            raw = p.read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    elif inline_json:
        raw = inline_json.lstrip("\ufeff")
        if raw.startswith("@"):
            target = raw[1:].strip()
            enforce_safe_input_path(target)
            p = Path(target)
            if not p.is_file():
                raise click.UsageError(f"{label} file not found: {p}")
            raw = p.read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    else:
        raise click.UsageError(f"Provide {label} via --file <path|-> or inline JSON.")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"Invalid JSON for {label}: {exc}") from exc


def _prepare_load_source_with_stage(source: str) -> tuple[str | None, str | None, dict[str, str | None]]:
    """Resolve a user build source string plus any `#stage=` / `#spec=` / `#item-set=` / `#skill-set=` fragment."""
    from .guardrails import validate_build_xml, validate_url
    from .importers_stages import split_source_stage_fragment

    raw_full = source.lstrip("\ufeff").strip()
    raw, stage_hints = split_source_stage_fragment(raw_full)
    candidate_path = raw[1:].lstrip("\ufeff").strip() if raw.startswith("@") else raw
    if raw.startswith("@"):
        enforce_safe_input_path(candidate_path)
    is_local_file = False
    if (
        len(candidate_path) <= 260
        and not candidate_path.startswith(("http://", "https://"))
        and not candidate_path.lstrip().startswith("<")
    ):
        enforce_safe_input_path(candidate_path)
        try:
            p = Path(candidate_path)
            is_local_file = p.exists() and p.is_file()
        except OSError:
            is_local_file = False

    if is_local_file:
        text = Path(candidate_path).read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff").strip()
        if text.lstrip().startswith("<"):
            validate_build_xml(text, expected_game="poe1", source_hint=candidate_path)
            from .importers import xml_to_share_code

            return None, xml_to_share_code(text), stage_hints
        validate_url(text, expected_game="poe1", cli_subcommand="load")
        from .bridge_protocol import _decode_share_code_xml

        decoded_file_xml = _decode_share_code_xml(text)
        if decoded_file_xml:
            validate_build_xml(decoded_file_xml, expected_game="poe1", source_hint=candidate_path)
        return text, None, stage_hints

    if raw.startswith("@"):
        raise click.ClickException(f"Build file not found: {candidate_path or raw}")

    if raw.lstrip().startswith("<"):
        validate_build_xml(raw, expected_game="poe1")
        from .importers import xml_to_share_code

        return None, xml_to_share_code(raw), stage_hints

    validate_url(raw, expected_game="poe1", cli_subcommand="load")
    if not raw.startswith(("http://", "https://")):
        from .bridge_protocol import _decode_share_code_xml

        decoded_xml = _decode_share_code_xml(raw)
        if decoded_xml:
            validate_build_xml(decoded_xml, expected_game="poe1")

    return raw, None, stage_hints


def _prepare_load_source(source: str) -> tuple[str | None, str | None]:
    """Resolve a user build source string (`@path`, `.xml` file, share-code `.txt` file, URL, or Base64 code)."""
    resolved_src, resolved_xml, _ = _prepare_load_source_with_stage(source)
    return resolved_src, resolved_xml
