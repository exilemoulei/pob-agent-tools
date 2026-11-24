"""Gates 1-3: Build XML, bidirectional URL, and engine root cross-game isolation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import CrossGameHallucinationError
from .warnings import GAME_ID

_XML_NOTES_STRIP_RE = re.compile(r"<Notes\b[^>]*>[\s\S]*?</Notes>", re.IGNORECASE)
_XML_ROOT_TAG_RE = re.compile(r"<\s*(PathOfBuilding2?)\b")
_XML_TREE_VER_RE = re.compile(r'\btreeVersion\s*=\s*["\']([^"\']+)["\']')
_XML_TARGET_VER_RE = re.compile(r'\btargetVersion\s*=\s*["\']([^"\']+)["\']')
_XML_CLASS_RE = re.compile(r'<\s*Build\b[^>]*\bclassName\s*=\s*["\']([^"\']+)["\']')
_XML_SLOT_RE = re.compile(r'<\s*Slot\b[^>]*\bname\s*=\s*["\']([^"\']+)["\']')

POE1_ONLY_CLASSES = frozenset({"marauder", "duelist", "shadow", "templar", "scion"})
POE2_ONLY_CLASSES = frozenset({"warrior", "monk", "mercenary", "sorceress", "huntress", "druid"})


def validate_build_xml(
    xml_text: str,
    *,
    expected_game: str = GAME_ID,
    source_hint: str | None = None,
) -> None:
    """Hard-block cross-game PoB2 vs PoB1 XML builds before touching Lua `import_code`."""
    cleaned = _XML_NOTES_STRIP_RE.sub("", (xml_text or "").lstrip("\ufeff").strip())
    if not cleaned.startswith("<"):
        return

    root_m = _XML_ROOT_TAG_RE.search(cleaned[:1024])
    root_tag = root_m.group(1) if root_m else None
    tree_versions = _XML_TREE_VER_RE.findall(cleaned)
    target_versions = _XML_TARGET_VER_RE.findall(cleaned)
    class_m = _XML_CLASS_RE.search(cleaned[:4096])
    class_name = class_m.group(1).strip().lower() if class_m else ""
    slots = _XML_SLOT_RE.findall(cleaned)
    src_arg = f' "{source_hint}"' if source_hint else " <source>"

    if expected_game == "poe1":
        _validate_xml_for_poe1(root_tag, tree_versions, target_versions, class_name, slots, src_arg)
    elif expected_game == "poe2":
        _validate_xml_for_poe2(root_tag, tree_versions, class_name, slots, src_arg)


def _validate_xml_for_poe1(
    root_tag: str | None,
    tree_versions: list[str],
    target_versions: list[str],
    class_name: str,
    slots: list[str],
    src_arg: str,
) -> None:
    if root_tag == "PathOfBuilding2":
        tv_note = f" (treeVersion={tree_versions[0]!r})" if tree_versions else ""
        raise CrossGameHallucinationError(
            game="poe1",
            detected_game="poe2",
            trigger=f"<PathOfBuilding2>{tv_note}",
            error=(
                f"Build XML uses PoE2 root `<PathOfBuilding2>`{tv_note}; "
                "`pob` only supports PoE1 `<PathOfBuilding>` builds."
            ),
            remediation="Load PoE2 builds with `pob2`.",
            equivalent_command=f"pob2 load{src_arg}",
            rule_id="XML_POE2_ROOT_IN_POB1",
        )
    for tv in tree_versions:
        if re.match(r"^0_\d+", tv.strip()):
            raise CrossGameHallucinationError(
                game="poe1",
                detected_game="poe2",
                trigger=f'treeVersion="{tv}"',
                error=f"Build uses PoE2 version `{tv}` (PoE1 uses `3_*`).",
                remediation="Load PoE2 builds with `pob2`.",
                equivalent_command=f"pob2 load{src_arg}",
                rule_id="XML_POE2_TREE_IN_POB1",
            )
    for tv in target_versions:
        if re.match(r"^0_\d+", tv.strip()):
            raise CrossGameHallucinationError(
                game="poe1",
                detected_game="poe2",
                trigger=f'targetVersion="{tv}"',
                error=f"Build uses PoE2 targetVersion `{tv}` (PoE1 uses `3_0`).",
                remediation="Load PoE2 builds with `pob2`.",
                equivalent_command=f"pob2 load{src_arg}",
                rule_id="XML_POE2_TREE_IN_POB1",
            )
    if class_name in POE2_ONLY_CLASSES:
        raise CrossGameHallucinationError(
            game="poe1",
            detected_game="poe2",
            trigger=f'className="{class_name}"',
            error=f"Build uses PoE2-only class `{class_name}`.",
            remediation="Load PoE2 builds with `pob2`.",
            equivalent_command=f"pob2 load{src_arg}",
            rule_id="XML_POE2_CLASS_IN_POB1",
        )
    for sl in slots:
        if sl in ("Charm 1", "Charm 2", "Charm 3"):
            raise CrossGameHallucinationError(
                game="poe1",
                detected_game="poe2",
                trigger=f'Slot name="{sl}"',
                error=f"Build XML contains PoE2 belt slot `{sl}`.",
                remediation="Load PoE2 builds with `pob2`.",
                equivalent_command=f"pob2 load{src_arg}",
                rule_id="XML_POE2_SLOT_IN_POB1",
            )


def _validate_xml_for_poe2(
    root_tag: str | None,
    tree_versions: list[str],
    class_name: str,
    slots: list[str],
    src_arg: str,
) -> None:
    if root_tag == "PathOfBuilding":
        tv_note = f" (treeVersion={tree_versions[0]!r})" if tree_versions else ""
        raise CrossGameHallucinationError(
            game="poe2",
            detected_game="poe1",
            trigger=f"<PathOfBuilding>{tv_note}",
            error=(
                f"Build XML uses PoE1 root `<PathOfBuilding>`{tv_note}; "
                "Path of Building (PoE2) strictly requires `<PathOfBuilding2>`."
            ),
            remediation=(
                "Load PoE1 builds with `pob`, or export a PoE2 build from PathOfBuilding-PoE2."
            ),
            equivalent_command=f"pob load{src_arg}",
            rule_id="XML_POE1_ROOT_IN_POB2",
        )
    for tv in tree_versions:
        if re.match(r"^(?:2|3)_\d+", tv.strip()):
            raise CrossGameHallucinationError(
                game="poe2",
                detected_game="poe1",
                trigger=f'treeVersion="{tv}"',
                error=f"Build spec uses PoE1 passive tree version `{tv}` (PoE2 uses `0_1`..`0_5+`).",
                remediation="Use `pob` for PoE1 3.x builds.",
                equivalent_command=f"pob load{src_arg}",
                rule_id="XML_POE1_TREE_IN_POB2",
            )
    if class_name in POE1_ONLY_CLASSES:
        raise CrossGameHallucinationError(
            game="poe2",
            detected_game="poe1",
            trigger=f'className="{class_name}"',
            error=f"Build uses PoE1 class `{class_name}`.",
            remediation="Use `pob` for PoE1 builds.",
            equivalent_command=f"pob load{src_arg}",
            rule_id="XML_POE1_CLASS_IN_POB2",
        )
    for sl in slots:
        if sl in ("Flask 3", "Flask 4", "Flask 5", "Graft 1", "Graft 2") or "Abyssal Socket" in sl:
            raise CrossGameHallucinationError(
                game="poe2",
                detected_game="poe1",
                trigger=f'Slot name="{sl}"',
                error=f"Build XML contains PoE1-only equipment slot `{sl}`.",
                remediation="Use `pob` for PoE1 builds.",
                equivalent_command=f"pob load{src_arg}",
                rule_id="XML_POE1_SLOT_IN_POB2",
            )


# ---------------------------------------------------------------------------
# Gate 2: Bidirectional URL Domain & Path Gate (`validate_url`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UrlGateRule:
    rule_id: str
    belongs_to: str
    pattern: re.Pattern[str]
    label: str
    counterpart_hint: str
    url_rewriter: tuple[str, str] | None = None


URL_GATE_RULES: tuple[UrlGateRule, ...] = (
    UrlGateRule(
        rule_id="URL_POE1_POEDB",
        belongs_to="poe1",
        pattern=re.compile(r"^(?:https?://)?(?:www\.|cdn\.)?poedb\.tw(?=[/?#:]|$)", re.IGNORECASE),
        label="poedb.tw (PoE1 Database)",
        counterpart_hint="Use `https://poe2db.tw/...` or `pob2 poedb <query>` for PoE2.",
        url_rewriter=(r"(?i)\bpoedb\.tw\b", "poe2db.tw"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_NINJA",
        belongs_to="poe1",
        pattern=re.compile(
            r"^(?:https?://)?(?:www\.)?poe\.ninja/(?:poe1(?=[/?#]|$)|builds(?=[/?#]|$)|pob(?=[/?#]|$)|economy(?=[/?#]|$)|api/data(?=[/?#]|$))",
            re.IGNORECASE,
        ),
        label="poe.ninja PoE1 endpoint",
        counterpart_hint="Use `https://poe.ninja/poe2/...` for PoE2.",
    ),
    UrlGateRule(
        rule_id="URL_POE1_TRADE",
        belongs_to="poe1",
        pattern=re.compile(
            r"^(?:https?://)?(?:[a-z0-9-]+\.)?pathofexile\.com/(?:api/trade(?=[/?#]|$)|trade(?=[/?#]|$)|api/leagues(?=[/?#]|$))",
            re.IGNORECASE,
        ),
        label="pathofexile.com/trade or /api/leagues (PoE1 Trade/Leagues API)",
        counterpart_hint="PoE2 official trade uses `/trade2/` and `/api/trade2/...`.",
        url_rewriter=(r"(?i)(pathofexile\.com/(?:api/)?)trade(?=[/?#]|$)", r"\1trade2"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_WIKI",
        belongs_to="poe1",
        pattern=re.compile(
            r"^(?:https?://)?(?:www\.)?(?:poewiki\.net|pathofexile\.fandom\.com)(?=[/?#:]|$)",
            re.IGNORECASE,
        ),
        label="poewiki.net (PoE1 Wiki)",
        counterpart_hint="Use `https://www.poe2wiki.net` (`pob2 wiki <query>`) for PoE2.",
        url_rewriter=(r"(?i)\bpoewiki\.net\b", "poe2wiki.net"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_MAXROLL",
        belongs_to="poe1",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?maxroll\.gg/poe(?=[/?#]|$)", re.IGNORECASE),
        label="maxroll.gg/poe (PoE1)",
        counterpart_hint="PoE2 Maxroll URLs use `https://maxroll.gg/poe2/...`.",
        url_rewriter=(r"(?i)(maxroll\.gg/)poe(?=[/?#]|$)", r"\1poe2"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_MOBALYTICS",
        belongs_to="poe1",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?mobalytics\.gg/poe(?=[/?#]|$)", re.IGNORECASE),
        label="mobalytics.gg/poe (PoE1)",
        counterpart_hint="PoE2 Mobalytics URLs use `https://mobalytics.gg/poe-2/...`.",
        url_rewriter=(r"(?i)(mobalytics\.gg/)poe(?=[/?#]|$)", r"\1poe-2"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_URI_SCHEME",
        belongs_to="poe1",
        pattern=re.compile(r"^pob:(?://|[/\\])", re.IGNORECASE),
        label="pob:// protocol URI (PoE1)",
        counterpart_hint="PoE2 uses the `pob2://` URI scheme.",
        url_rewriter=(r"(?i)^pob:", "pob2:"),
    ),
    UrlGateRule(
        rule_id="URL_POE1_POBCODES",
        belongs_to="poe1",
        pattern=re.compile(r"^(?:https?://)?(?:api\.)?pob\.codes(?=[/?#:]|$)", re.IGNORECASE),
        label="pob.codes (PoE1-only share site)",
        counterpart_hint="PoE2 supports `pobb.in`, `poe2db.tw/pob`, `poe.ninja/poe2/pob`, and `maxroll.gg/poe2/pob`.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_POE2DB",
        belongs_to="poe2",
        pattern=re.compile(r"^(?:https?://)?(?:www\.|cdn\.)?poe2db\.tw(?=[/?#:]|$)", re.IGNORECASE),
        label="poe2db.tw (PoE2 Database)",
        counterpart_hint="Use `pob2` for PoE2DB links, or `https://poedb.tw/...` (`pob poedb`) for PoE1.",
        url_rewriter=(r"(?i)\bpoe2db\.tw\b", "poedb.tw"),
    ),
    UrlGateRule(
        rule_id="URL_POE2_NINJA",
        belongs_to="poe2",
        pattern=re.compile(
            # The optional segment is poe.ninja's language prefix (poe.ninja/zh-tw/poe2/...).
            r"^(?:https?://)?(?:(?:www\.)?poe\.ninja/(?:[a-z]{2}(?:-[a-z]{2})?/)?poe2(?=[/?#]|$)|(?:www\.)?poe2\.ninja(?=[/?#:]|$))",
            re.IGNORECASE,
        ),
        label="poe.ninja/poe2 (PoE2)",
        counterpart_hint="Use `pob2` for `poe.ninja/poe2` links, or `https://poe.ninja/poe1/...` (`pob`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_TRADE2",
        belongs_to="poe2",
        pattern=re.compile(
            r"^(?:https?://)?(?:[a-z0-9-]+\.)?pathofexile\.com/(?:api/)?trade2(?=[/?#]|$)",
            re.IGNORECASE,
        ),
        label="pathofexile.com/trade2 (PoE2 Trade)",
        counterpart_hint="Use `pob2 trade` for PoE2 `/trade2/`, or `/trade/` (`pob trade`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_OFFICIAL_SITE",
        belongs_to="poe2",
        pattern=re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)?pathofexile2\.com(?=[/?#:]|$)", re.IGNORECASE),
        label="pathofexile2.com (PoE2 Official Site)",
        counterpart_hint="Use `pob2` for PoE2 links, or `https://www.pathofexile.com/...` (`pob`) for PoE1.",
        url_rewriter=(r"(?i)\bpathofexile2\.com\b", "pathofexile.com"),
    ),
    UrlGateRule(
        rule_id="URL_POE2_WIKI",
        belongs_to="poe2",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?poe2wiki\.net(?=[/?#:]|$)", re.IGNORECASE),
        label="poe2wiki.net (PoE2 Wiki)",
        counterpart_hint="Use `pob2 wiki` for PoE2 Wiki, or `poewiki.net` (`pob wiki`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_MAXROLL",
        belongs_to="poe2",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?maxroll\.gg/poe2(?=[/?#]|$)", re.IGNORECASE),
        label="maxroll.gg/poe2 (PoE2)",
        counterpart_hint="Use `pob2 load` for `maxroll.gg/poe2`, or `maxroll.gg/poe` (`pob`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_MOBALYTICS",
        belongs_to="poe2",
        pattern=re.compile(r"^(?:https?://)?(?:www\.)?mobalytics\.gg/poe-2(?=[/?#]|$)", re.IGNORECASE),
        label="mobalytics.gg/poe-2 (PoE2)",
        counterpart_hint="Use `pob2` for `mobalytics.gg/poe-2`, or `mobalytics.gg/poe` (`pob`) for PoE1.",
    ),
    UrlGateRule(
        rule_id="URL_POE2_URI_SCHEME",
        belongs_to="poe2",
        pattern=re.compile(r"^pob2:(?://|[/\\])", re.IGNORECASE),
        label="pob2:// protocol URI (PoE2)",
        counterpart_hint="Use `pob2 load` for `pob2://` URIs, or `pob://` (`pob`) for PoE1.",
    ),
)


def validate_url(
    text: str,
    *,
    expected_game: str = GAME_ID,
    cli_subcommand: str = "load",
) -> None:
    """Block cross-game URLs in `load`, `compare`, `poedb`, `wiki`, `price`, `trade`, or `import_tree_url`."""
    cand = (text or "").strip()
    if not cand:
        return
    for rule in URL_GATE_RULES:
        if rule.belongs_to != expected_game and rule.pattern.search(cand):
            other_cli = "pob" if expected_game == "poe2" else "pob2"
            equiv = f'{other_cli} {cli_subcommand} "{cand}"'
            raise CrossGameHallucinationError(
                game=expected_game,
                detected_game=rule.belongs_to,
                trigger=cand,
                error=f"URL matches {rule.label}, which belongs to {rule.belongs_to.upper()}.",
                remediation=rule.counterpart_hint,
                equivalent_command=equiv,
                rule_id=rule.rule_id,
            )


# ---------------------------------------------------------------------------
# Gate 3: Engine Root Isolation Gate (`validate_engine_root`)
# ---------------------------------------------------------------------------


def validate_engine_root(
    path: Path | None,
    *,
    expected_game: str = GAME_ID,
    source_label: str = "POB_ROOT",
) -> None:
    """Block an engine directory that contains ONLY the wrong game's subdirectory."""
    if path is None:
        return
    p = Path(path)
    if not p.exists():
        return
    has_poe1 = (p / "PoE1").is_dir()
    has_poe2 = (p / "PoE2").is_dir()
    if expected_game == "poe1" and has_poe2 and not has_poe1:
        raise CrossGameHallucinationError(
            game="poe1",
            detected_game="poe2",
            trigger=f"{source_label}={p}",
            error=f"Engine directory `{p}` contains `PoE2/` only and has no `PoE1/` subdirectory.",
            remediation=(
                "Point `POB_ROOT` to a PoE1 PobTools directory or run `pob bootstrap` "
                "(or use `pob2`)."
            ),
            equivalent_command="pob bootstrap",
            rule_id="ENV_POE2_ENGINE_IN_POB1",
        )
    if expected_game == "poe2" and has_poe1 and not has_poe2:
        raise CrossGameHallucinationError(
            game="poe2",
            detected_game="poe1",
            trigger=f"{source_label}={p}",
            error=f"Engine directory `{p}` contains `PoE1/` only and has no `PoE2/` subdirectory.",
            remediation=(
                "Point `POB2_ROOT` to a PobTools/PoeCharm3 directory containing `PoE2/`, or run `pob2 bootstrap`."
            ),
            equivalent_command="pob2 bootstrap",
            rule_id="ENV_POE1_ENGINE_IN_POB2",
        )
