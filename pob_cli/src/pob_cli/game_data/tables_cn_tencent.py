"""Static Tencent CN entity alias tables for bases, uniques, tattoos, and all 32 Runegrafts (L1)."""

from __future__ import annotations

from typing import Any

from .tw_norm import _fold_search_text, fold_han

TENCENT_BASE_ALIASES: dict[str, str] = {
    "大型星团珠宝": "Large Cluster Jewel", "中型星团珠宝": "Medium Cluster Jewel",
    "小型星团珠宝": "Small Cluster Jewel", "菌丝核心": "Mycelium Core",
}

TENCENT_UNIQUE_ALIASES: dict[str, str] = {
    "绝望逃脱": "Impossible Escape", "无所遁形": "Impossible Escape", "逃脱不能": "Impossible Escape",
    "希望之线": "Thread of Hope", "希望之弦": "Thread of Hope", "直觉之跃": "Intuitive Leap",
    "妄语": "Voices", "天神之音": "Voices",
    "英雄悲剧": "Heroic Tragedy", "英勇悲剧": "Heroic Tragedy", "英雄的悲歌": "Heroic Tragedy",
    "分裂人格": "Split Personality", "人格分裂": "Split Personality",
    "普兰德斯之契": "The Perandus Pact", "普兰德斯契约": "The Perandus Pact", "普兰德斯协议": "The Perandus Pact",
    "妄想症": "Megalomaniac", "腐败之种": "Putrid Seed",
    "禁断之火": "Forbidden Flame", "禁断之肉": "Forbidden Flesh",
    "超自然本能": "Unnatural Instinct", "意义的光辉": "The Light of Meaning", "崇高愿景": "Sublime Vision",
    "光彩夺目": "Glorious Vanity", "致命的骄傲": "Lethal Pride", "残酷的约束": "Brutal Restraint",
    "好战的信仰": "Militant Faith", "优雅的狂妄": "Elegant Hubris",
    "溃烂复仇": "Festering Vengeance", "熄灭之握": "Extinguishing Grasp", "噩兆统御": "Baleful Dominion",
    "毁灭渴望": "Destructive Aspiration", "再造恶意": "Reclaimed Malevolence",
}

_HONOURED_TATTOOS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Barbarian", ("野蛮人",)), ("Berserker", ("暴徒",)), ("Dove", ("白鸽", "和平鸽", "鸽子")),
    ("Flock", ("群", "鸟群")), ("Flood", ("水难", "洪流")), ("Hatungo", ("灵语者",)),
    ("Hunter", ("猎人",)), ("Makanga", ("玛坎卡", "马坎加")), ("Mountain", ("山峦",)),
    ("Oak", ("欧克",)), ("Pa", ("帕",)), ("Pillager", ("劫掠者",)), ("Sky", ("天际",)),
    ("Storm", ("风暴",)), ("Tuatara", ("巨蜥", "蜥蜴人")), ("Turtle", ("巨龟",)),
    ("Warlord", ("战争君王",)), ("Wise", ("智者",)),
)
_JOURNEY_TATTOOS: tuple[tuple[str, tuple[str, ...]], ...] = (("Body", ("躯体",)), ("Mind", ("心智",)), ("Soul", ("灵魂",)))
_LOYALTY_TATTOOS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Ahuana", ("阿呼阿娜",)), ("Akoya", ("阿克雅", "阿寇亚", "艾古亚")), ("Ikiaho", ("伊基亚呼",)),
    ("Kahuturoa", ("卡胡图罗亚",)), ("Kaom", ("冈姆",)), ("Kiloava", ("基洛阿瓦", "基洛瓦")),
    ("Maata", ("玛塔",)), ("Rakiata", ("拉凯尔塔", "莱基亚塔", "拉其塔")),
    ("Tawhanuku", ("塔瓦努库",)), ("Utula", ("乌图拉",)),
)

# All 32 PoE1 3.30 Runegrafts (`AlternateMastery` in TattooPassives.lua) -> Tencent CN stems.
_RUNEGRAFT_STEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Bellows", ("咆哮", "轰鸣")), ("Blasphemy", ("渎神",)), ("Connection", ("联结", "联系")),
    ("Consecration", ("奉献",)), ("Fury", ("狂怒", "愤怒")), ("Gemcraft", ("宝石工艺",)),
    ("Loyalty", ("忠诚",)), ("Quaffing", ("畅饮", "狂饮")), ("Rallying", ("奋锐", "召集")),
    ("Refraction", ("折射",)), ("Restitching", ("缝合", "重新缝合")), ("Resurgence", ("复苏",)),
    ("Rotblood", ("腐血",)), ("Stability", ("稳定",)), ("Suffering", ("苦难", "折磨")),
    ("Time", ("时光", "时间")), ("Treachery", ("背叛", "背信")), ("the Agile", ("灵巧", "迅敏")),
    ("the Angler", ("渔夫", "长竿")), ("the Bound", ("束缚", "紧密")), ("the Combatant", ("战士", "战斗者")),
    ("the Fortress", ("堡垒",)), ("the Gauche", ("叉刃", "笨拙")), ("the Imbued", ("灌注",)),
    ("the Jeweller", ("珠宝匠", "工匠")), ("the Novamark", ("新星印记", "星印")),
    ("the River", ("河川",)), ("the Sinistral", ("左旋",)), ("the Soulwick", ("魂芯",)),
    ("the Spellbound", ("着魔", "魔缚")), ("the Warp", ("曲空", "扭曲")), ("the Witchmark", ("女巫印记", "巫印")),
)

