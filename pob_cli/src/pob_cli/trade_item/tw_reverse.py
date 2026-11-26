"""Traditional Chinese (`zh-rTW`) -> English reverse item text translation for `pathofexile.tw` eval."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..game_data.paths import list_available_zh_dirs, resolve_pob_dir
from .models import normalize_template
from .parser import build_item_text, trade_mod_tiers

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_PH_RE = re.compile(r"\{(\d*)[^}]*\}")
_NUM_PAT = r"([+-]?\d+(?:\.\d+)?%?)"
_SKELETON_STRIP_RE = re.compile(r"\{[^}]*\}|#|[0-9.+\-%\s]")
_LEADING_TAGS_RE = re.compile(r"^((?:\{[a-z0-9_:]+\})+)\s*", re.IGNORECASE)
_TRAILING_PAREN_RE = re.compile(r"^(.*?)\s*(\([A-Za-z ]+\))\s*$")
_ROLL_RANGE_RE = re.compile(r"(\d)\([0-9.]+-[0-9.]+\)")
_WIKI_LINK_PIPE_RE = re.compile(r"\[[^\]|]*\|([^\]]*)\]")
_WIKI_LINK_PLAIN_RE = re.compile(r"\[([^\]]*)\]")

_FALLBACK_HEADERS: dict[str, str] = {
    "物品種類": "Item Class",
    "物品类别": "Item Class",
    "稀有度": "Rarity",
    "品質": "Quality",
    "品质": "Quality",
    "閃避值": "Evasion Rating",
    "闪避值": "Evasion Rating",
    "能量護盾": "Energy Shield",
    "能量护盾": "Energy Shield",
    "護甲": "Armour",
    "护甲": "Armour",
    "需求": "Requirements",
    "等級": "Level",
    "等级": "Level",
    "力量": "Str",
    "敏捷": "Dex",
    "智慧": "Int",
    "插槽": "Sockets",
    "物品等級": "Item Level",
    "物品等级": "Item Level",
    "物理傷害": "Physical Damage",
    "元素傷害": "Elemental Damage",
    "混沌傷害": "Chaos Damage",
    "暴擊率": "Critical Strike Chance",
    "每秒攻擊次數": "Attacks per Second",
    "武器範圍": "Weapon Range",
    "格擋機率": "Chance to Block",
    "範圍": "Radius",
    "僅限": "Limited to",
    "記憶絲縷": "Memory Strands",
    "记忆丝缕": "Memory Strands",
}
_FALLBACK_RARITY: dict[str, str] = {
    "稀有": "Rare",
    "魔法": "Magic",
    "傳奇": "Unique",
    "传奇": "Unique",
    "普通": "Normal",
}
_FALLBACK_STATUS: dict[str, str] = {
    "已汙染": "Corrupted",
    "已污染": "Corrupted",
    "已腐化": "Corrupted",
    "已複製": "Mirrored",
    "已复制": "Mirrored",
    "未鑑定": "Unidentified",
    "未鉴定": "Unidentified",
    "已分裂": "Split",
    "追憶物品": "Synthesised Item",
    "忆境物品": "Synthesised Item",
    "破裂物品": "Fractured Item",
    "分裂物品": "Fractured Item",
    "塑者之物": "Shaper Item",
    "尊師之物": "Elder Item",
}
_FALLBACK_RADIUS: dict[str, str] = {
    "小": "Small",
    "中": "Medium",
    "大": "Large",
    "非常大": "Very Large",
    "巨大": "Massive",
    "可變的": "Variable",
}

_REVERSE_CACHE: dict[tuple[tuple[str, float], ...], dict[str, Any]] = {}


def clear_tw_reverse_cache() -> None:
    """Clear in-process reverse translation table cache (used by tests)."""
    _REVERSE_CACHE.clear()


def has_cjk(text: str | None) -> bool:
    return bool(text and _CJK_RE.search(text))


def _nows(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _skeleton(s: str) -> str:
    return _SKELETON_STRIP_RE.sub("", s)


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_entries(path: Path) -> dict[str, str]:
    obj = _read_json(path)
    if not isinstance(obj, dict):
        return {}
    raw = obj.get("entries") if isinstance(obj.get("entries"), dict) else obj
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _add_stat(tbl: dict[str, Any], zh: str, en: str) -> None:
    if not zh or not en or "\n" in zh or "\n" in en:
        return
    if _PH_RE.search(zh) or "#" in zh:
        sk = _skeleton(zh)
        if not sk:
            return
        bucket = tbl["tpl"].setdefault(sk, [])
        if len(bucket) < 40 and not any(item[0] == zh and item[1] == en for item in bucket):
            bucket.append([zh, en, None, None])
    elif has_cjk(zh):
        key = _nows(zh)
        bucket = tbl["exact"].setdefault(key, [])
        if len(bucket) < 6 and en not in bucket:
            bucket.append(en)


def _load_meta(tbl: dict[str, Any], zh_dir: Path) -> None:
    meta = _read_json(zh_dir / "item_metadata.json")
    if not isinstance(meta, dict):
        return

    def _fill(pairs: Any, target: dict[str, str]) -> None:
        if not isinstance(pairs, list):
            return
        for pair in pairs:
            if isinstance(pair, dict) and isinstance(pair.get("zh"), str) and isinstance(pair.get("en"), str):
                target.setdefault(_nows(pair["zh"]), pair["en"])

    _fill(meta.get("headers"), tbl["headers"])
    _fill(meta.get("rarity_values"), tbl["rarity"])
    _fill(meta.get("item_classes"), tbl["classes"])
    _fill(meta.get("status_lines"), tbl["status"])
    _fill(meta.get("influence_tags"), tbl["status"])
    hv = meta.get("header_values")
    if isinstance(hv, dict):
        _fill(hv.get("Radius"), tbl["radius"])
    for p in meta.get("composite_prefixes") or []:
        if isinstance(p, dict) and isinstance(p.get("zh"), str) and isinstance(p.get("en"), str):
            tbl["prefixes"].append((p["zh"], p["en"]))


def _load_reverse_tables(
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any] | None:
    try:
        resolved = resolve_pob_dir(pob_dir, runtime_dir)
        avail = {d.name: d for d in list_available_zh_dirs(resolved)}
    except Exception:
        return None

    dirs = [avail[sub] for sub in ("zh-rTW", "zh-rCN") if sub in avail]
    if not dirs:
        return None
    key_parts: list[tuple[str, float]] = []
    try:
        for d in dirs:
            meta = d / "meta.json"
            mtime = meta.stat().st_mtime if meta.is_file() else d.stat().st_mtime
            key_parts.append((str(d.resolve()), mtime))
    except OSError:
        return None
    cache_key = tuple(key_parts)
    cached = _REVERSE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    tbl: dict[str, Any] = {
        "exact": {},
        "tpl": {},
        "uniques": {},
        "bases": {},
        "base_list": [],
        "headers": {},
        "rarity": {},
        "classes": {},
        "status": {},
        "radius": {},
        "prefixes": [],
        "syn": [],
    }
    for zh_dir in dirs:
        for fname in ("ggpk_reverse.json", "stats.json", "ggpk_full.json", "stat_wrappers.json"):
            for en, zh in _read_entries(zh_dir / fname).items():
                _add_stat(tbl, zh, en)
        for en, zh in _read_entries(zh_dir / "items.json").items():
            if has_cjk(zh):
                tbl["bases"].setdefault(_nows(zh), en)
        for en, zh in _read_entries(zh_dir / "uniques.json").items():
            if has_cjk(zh):
                tbl["uniques"].setdefault(_nows(zh), en)
        _load_meta(tbl, zh_dir)
        syn = _read_json(zh_dir / "synonyms.json")
        if isinstance(syn, dict):
            for pair in syn.get("replacements") or []:
                if isinstance(pair, dict) and pair.get("from") and pair.get("to"):
                    tbl["syn"].append((str(pair["from"]), str(pair["to"])))

    for k, v in _FALLBACK_HEADERS.items():
        tbl["headers"].setdefault(k, v)
    for k, v in _FALLBACK_RARITY.items():
        tbl["rarity"].setdefault(k, v)
    for k, v in _FALLBACK_STATUS.items():
        tbl["status"].setdefault(k, v)
    for k, v in _FALLBACK_RADIUS.items():
        tbl["radius"].setdefault(k, v)
    tbl["base_list"] = sorted(tbl["bases"].items(), key=lambda kv: len(kv[0]), reverse=True)
    _REVERSE_CACHE[cache_key] = tbl
    return tbl


def _compile_tpl(zh: str) -> tuple[re.Pattern[str], list[int]]:
    parts: list[str] = [r"^\s*"]
    keys: list[int] = []
    pos = 0
    seq = 0
    while pos < len(zh):
        m_ph = _PH_RE.search(zh, pos)
        h_idx = zh.find("#", pos)
        use_hash = h_idx != -1 and (m_ph is None or h_idx < m_ph.start())
        if use_hash:
            s, e, inner = h_idx, h_idx + 1, ""
        elif m_ph is not None:
            s, e, inner = m_ph.start(), m_ph.end(), m_ph.group(1)
        else:
            break
        lit = re.escape(zh[pos:s]).replace(r"\ ", r"\s*")
        parts.append(lit)
        parts.append(_NUM_PAT)
        keys.append(int(inner) if inner.isdigit() else seq)
        seq += 1
        pos = e
    parts.append(re.escape(zh[pos:]).replace(r"\ ", r"\s*") + r"\s*$")
    return re.compile("".join(parts)), keys


def _render_en(en: str, keys: list[int], caps: tuple[str, ...]) -> str | None:
    vals = {k: caps[i] for i, k in enumerate(keys) if i < len(caps)}
    seq = -1

    def _sub_ph(m: re.Match[str]) -> str:
        nonlocal seq
        seq += 1
        d = m.group(1)
        key = int(d) if d.isdigit() else seq
        return vals.get(key, m.group(0))

    def _sub_hash(_m: re.Match[str]) -> str:
        nonlocal seq
        seq += 1
        return vals.get(seq, "#")

    out = _PH_RE.sub(_sub_ph, en)
    out = re.sub(r"#", _sub_hash, out)
    if "{" in out or "#" in out or has_cjk(out):
        return None
    out = _WIKI_LINK_PIPE_RE.sub(r"\1", out)
    out = _WIKI_LINK_PLAIN_RE.sub(r"\1", out)
    return out.replace("++", "+").replace("+-", "-")


_SHORT_ATTR_RE = re.compile(r"^[+-]?\d+\s+(?:Str|Dex|Int)$")


def _match_stat(tbl: dict[str, Any], core: str) -> str | None:
    exact_hits = tbl["exact"].get(_nows(core))
    if exact_hits:
        return exact_hits[0]
    cands: list[str] = []
    for entry in tbl["tpl"].get(_skeleton(core)) or ():
        if entry[2] is None:
            entry[2], entry[3] = _compile_tpl(entry[0])
        m = entry[2].match(core)
        if m:
            rendered = _render_en(entry[1], entry[3], m.groups())
            if rendered and rendered not in cands:
                cands.append(rendered)
    if not cands:
        return None
    for cand in cands:
        if not _SHORT_ATTR_RE.match(cand):
            return cand
    return cands[0]


def _translate_mod_line(tbl: dict[str, Any], text: str) -> str | None:
    m_tags = _LEADING_TAGS_RE.match(text)
    tag_prefix = m_tags.group(1) if m_tags else ""
    body = text[m_tags.end() :] if m_tags else text

    m_suf = _TRAILING_PAREN_RE.match(body)
    core, suf = (m_suf.group(1), m_suf.group(2)) if m_suf else (body, "")
    core = _ROLL_RANGE_RE.sub(r"\1", core).strip()
    if not has_cjk(core):
        return text

    for p_zh, p_en in tbl["prefixes"]:
        if core.startswith(p_zh):
            inner = _translate_mod_line(tbl, core[len(p_zh) :])
            if inner:
                return f"{tag_prefix}{p_en}{inner}{(' ' + suf) if suf else ''}"

    en = _match_stat(tbl, core)
    if not en and tbl["syn"]:
        alt = core
        for frm, to in tbl["syn"]:
            alt = alt.replace(frm, to)
        if alt != core:
            en = _match_stat(tbl, alt)
    if not en:
        return None
    return f"{tag_prefix}{en}{(' ' + suf) if suf else ''}"


def _find_base(tbl: dict[str, Any], line: str) -> str | None:
    k = _nows(line)
    if k in tbl["bases"]:
        return tbl["bases"][k]
    for zh_k, en_v in tbl["base_list"]:
        if zh_k in k:
            return en_v
    return None


def _translate_header(tbl: dict[str, Any], line: str) -> tuple[str | bool | None, str | None, str | None]:
    norm = line.replace("：", ":")
    if ":" not in norm:
        return None, None, None
    key, val = norm.split(":", 1)
    en_key = tbl["headers"].get(_nows(key))
    if not en_key:
        return None, None, None
    val = val.strip()
    if not val:
        return f"{en_key}:", en_key, ""
    v = val
    if en_key == "Rarity":
        v = tbl["rarity"].get(_nows(val), val)
    elif en_key == "Item Class":
        mapped = tbl["classes"].get(_nows(val))
        if not mapped:
            return False, en_key, None
        v = mapped
    elif en_key == "Radius":
        v = tbl["radius"].get(_nows(val), val)
    elif has_cjk(val):
        v = _translate_mod_line(tbl, val) or val
    return f"{en_key}: {v}", en_key, v


def decode_extended_item_text(raw_item: dict[str, Any]) -> str | None:
    """Base64-decode `item.extended.text` from a PoE1 trade fetch payload if present."""
    if not isinstance(raw_item, dict):
        return None
    item = raw_item.get("item") if isinstance(raw_item.get("item"), dict) else raw_item
    if not isinstance(item, dict):
        return None
    ext = item.get("extended")
    if not isinstance(ext, dict):
        return None
    b64 = ext.get("text")
    if not isinstance(b64, str) or not b64.strip():
        return None
    cleaned = b64.strip()
    pad = (-len(cleaned)) % 4
    try:
        raw_bytes = base64.b64decode(cleaned + ("=" * pad))
        decoded = raw_bytes.decode("utf-8", errors="replace").strip()
        return decoded or None
    except Exception:
        return None


def reverse_tw_item_text(
    raw: str,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    worker_reverse: Callable[[str], str] | None = None,
) -> str:
    """Convert Traditional/Simplified Chinese item text into valid English PoB clipboard text."""
    if not isinstance(raw, str) or not has_cjk(raw):
        return raw

    text = raw
    if callable(worker_reverse):
        try:
            candidate = worker_reverse(text)
            if isinstance(candidate, str) and candidate.strip():
                text = candidate
                if not has_cjk(text):
                    return text
        except Exception:
            pass

    tbl = _load_reverse_tables(pob_dir=pob_dir, runtime_dir=runtime_dir)
    if not tbl:
        return text

    out: list[str] = []
    rarity: str | None = None
    names_left = 0
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        res: str | None = None
        if not line:
            continue
        if line == "--------":
            names_left = 0
            res = line
        elif names_left > 0 and ":" not in line.replace("：", ":"):
            k = _nows(line)
            if names_left == 2 and rarity in ("Rare", "Unique", "Relic"):
                uniq = tbl["uniques"].get(k)
                if uniq:
                    res = uniq
                else:
                    base = tbl["bases"].get(k)
                    if base:
                        res = base
                        names_left = 1
                    else:
                        res = line
            else:
                res = (_find_base(tbl, line) or line) if has_cjk(line) else line
            names_left -= 1
        elif not has_cjk(line):
            res = line
            if line.lower().startswith("implicits:"):
                names_left = 0
            else:
                m_rar = re.match(r"^Rarity:\s*([A-Za-z]+)", line, re.IGNORECASE)
                if m_rar:
                    rv = m_rar.group(1).title()
                    rarity = rv
                    names_left = 2 if rv in ("Rare", "Unique", "Relic") else 1
        elif line.startswith("{") and not _LEADING_TAGS_RE.match(line):
            res = None
        else:
            names_left = 0
            hdr, en_key, val = _translate_header(tbl, line)
            if hdr is False:
                res = None
            elif isinstance(hdr, str):
                res = hdr
                if en_key == "Rarity" and val:
                    rv = val.title()
                    rarity = rv
                    names_left = 2 if rv in ("Rare", "Unique", "Relic") else 1
            else:
                res = tbl["status"].get(_nows(line)) or _translate_mod_line(tbl, line) or line
        if res is not None:
            out.append(res)
    return "\n".join(out)


def build_english_item_text(
    raw_item: dict[str, Any],
    *,
    realm: str = "global",
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    worker_reverse: Callable[[str], str] | None = None,
) -> str:
    """Build PoB clipboard text from a trade fetch payload, reverse-translating `zh-TW` items to EN."""
    text: str | None = None
    try:
        text = build_item_text(raw_item)
    except ValueError:
        text = decode_extended_item_text(raw_item)
        if not text:
            raise
    if realm == "tw" or has_cjk(text):
        return reverse_tw_item_text(
            text,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
            worker_reverse=worker_reverse,
        )
    return text


def trade_mod_tiers_english(
    raw_item: dict[str, Any],
    *,
    realm: str = "global",
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Extract trade mod tiers keyed by English templates (reverse-translating CJK keys on TW items)."""
    raw_tiers = trade_mod_tiers(raw_item)
    if not raw_tiers:
        return {}
    if realm != "tw" and not any(has_cjk(k) for k in raw_tiers):
        return raw_tiers
    tbl = _load_reverse_tables(pob_dir=pob_dir, runtime_dir=runtime_dir)
    if not tbl:
        return raw_tiers
    out: dict[str, dict[str, Any]] = dict(raw_tiers)
    for tpl_key, meta in raw_tiers.items():
        if not has_cjk(tpl_key):
            continue
        sample = tpl_key.replace("#", "10")
        en_line = _translate_mod_line(tbl, sample)
        if en_line and not has_cjk(en_line):
            en_tpl = normalize_template(en_line)
            if en_tpl:
                out.setdefault(en_tpl, meta)
    return out
