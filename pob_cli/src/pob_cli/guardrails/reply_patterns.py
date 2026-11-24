"""L0 compiled regex patterns and static rule tables for assistant-reply scanning."""

from __future__ import annotations

import re

_SENTENCE_SPLIT_RE = re.compile(
    r"[。！？；\n!?;]+|[,，]\s*(?=(?:所以|因此|于是|只能靠|请改用|改用|推荐连|建议连|可以连|直接连|"
    r"(?:so|therefore)\s+))",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+|pob2://[^\s<>\"']+", re.IGNORECASE)

_NEGATION_EXEMPT_RE = re.compile(
    r"(?:移除|删了|删除|绝版|没有|不存在|不可用|不是|并非|不能|切勿|别用|不要|无需|无效|"
    r"不吃|不受|不会|取代|替代|改为|仅限|已改为|改为固定|不生效|不算|不属于|无法降低|"
    r"是错的|不对|错了|这就错了|错误|误区|误解|辟谣|已?失效|无用|没用|没有用|会?(?:算错|出错)|凭空|误算|假阈值|"
    r"正确[（(]\s*33\s*ms|(?:若|如果|假如|要是)按[^\n。；;]{0,24}(?:则|会|导致|计算|算|换算)|"
    r"已经没了|没了|早已移除|已删除|已删|"
    r"不可得|已绝版|无法(?:再)?获取|打不到|掉不了|买不到|刷不到|(?:属于|已是)遗产|不再掉落|不再存在|"
    r"别挂|别连|别带|别堆|别在|不要挂|不要连|不要带|不要堆|"
    r"(?:是|属于|仅在|只在)[^\n。；;]{0,12}\bpoe2\b|\bpoe2\s*(?:专属|独有|特有|机制|资源)|"
    r"=\s*0\s*(?:收益|效果|作用|提升)|0\s*(?:收益|效果|作用|提升)|零收益|零效果|毫无(?:收益|效果|作用)|"
    r"天生上限\s*3|自然上限\s*3|上限为?\s*3\s*级?|永远到不了|到不了\s*20|"
    r"\blegacy_removed_328\b|\bdrop[- ]disabled\b|\bunobtainable\b|\bnot\s+obtainable\b|"
    r"\bis\s+wrong\b|\bwrong\s+in\s+poe1\b|"
    r"\bremoved\b|\bno\s+longer\b|\bnot\s+in\s+poe1\b|\bzero\s+effect\b|"
    r"\bdoes\s+not\b|\bdoesn't\b|\bdon't\b|\bdo\s+not\b|\bnever\b|\binstead\s+of\b|"
    r"\bnot\s+a\s+curse\b|\bisn't\b|\bis\s+not\b)",
    re.IGNORECASE,
)
_AWAKE_3_RE = re.compile(
    r"(?:\b(?:empower|enhance|enlighten)\b|赋予|賦予|赋能|賦能|增幅|增强|增強|启蒙|啟蒙)",
    re.IGNORECASE,
)
_EXC_OVER_LV5_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])(?:lv\.?|level)\s*(?:[6-9]|[12]\d)\b|(?:[6-9]|[12]\d)\s*(?:级|級)|\b2[01]/2[03]\b)",
    re.IGNORECASE,
)
_INTERROGATIVE_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s+.*(?:吗|么|呢|对不对|行不行|能不能|可不可|是否|[?？])\s*$"
)
_ALT_QUALITY_REPLY_RE = re.compile(
    r"(?:\b(?:anomalous|divergent|phantasmal|alternate\s+quality|alt[- ]quality|regrading\s+lens)\b|"
    r"(?:异常|分歧|魅影|替代)品质|异化宝石|(?:异常|分歧|魅影)的[\u4e00-\u9fff]|"
    r"(?:^|[\s「『(（])(?:异常|分歧|魅影)\s*[:：]\s*[\u4e00-\u9fff])",
    re.IGNORECASE,
)
_SINGLE_SUPPORT_DEBUNK_RE = re.compile(
    r"(?:没有.{0,8}限制|允许重复|可以重复|可重复|不像\s*poe2|"
    r"\bunlike\s+poe2\b|\bno\s+.*limit\b|\bnot\s+.*limit\b|\bare\s+legal\b|\bis\s+legal\b|\ballowed\b)",
    re.IGNORECASE,
)