TENCENT_TATTOO_ALIASES: dict[str, str] = {"血族的祖灵文身": "Ancestral Tattoo of Bloodlines"}
for _en, _zhs in _HONOURED_TATTOOS:
    for _z in _zhs:
        TENCENT_TATTOO_ALIASES[f"{_z}的尊贵文身"] = f"Honoured Tattoo of the {_en}"
        TENCENT_TATTOO_ALIASES[f"{_z}的尊荣文身"] = f"Honoured Tattoo of the {_en}"
for _en, _zhs in _JOURNEY_TATTOOS:
    for _z in _zhs:
        TENCENT_TATTOO_ALIASES[f"{_z}的旅途文身"] = f"Journey Tattoo of the {_en}"
        TENCENT_TATTOO_ALIASES[f"{_z}的历练文身"] = f"Journey Tattoo of the {_en}"
for _en, _zhs in _LOYALTY_TATTOOS:
    for _z in _zhs:
        TENCENT_TATTOO_ALIASES[f"{_z}的忠诚文身"] = f"Loyalty Tattoo of {_en}"

TENCENT_RUNEGRAFT_ALIASES: dict[str, str] = {}
for _en, _zhs in _RUNEGRAFT_STEMS:
    _full = f"Runegraft of {_en}"
    for _z in _zhs:
        TENCENT_RUNEGRAFT_ALIASES[f"{_z}之符文嵌板"] = _full
        TENCENT_RUNEGRAFT_ALIASES[f"{_z}之符文嫁接"] = _full
        TENCENT_RUNEGRAFT_ALIASES[f"{_z}符文之结"] = _full


def passive_lookup_variants(text: str) -> tuple[str, ...]:
    """Yield normalized CN lookup variants bridging `文身/纹身`, `符文嫁接/符文嵌板`, and optional `之`/`的`."""
    base = fold_han(str(text or "")).replace("\u00a0", " ").strip().lower()
    if not base:
        return ()
    out: list[str] = [base]

    def _add(v: str) -> None:
        if v and v not in out:
            out.append(v)

    for a, b in (("纹身", "文身"), ("文身", "纹身"), ("符文嫁接", "符文嵌板"), ("符文嵌板", "符文嫁接")):
        for cur in list(out):
            if a in cur:
                _add(cur.replace(a, b))
    for particle, suffixes in (
        ("之", ("符文嵌板", "符文嫁接")),
        ("的", ("尊贵文身", "尊荣文身", "旅途文身", "历练文身", "忠诚文身", "文身", "纹身")),
    ):
        for sfx in suffixes:
            for cur in list(out):
                if f"{particle}{sfx}" in cur:
                    _add(cur.replace(f"{particle}{sfx}", sfx))
                elif sfx in cur and not cur.endswith(f"{particle}{sfx}"):
                    _add(cur.replace(sfx, f"{particle}{sfx}"))
    return tuple(out)


def search_tencent_cn_aliases(query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Search static Tencent CN entity aliases (uniques, bases, tattoos, runegrafts) for `pob data translate`."""
    raw = str(query or "").strip()
    probes = passive_lookup_variants(raw) if raw else ()
    if not probes:
        return []
    pools = (
        ("unique", TENCENT_UNIQUE_ALIASES), ("base", TENCENT_BASE_ALIASES),
        ("tattoo", TENCENT_TATTOO_ALIASES), ("runegraft", TENCENT_RUNEGRAFT_ALIASES),
    )
    hits: list[dict[str, Any]] = []
    seen_en: set[str] = set()
    for cat, tbl in pools:
        for probe in probes:
            en = tbl.get(probe)
            if en and en not in seen_en:
                seen_en.add(en)
                hits.append({"en": en, "zh": raw, "category": cat, "match_type": "exact", "source": "tencent"})
                break
    if not hits:
        q_f = _fold_search_text(raw)
        for cat, tbl in pools:
            for zh_k, en in tbl.items():
                if en not in seen_en and (zh_k.startswith(q_f) or _fold_search_text(en) == q_f):
                    seen_en.add(en)
                    mtype = "exact" if _fold_search_text(en) == q_f else "prefix"
                    hits.append({"en": en, "zh": zh_k, "category": cat, "match_type": mtype, "source": "tencent"})
    return hits[:limit] if limit > 0 else hits
