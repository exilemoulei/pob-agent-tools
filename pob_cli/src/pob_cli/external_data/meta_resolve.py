"""Bilingual EN/ZH & player-slang resolution helpers for `pob meta`."""

from __future__ import annotations

import re
from typing import Any

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# PoE1 base classes & all 19 ascendancies + Reliquarian (Simplified Chinese -> Canonical EN).
# Traditional Chinese inputs are folded via `fold_han` / `normalize_tw_query` before lookup.
CLASS_AND_ASC_ZH_TO_EN: dict[str, str] = {
    "野蛮人": "Marauder",
    "勇士": "Juggernaut",
    "暴徒": "Berserker",
    "酋长": "Chieftain",
    "游侠": "Ranger",
    "锐眼": "Deadeye",
    "追猎者": "Pathfinder",
    "药侠": "Pathfinder",
    "守望者": "Warden",
    "女巫": "Witch",
    "死灵师": "Necromancer",
    "死灵": "Necromancer",
    "元素使": "Elementalist",
    "元素": "Elementalist",
    "秘术家": "Occultist",
    "秘术": "Occultist",
    "决斗者": "Duelist",
    "处刑者": "Slayer",
    "处刑": "Slayer",
    "卫士": "Gladiator",
    "冠军": "Champion",
    "圣堂武僧": "Templar",
    "圣堂": "Templar",
    "判官": "Inquisitor",
    "圣宗": "Hierophant",
    "守护者": "Guardian",
    "暗影刺客": "Shadow",
    "暗影": "Shadow",
    "刺客": "Assassin",
    "破坏者": "Saboteur",
    "破坏": "Saboteur",
    "欺诈师": "Trickster",
    "欺诈": "Trickster",
    "贵族": "Scion",
    "升华使徒": "Ascendant",
    "使徒": "Ascendant",
    "守藏者": "Reliquarian",
    "守藏": "Reliquarian",
}

CLASS_AND_ASC_EN_TO_ZH: dict[str, str] = {
    "Marauder": "野蛮人",
    "Juggernaut": "勇士",
    "Berserker": "暴徒",
    "Chieftain": "酋长",
    "Ranger": "游侠",
    "Deadeye": "锐眼",
    "Pathfinder": "追猎者",
    "Warden": "守望者",
    "Witch": "女巫",
    "Necromancer": "死灵师",
    "Elementalist": "元素使",
    "Occultist": "秘术家",
    "Duelist": "决斗者",
    "Slayer": "处刑者",
    "Gladiator": "卫士",
    "Champion": "冠军",
    "Templar": "圣堂武僧",
    "Inquisitor": "判官",
    "Hierophant": "圣宗",
    "Guardian": "守护者",
    "Shadow": "暗影刺客",
    "Assassin": "刺客",
    "Saboteur": "破坏者",
    "Trickster": "欺诈师",
    "Scion": "贵族",
    "Ascendant": "升华使徒",
    "Reliquarian": "守藏者",
}


def resolve_meta_filter(
    raw: str | None,
    *,
    kind: str,
    poedb_lookup: Any = None,
) -> tuple[str | None, str | None]:
    """Resolve `pob meta --skill` or `--class` input (EN, ZH, TW, or slang) to an English filter needle.

    Returns `(needle_lower, resolved_canonical_en_if_changed)`.
    """
    if not raw or not raw.strip():
        return None, None
    cleaned = raw.strip()
    if not _CJK_RE.search(cleaned):
        return cleaned.lower(), None

    from ..game_data.slang import resolve_player_slang
    from ..game_data.tw_norm import fold_han, normalize_tw_query

    norm_tw = normalize_tw_query(cleaned)
    folded = fold_han(norm_tw)
    cands: list[str] = []
    for c in (cleaned, norm_tw, folded):
        if c and c not in cands:
            cands.append(c)

    if kind == "class":
        for c in cands:
            hit = CLASS_AND_ASC_ZH_TO_EN.get(c)
            if hit:
                return hit.lower(), hit
        for c in cands:
            slang = resolve_player_slang(c)
            if slang and slang[1] in ("ascendancy", "class"):
                return slang[0].lower(), slang[0]

    for c in cands:
        slang = resolve_player_slang(c)
        if slang and (kind != "skill" or slang[1] in ("gem", "skill", "mechanic")):
            return slang[0].lower(), slang[0]

    if callable(poedb_lookup):
        en_poedb = poedb_lookup(tuple(cands))
        if isinstance(en_poedb, str) and en_poedb.strip():
            return en_poedb.strip().lower(), en_poedb.strip()

    try:
        from ..game_data.zh_match import english_for_zh

        for c in cands:
            en_zh = english_for_zh(c)
            if en_zh and not _CJK_RE.search(en_zh):
                return en_zh.strip().lower(), en_zh.strip()
    except Exception:
        pass

    if norm_tw and not _CJK_RE.search(norm_tw):
        en_tw = norm_tw.strip()
        if en_tw:
            return en_tw.lower(), en_tw

    return cleaned.lower(), None


def bilingual_meta_label(en_name: str, *, kind: str, show_zh: bool) -> str:
    """Format an English ascendancy or skill name with optional Chinese label (`EN (中文)`)."""
    if not en_name or not show_zh:
        return en_name
    if kind == "class":
        zh = CLASS_AND_ASC_EN_TO_ZH.get(en_name)
        return f"{en_name} ({zh})" if zh else en_name

    from ..game_data.slang import PLAYER_SLANG_MAP

    en_low = en_name.lower()
    for zh_key, (target_en, target_kind) in PLAYER_SLANG_MAP.items():
        if target_kind == "gem" and target_en.lower() == en_low:
            return f"{en_name} ({zh_key})"
    return en_name