_EXCLUDED_POE2_IDS = frozenset({"POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY"})
_AMBIGUOUS_ASC_EN = frozenset({"titan", "oracle", "shaman", "amazon", "lich"})
_FREE_TEXT_ASC_ZH = (
    "风暴编织者", "时空术士", "古灵使徒", "宝石军团", "血法师", "鲜血法师",
    "狱火师", "奇塔弗铁匠", "奇塔弗的铁匠", "夏乌拉侍僧", "夏乌拉的侍僧", "猎巫人", "祈求者",
)
_HARD_REMOVED_LEGACY_IDS = frozenset({
    "PATCH_328_AWAKENED_REMOVED",
    "PATCH_325_DIVINE_BLESSING_REMOVED",
    "PATCH_325_ANCESTRAL_TOTEMS_REMOVED",
    "PATCH_323_LAB_ENCHANTS_REMOVED",
    "PATCH_323_ALT_QUALITY_REMOVED",
    "PATCH_324_LEFT_CLICK_AND_CALL_TO_ARMS",
    "PATCH_324_SEXTANTS_REMOVED",
})

_SPIRIT_EN_CTX_RE = re.compile(
    r"(?:\b(?:stack|reserve|reserving|spend|gain|more|extra|total|unreserved)\s+spirit\b|"
    r"\bspirit\s+(?:pool|cost|gem|gems|reservation|reserve|requirement)\b)",
    re.IGNORECASE,
)
_SPIRIT_EN_EXCL_RE = re.compile(
    r"spirit\s+shield|spirit\s+offering|raging\s+spirit|spectral\s+spirit|spirit\s+burst|spirit\s+charge",
    re.IGNORECASE,
)
_DISTILLED_EN_CTX_RE = re.compile(r"\bdistilled\s+(?:emotion|emotions|anoint|oil|oils)\b", re.IGNORECASE)
_DISTILLED_EN_EXCL_RE = re.compile(r"distilled\s+perfection", re.IGNORECASE)
_CHARM_CTX_RE = re.compile(r"(?:\bcharm\s+(?:slot|slots|belt)\b|护符槽|咒符槽|腰带护符|腰带咒符)", re.IGNORECASE)

_POE2_EB_TERM_RE = re.compile(r"(?:\beldritch\s+battery\b|\beb\b|异能魔力)", re.IGNORECASE)
_POE2_EB_FALLACY_RE = re.compile(
    r"(?:(?:把|将)?(?:能量)?护盾转(?:化|换)?为(?:最大)?(?:魔力|蓝量)|"
    r"(?:把|将)?(?:最大)?(?:魔力|蓝量)转(?:化|换)?为(?:最大)?(?:能量)?(?:护盾|es)|"
    r"转(?:化|换)?为(?:最大)?(?:魔力|护盾|es)|"
    r"\bconverts?\s+(?:all\s+)?(?:energy\s+shield|es)\s+(?:in)?to\s+(?:maximum\s+)?mana\b|"
    r"\bconverts?\s+(?:all\s+)?(?:maximum\s+)?mana\s+(?:in)?to\s+(?:maximum\s+)?(?:energy\s+shield|es)\b|"
    r"\bmana[- ]to[- ]es\b|\bes[- ]to[- ]mana\b)",
    re.IGNORECASE,
)
_POE2_SINGLE_SUPPORT_RE = re.compile(
    r"(?:(?:不能|不可|无法|禁止)重复(?:使用|带|插|镶嵌|连)?(?:同一个|相同)?辅助(?:宝石|石)?|"
    r"(?:辅助(?:宝石|石)?|同一(?:颗|个)辅助).{0,12}(?:全(?:角色|身)|每个角色)?只能(?:带|插|用|装备)(?:一颗|一个|1颗|1个)|"
    r"单辅助限制|"
    r"\bonly\s+(?:use|socket|equip|have)\s+(?:one|1)\s+(?:copy\s+of\s+(?:each|a)\s+)?support\s+gem\b|"
    r"\b(?:cannot|can't)\s+(?:use|socket|equip|have)\s+duplicate\s+support\s+gems?\b|"
    r"\bone[- ]per[- ]character\s+support\s+limit\b|\bsingle[- ]support\s+limit\b)",
    re.IGNORECASE,
)

# (rule_id, category, term_re_or_None, trigger_re, reason, remediation, ground_terms)
_FALLACY_SPECS: tuple[tuple[str, str, re.Pattern[str] | None, re.Pattern[str], str, str, tuple[str, ...]], ...] = (
    (
        "EXCEPTIONAL_SUPPORT_LEVEL_CAP",
        "exceptional_level",
        re.compile(
            r"(?:\bexceptional\s*(?:supports?|gems?|辅助|宝石|石)|\bgreater\s+(?:spell\s+echo|multistrike|spell\s+cascade|"
            r"multiple\s+projectiles)\b|卓越辅助|卓越宝石|卓越石|高阶施法回响|高阶多重打击|高阶法术凝聚)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:(?<![A-Za-z0-9])(?:lv\.?|level)\s*(?:[5-9]|[12]\d)\b|(?:[5-9]|[12]\d)\s*(?:级|級)|"
            r"(?:升到|升至|拉到)\s*(?:[5-9]|[12]\d)(?:\s*(?:级|級))?|\b2[01]/2[03]\b)",
            re.IGNORECASE,
        ),
        "3.28+ Exceptional Supports cap at natural Lv3 (corrupted Lv4), never Lv5+ or Lv20.",
        "Correct Exceptional Support level to Lv1-3 (corrupted Lv4).",
        ("Greater Spell Echo",),
    ),
    (
        "FALLACY_SERVER_TICK_33_33MS",
        "mechanic_fallacy",
        None,
        re.compile(r"(?:33\.33\s*ms|0\.0333+\s*s|1\s*/\s*30\s*(?:s|秒|帧))", re.IGNORECASE),
        "PoE1 server tick is 0.033s (33ms), NOT 33.33ms (1/30s).",
        "Recalculate trigger breakpoints with 0.033s tick (`pob data tick`).",
        ("server tick",),
    ),
    (
        "FALLACY_CWC_SCALES_WITH_CDR",
        "mechanic_fallacy",
        re.compile(r"(?:\bcast\s+while\s+channelling\b|\bcwc\b|吟唱时施放|引导时施放)", re.IGNORECASE),
        re.compile(r"(?:\bcdr\b|\bcooldown\s+recovery\b|冷却回复|冷却缩减|冷却速度|帧档位|冷却断点)", re.IGNORECASE),
        "Cast while Channelling (CwC) uses a fixed channelling interval and does NOT scale with CDR.",
        "Remove CDR advice for CwC; only gem level changes its trigger interval.",
        ("Cast while Channelling",),
    ),
    (
        "FALLACY_RAMAKO_RES_LOWERING",
        "mechanic_fallacy",
        re.compile(r"(?:\bramako\b|雷默科|烈阳之光|烈陽之光|太阳之光|太陽之光)", re.IGNORECASE),
        re.compile(
            r"(?:\bflammability\b|\belemental\s+weakness\b|\bexposure\b|\bscorch\b|"
            r"易燃|元素要害|曝露|曝光|焦灼|减火抗|降低火(?:焰)?抗(?:性)?)",
            re.IGNORECASE,
        ),
        "Ramako sets nearby enemies' Fire Res vs DoT to fixed -20% while stationary; Flammability/Ele Weakness/Exposure have zero effect.",
        "Use Punishment or defensive curses instead for stationary Ramako Fire DoT.",
        ("Ramako",),
    ),
    (
        "FALLACY_FROST_BOMB_AS_CURSE",
        "mechanic_fallacy",
        re.compile(r"(?:\bfrost\s+bomb\b|寒霜爆)", re.IGNORECASE),
        re.compile(
            r"(?:(?:作为|当成|当作|算作|属于)诅咒|占(?:用)?(?:一个|1个)?诅咒(?:上限|位)?|诅咒上限|"
            r"\bis\s+a\s+curse\b|\bas\s+a\s+curse\b|\bcounts?\s+as\s+a\s+curse\b|"
            r"\bcurse\s+(?:limit|slot)\b|\buses?\s+a\s+curse\b)",
            re.IGNORECASE,
        ),
        "Frost Bomb is an Exposure/debuff spell, NOT a Curse, and does not use curse limit.",
        "Correct Frost Bomb classification: it is not a Curse.",
        ("Frost Bomb",),
    ),
)

_EXTRA_SLOT_ALIASES: dict[str, str] = {
    "1号戒指": "Ring 1", "1 号戒指": "Ring 1", "一号戒指": "Ring 1", "戒指一": "Ring 1",
    "2号戒指": "Ring 2", "2 号戒指": "Ring 2", "二号戒指": "Ring 2", "戒指二": "Ring 2",
    "3号戒指": "Ring 3", "3 号戒指": "Ring 3", "三号戒指": "Ring 3",
}
_OPEN_AFFIX_CLAIM_RE = re.compile(
    r"(?:空前缀|空后缀|空词缀|还有空位|有空余词缀|有空词缀|有空位|可(?:以)?直接工艺|工艺台补|工艺补|"
    r"\bopen\s+(?:prefix|suffix|affix)\b|\bfree\s+(?:prefix|suffix|affix)\b|\bcraft(?:able)?\s+(?:prefix|suffix|affix)\b)",
    re.IGNORECASE,
)
_OPEN_AFFIX_EXEMPT_RE = re.compile(
    r"(?:先检查|先看|不确定|确认是否有|先确认|需要确认|看(?:一下|看)?有没有空|"
    r"如果有空|若有空|若满词缀|如果还有空|前提是有空|没有空(?:前缀|后缀|词缀)|已满词缀|词缀已满|"
    r"\bcheck\s+if\b|\bif\s+[^\n。；;]{0,20}\bopen\b|\bassuming\s+[^\n。；;]{0,20}\bopen\b|"
    r"\bwhether\s+[^\n。；;]{0,20}\bopen\b|\bno\s+open\s+(?:prefix|suffix|affix)\b|\bfull\s+affixes\b)",
    re.IGNORECASE,
)

_PANEL_PATTERNS = (
    re.compile(
        r"\d+(?:\.\d+)?\s*(?:M|K|万|亿)\s*(?:DPS|EHP|秒伤|有效生命|总伤|击中伤害|持续伤害|点燃\s*DPS|中毒\s*DPS)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:DPS|EHP|秒伤|总秒伤|有效生命|满血有效生命)\s*(?:[:：=≈]|约为?|达到?|高达)\s*\d+(?:\.\d+)?\s*(?:M|K|万|亿)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?:DPS|EHP|总伤|秒伤|满血有效生命|有效生命)[^\n。；;，,]{0,16}?"
        r"(?:[+-]\d+(?:\.\d+)?%|(?:提升|提高|增加|增长|降低|减少|下降|少了|多了)\s*(?:约\s*)?\d+(?:\.\d+)?%)|"
        r"[+-]\d+(?:\.\d+)?%\s*(?:more\s+)?(?:DPS|EHP|总伤|秒伤|有效生命))",
        re.IGNORECASE,
    ),
)

_TRIGGER_CTX_RE = re.compile(
    r"(?:\bcoc\b|暴击时施放|\bcospri\b|冰剑|\bmjolner\b|\bmjölner\b|雷锤|\bpoet\b|诗人|"
    r"\bcwdt\b|受伤时施放|\bsword\s+of\s+ages\b|万古之剑|万古剑|触发冷却|触发器|触发上限|触发频率)",
    re.IGNORECASE,
)
_TRIGGER_NUM_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:APS|次/秒|次每秒|/s)|"
    r"(?:攻速上限|触发上限)[^\n。；;]{0,12}\d+(?:\.\d+)?|"
    r"\d{1,3}\s*%[^\n。；;]{0,16}(?:CDR|冷却回复|冷却缩减|断点|档位|帧)|"
    r"(?:CDR|冷却回复|断点|档位)[^\n。；;]{0,16}\d{1,3}\s*%)",
    re.IGNORECASE,
)

_BUILD_SOURCE_PATTERNS = (
    re.compile(r"@([^\s\"'<>：,，。；;？?！!)\]}”’」』》]+?\.(?:xml|code))(?=\s|$|[：:,，。；;、？?！!)\]}”\"’'」』》])", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?pobb\.in/[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?poe\.ninja/(?:poe1|builds|pob)/[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"https?://(?:api\.)?pob\.codes/[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"(?:[A-Za-z]:[/\\]|(?:\.\.?[/\\]))[^\s\"'<>：,，。；;？?！!)\]}”’」』》]+?\.(?:xml|code)(?=\s|$|[：:,，。；;、？?！!)\]}”\"’'」』》])", re.IGNORECASE),
)
_BUILD_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/_=-])e[NJ][A-Za-z0-9+/_=-]{78,}(?![A-Za-z0-9+/_=-])")

# V5: Hesitation / guessing fingerprints & exemptions
_HESITATION_ZH_RE = re.compile(
    r"(?:不确定|不太确定|不太清楚|记不太清|如果没记错|凭记忆|据我所知|印象中|"
    r"大概(?:可以|会|不会|是|能|不能)|"
    r"应该(?:可以|会|不会|能|不能|吃|不吃|算|不算)|"
    r"理论上(?:可以|应该|会|能|生效)|"
    r"可能(?:可以|不行|有效|无效|触发|不触发|共享|不共享|叠加|不叠加|受限于|吃|不吃)|"
    r"建议(?:进游戏|自行)?实测|以游戏(?:内)?实测为准|"
    r"PoB\s*(?:未模拟|没模拟|算不出|可能不准))",
    re.IGNORECASE,
)
_HESITATION_EN_RE = re.compile(
    r"(?:\bnot\s+(?:100%\s+)?sure\b|\bi\s+think\b|"
    r"\bprobably\s+(?:works?|doesn't|does|triggers?|stacks?)\b|"
    r"\bpresumably\b|\btheoretically\b|\bas\s+far\s+as\s+i\s+know\b|"
    r"\bfrom\s+memory\b|\bunverified\s+in\s+pob\b|"
    r"\bmight\s+(?:work|trigger|stack|apply)\b|\btest\s+in[- ]game\b)",
    re.IGNORECASE,
)
_HESITATION_V3_EXEMPT_RE = re.compile(
    r"(?:不确定[^\n。；;]{0,24}空(?:前缀|后缀|词缀)|"
    r"先检查[^\n。；;]{0,24}空(?:前缀|后缀|词缀)?|"
    r"不确定[^\n。；;]{0,24}(?:先检查|先确认|检查装备)|"
    r"\bcheck\s+if\b[^\n.;]{0,30}\bopen\b|"
    r"\bnot\s+sure\b[^\n.;]{0,30}\bopen\s+(?:prefix|suffix|affix)\b)",
    re.IGNORECASE,
)
_HESITATION_CITED_EXEMPT_RE = re.compile(
    r"(?:据\s*(?:poewiki|poedb)|根据\s*(?:poewiki|poedb)|实测数据|\bpob\s+diagnose\b|"
    r"\baccording\s+to\s+(?:poewiki|poedb)\b|\bper\s+(?:poewiki|poedb)\b)",
    re.IGNORECASE,
)

# V5: Mechanics / interaction / PoB-discrepancy intent detection
_MECH_INTENT_SPECS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "mechanics_principle",
        re.compile(
            r"(?:怎么运作|运作机制|机制原理|触发(?:条件|顺序|先后顺序|机制)|"
            r"(?:能不能|能否)[^\n，,。？?]{0,6}触发|吃不吃|算不算|叠不叠加|能不能叠加|能叠加吗|"
            r"共享冷却|独立冷却|会不会互相覆盖|有没有上限|实际生效|实战怎么循环|"
            r"隐藏机制|互动|交互|热修|\bbug\b|"
            r"\bhow\s+does\b[^\n?.!]{0,60}\bwork\b|"
            r"\binteractions?\b|"
            r"\bdoes\s+[^\n?.!]{0,60}\b(?:apply|trigger|stack|scale|work\s+with)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "pob_discrepancy",
        re.compile(
            r"(?:PoB\s*(?:算得?准不准|算对|没算|未模拟|没模拟|模拟)|"
            r"\bpob\s+(?:calculate|simulate|accurate|handle)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "explicit_web_source",
        re.compile(
            r"(?:\bpoedb(?:\.tw)?\b|\bpoewiki(?:\.net)?\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "assertion_verification",
        re.compile(
            r"(?:这套(?:思路|搭配|玩法|配置|构筑)?(?:对吗|对不对|行不行|可行吗|成立吗)|"
            r"这样(?:搭|配|连|搞|理解)(?:对吗|对不对|行不行|可行吗)|"
            r"(?:听说|有人说)[^\n。！？?!]{6,90}(?:对吗|对不对|真的吗|是真的吗|可行吗|行不行|成立吗))",
            re.IGNORECASE,
        ),
    ),
)

_PAREN_EN_ENTITY_RE = re.compile(
    r"[（(]\s*([A-Z][A-Za-z0-9'\-]*(?:\s+(?:of|the|on|while|with|and|[A-Z][A-Za-z0-9'\-]*)){0,4})\s*[)）]"
)
_QUOTED_TERM_RE = re.compile(r"[「『《\"']([^「『《》」』\"'\n]{2,32})[」』》\"']")
_TITLE_EN_ENTITY_RE = re.compile(
    r"\b([A-Z][a-zA-Z'\-]+(?:\s+(?:of|the|on|while|with|and|[A-Z][a-zA-Z'\-]+)){1,4})\b"
)
_QUERY_STOPWORDS = frozenset({
    "poe", "poe1", "poe2", "pob", "dps", "ehp", "cdr", "aps", "ilvl", "boss",
    "pinnacle", "pinnacle dps", "ring", "gloves", "helmet", "boots", "belt",
    "poedb", "poewiki", "how does", "does", "pob xml", "path of exile",
})
_ZH_QUERY_SPLIT_RE = re.compile(
    r"(?:[^\u4e00-\u9fff]+|怎么运作|运作机制|机制原理|触发(?:条件|顺序|先后顺序|机制)|"
    r"能不能|能否|可不可以|可以|应该|吃不吃|算不算|叠不叠加|有没有|会不会|是不是|"
    r"各自需要多少|需要多少|多少|高不高|好不好|准不准|对不对|行不行|是什么|什么|哪些|为什么|"
    r"配合|同时|另外|如果|请问|帮我|查一下|看看|一下|以及|或者|还是|到底|实际|实战|持续|"
    r"百分比|固定|物品等级|权重|二次击中|击中|单体|同类|叠加|冷却|上限|伤害|基础|数值|底材|"
    r"的|在|上|里|中|了|吗|呢|吧|啊|呀|把|给|对|从|到|比|按|让|被|和|与|及|或|连|带|用|玩|是|有|不|没|别|再|还|也|都|就)"
)
_ZH_QUERY_STOPWORDS = frozenset({
    "构筑", "流派", "技能", "问题", "装备", "词缀", "人物", "怪物", "辅助", "宝石",
    "魔力", "抗性", "护盾", "生命", "面板", "机制", "顺序", "效果", "提升", "来源", "方法",
})
