"""Cluster jewel `hashes_ex` -> PoB synthetic node ID conversion and import reconciliation (L1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JEWEL_SLOTS_3_30: tuple[int, ...] = (
    26725, 36634, 33989, 41263, 60735, 61834, 31683, 28475, 6230, 48768,
    34483, 7960, 46882, 55190, 61419, 2491, 54127, 32763, 26196, 33631,
    21984, 29712, 48679, 9408, 12613, 16218, 2311, 22994, 40400, 46393,
    61305, 12161, 3109, 49080, 17219, 44169, 24970, 36931, 14993, 10532,
    23756, 46519, 23984, 51198, 61666, 6910, 49684, 33753, 18436, 11150,
    22748, 64583, 61288, 13170, 9797, 41876, 59585, 43670, 29914, 18060,
)

_FALLBACK_LARGE_SOCKETS: dict[int, dict[str, int]] = {
    7960: {"index": 0, "proxy": 43989, "size": 2},
    46882: {"index": 1, "proxy": 25134, "size": 2},
    55190: {"index": 2, "proxy": 30275, "size": 2},
    2491: {"index": 3, "proxy": 28650, "size": 2},
    32763: {"index": 4, "proxy": 48132, "size": 2},
    21984: {"index": 5, "proxy": 18756, "size": 2},
}

_FALLBACK_ORBIT_OFFSETS: dict[int, dict[int, int]] = {
    43989: {0: 3, 1: 5, 2: 5}, 25134: {0: 0, 1: 11, 2: 11}, 30275: {0: 2, 1: 3, 2: 3},
    28650: {0: 1, 1: 1, 2: 1}, 48132: {0: 5, 1: 9, 2: 9}, 18756: {0: 4, 1: 7, 2: 7},
    55706: {0: 2, 1: 3}, 26661: {0: 3, 1: 5}, 13201: {0: 3, 1: 7}, 40114: {0: 1},
    18361: {0: 2}, 7956: {0: 3}, 51233: {0: 5, 1: 9}, 57194: {0: 5, 1: 11},
    35853: {0: 0, 1: 1}, 35313: {0: 4}, 44470: {0: 5}, 37147: {0: 0},
    25441: {0: 1, 1: 1}, 28018: {0: 2, 1: 3}, 53203: {0: 3, 1: 5}, 3854: {0: 0},
    49951: {0: 1}, 22046: {0: 2}, 37898: {0: 5, 1: 11}, 64166: {0: 1, 1: 1},
    58355: {0: 2, 1: 3}, 48128: {0: 5}, 27475: {0: 0}, 35070: {0: 1},
    35926: {0: 4, 1: 7}, 33833: {0: 5, 1: 9}, 50179: {0: 5, 1: 11}, 36414: {0: 3},
    10643: {0: 4}, 56439: {0: 5}, 58194: {0: 3, 1: 5}, 34013: {0: 4, 1: 7},
    24452: {0: 5, 1: 9}, 63754: {0: 2}, 54600: {0: 3}, 27819: {0: 4},
}

_ORBIT_16_TO_12: tuple[int, ...] = (0, 1, 1, 2, 3, 4, 4, 5, 6, 7, 7, 8, 9, 10, 10, 11)
_ORBIT_16_TO_6: tuple[int, ...] = (0, 0, 0, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 5, 5, 5)

# Offline 3.30 fallback: first valid mastery effect ID -> mastery node IDs (all 315 mastery nodes).
_FALLBACK_MASTERY_EFFECT_GROUPS: dict[int, tuple[int, ...]] = {
    1915: (4327, 26037, 41744, 55017, 58728), 2459: (2828, 17945, 22067, 41273, 47294, 57949, 60834),
    2498: (7528, 9471, 40698, 52061), 2800: (32657, 56865, 58563, 62759),
    2902: (2841, 3571, 8732, 10495, 13862, 25186, 44948, 48007, 48859, 53188, 63559, 65395),
    4116: (7023, 17380, 38207, 44179, 54887, 60170), 5356: (4492, 8370, 28284),
    9986: (9586, 16123, 31039, 33823, 35085, 51583, 63861), 11541: (1215, 11032),
    13257: (10429, 17906, 37616, 44540), 13382: (6570, 22959, 23796, 50540, 61785, 62506),
    14206: (8460, 25031, 26154, 35977, 60992, 62015), 14796: (8872, 50071, 51761, 52018),
    16545: (35038, 48411, 53828, 56128), 16644: (5826, 47212, 56023), 17852: (29993, 41522),
    19002: (10141, 10414, 24481, 27193, 27235, 53517, 61529),
    19422: (12503, 12873, 17127, 31818, 33657, 42792),
    24180: (11505, 19749, 34927, 37911, 38320, 40271, 48267, 63268),
    25487: (3883, 27371, 41163, 50757, 65528), 28354: (4424, 24224, 28039, 58540),
    28524: (8556, 8629, 10245, 28863, 33037, 45372, 49391, 52875, 55348, 56595),
    28638: (6507, 13712, 39836, 46495, 47197, 53615, 55491, 64128), 28720: (4707, 33678, 34723),
    29161: (89, 10166, 27872, 53738, 59926), 29214: (9393, 9458, 18750, 20736, 41016, 45019, 46665, 46761),
    34563: (1205, 28903, 38579, 60210), 35582: (6912, 25934, 35118, 39338),
    37132: (11596, 14832, 27931, 31292, 59013), 37186: (21143, 40439, 61343), 37925: (51974, 59335),
    40037: (32242, 35321, 43307, 55281), 40041: (4788, 12518, 19050, 53365), 40578: (30393, 37641),
    43295: (31400, 47059, 48982, 49677, 52074), 43524: (38568, 59501),
    44252: (15697, 38622, 44206, 44330, 56519), 45263: (16810, 25535, 37795, 38436, 45558),
    46180: (45358, 48717, 52462, 54340, 64406),
    47429: (14505, 32278, 34552, 39416, 43647, 47242, 54849, 61992), 47595: (10204, 26697, 31291, 63184),
    48358: (14113, 17411, 25313, 27733, 28680, 32509, 40196, 60512),
    48385: (5348, 5726, 13387, 34317, 44298, 48660), 49522: (19750, 62235),
    50863: (5230, 6968, 39332, 48349, 62416), 50933: (21801, 23547, 42361),
    51424: (857, 3471, 6338, 10729, 18240, 27307), 52274: (6588, 19725, 37698, 53216, 62023),
    53046: (240, 14122, 37532, 58816, 63482), 55013: (5368, 21324, 22480, 38921),
    57597: (26148, 27157, 35221, 38595, 42533, 58302), 58447: (20730, 49820, 54413),
    59626: (22295, 37956, 40170, 43601, 63710), 59634: (6427, 24552, 25349, 27865, 37502),
    59815: (9213, 12244, 24334, 48144), 60050: (26608, 34487, 38377, 48508, 55152, 65154),
    61283: (9083, 22970, 26393, 41225, 48290),
    62252: (6384, 7488, 16141, 25446, 28862, 35859, 40383, 43495, 55230),
    62782: (12169, 12239, 20675, 52220, 64042), 63223: (7634, 15409, 31197, 62853),
    64381: (292, 3042, 4139, 12125, 12382, 25011, 25281, 32241, 38235, 41415, 43818, 44316, 48505, 62588),
}
_FALLBACK_MASTERY_FIRST_EFFECT: dict[int, int] = {
    nid: eff for eff, nids in _FALLBACK_MASTERY_EFFECT_GROUPS.items() for nid in nids
}


@dataclass(frozen=True)
class TreeClusterMeta:
    jewel_slots: tuple[int, ...]
    exp_sockets: dict[int, dict[str, int]]
    proxy_to_socket: dict[int, int]
    orbit_offsets: dict[int, dict[int, int]]
    mastery_first_effect: dict[int, int]


_META_CACHE: dict[tuple[str, str], TreeClusterMeta] = {}


def _parse_tree_meta(pob_root: Path | None, tree_version: str) -> TreeClusterMeta:
    key = (str(pob_root or ""), tree_version)
    if key in _META_CACHE:
        return _META_CACHE[key]

    jewel_slots = JEWEL_SLOTS_3_30
    exp_sockets: dict[int, dict[str, int]] = dict(_FALLBACK_LARGE_SOCKETS)
    orbit_offsets: dict[int, dict[int, int]] = dict(_FALLBACK_ORBIT_OFFSETS)
    mastery_first_effect: dict[int, int] = dict(_FALLBACK_MASTERY_FIRST_EFFECT)

    if pob_root is not None:
        poe1 = Path(pob_root) / "PoE1"
        tree_lua = poe1 / "TreeData" / tree_version / "tree.lua"
        if tree_lua.is_file():
            txt = tree_lua.read_text(encoding="utf-8", errors="replace")
            if m_js := re.search(r'\["jewelSlots"\]\s*=\s*\{([^}]+)\}', txt):
                nums = tuple(int(n) for n in re.findall(r"\d+", m_js.group(1)))
                if nums:
                    jewel_slots = nums
            cur_id: int | None = None
            in_exp = False
            exp_buf: list[str] = []
            parsed_masteries: dict[int, int] = {}
            for line in txt.splitlines():
                if m_id := re.match(r"^\t\t\[(\d+)\]\s*=\s*\{", line):
                    cur_id = int(m_id.group(1))
                    in_exp = False
                elif cur_id is not None and '["expansionJewel"] = {' in line:
                    in_exp = True
                    exp_buf = []
                elif in_exp:
                    if line.strip().startswith("}"):
                        in_exp = False
                        kv = {
                            mk.group(1): int(mk.group(2))
                            for entry in exp_buf
                            if (mk := re.search(r'\["([^"]+)"\]\s*=\s*"?(\d+)"?', entry))
                        }
                        if kv and cur_id is not None:
                            exp_sockets[cur_id] = kv
                    else:
                        exp_buf.append(line)
                elif cur_id is not None and cur_id not in parsed_masteries and '["effect"] =' in line:
                    if me := re.search(r'\["effect"\]\s*=\s*(\d+)', line):
                        parsed_masteries[cur_id] = int(me.group(1))
            mastery_first_effect.update(parsed_masteries)

        cj_lua = poe1 / "Data" / "ClusterJewels.lua"
        if cj_lua.is_file():
            cj = cj_lua.read_text(encoding="utf-8", errors="replace")
            oo_idx = cj.find("orbitOffsets = {")
            if oo_idx != -1:
                oo_sub = cj[oo_idx : cj.find("blightcoreNotableIndex", oo_idx)]
                parsed_oo = {
                    int(m_p.group(1)): {int(k): int(v) for k, v in re.findall(r"\[(\d+)\]\s*=\s*(\d+)", m_p.group(2))}
                    for m_p in re.finditer(r"\[(\d+)\]\s*=\s*\{([^}]+)\}", oo_sub)
                }
                if parsed_oo:
                    orbit_offsets = parsed_oo

    proxy_to_socket = {info["proxy"]: sid for sid, info in exp_sockets.items() if "proxy" in info}
    meta = TreeClusterMeta(jewel_slots, exp_sockets, proxy_to_socket, orbit_offsets, mastery_first_effect)
    _META_CACHE[key] = meta
    return meta


def load_tree_cluster_meta(pob_root: Path | None, tree_version: str = "3_30") -> TreeClusterMeta:
    return _parse_tree_meta(pob_root, tree_version)


def _translate_orbit_index(src_oidx: int, src_per_orbit: int, dst_per_orbit: int) -> int:
    if src_per_orbit == dst_per_orbit:
        return src_oidx
    if src_per_orbit == 16 and dst_per_orbit == 12 and 0 <= src_oidx < 16:
        return _ORBIT_16_TO_12[src_oidx]
    if src_per_orbit == 16 and dst_per_orbit == 6 and 0 <= src_oidx < 16:
        return _ORBIT_16_TO_6[src_oidx]
    return (src_oidx * dst_per_orbit) // max(1, src_per_orbit)


def resolve_cluster_hashes(
    passives: dict[str, Any],
    *,
    pob_root: Path | None = None,
    tree_version: str = "3_30",
) -> list[int]:
    """Convert GGG `hashes_ex` + `jewel_data` subgraphs into PoB `0x10000+` synthetic IDs & child socket IDs."""
    raw_ex = passives.get("hashes_ex") or []
    if not raw_ex:
        return []
    jewel_data = passives.get("jewel_data") or {}
    meta = load_tree_cluster_meta(pob_root, tree_version)

    sub_groups: dict[str, dict[str, Any]] = {}
    sub_nodes: dict[str, dict[str, Any]] = {}
    exp_sockets = dict(meta.exp_sockets)
    proxy_to_socket = dict(meta.proxy_to_socket)

    if isinstance(jewel_data, dict):
        for jd in jewel_data.values():
            sg = jd.get("subgraph") if isinstance(jd, dict) else None
            if not isinstance(sg, dict):
                continue
            for gid, gdata in (sg.get("groups") or {}).items():
                if isinstance(gdata, dict):
                    sub_groups[str(gid)] = gdata
            for nid, ndata in (sg.get("nodes") or {}).items():
                if not isinstance(ndata, dict):
                    continue
                sub_nodes[str(nid)] = ndata
                exp = ndata.get("expansionJewel")
                if isinstance(exp, dict) and exp.get("proxy") is not None:
                    info = {
                        "size": int(exp.get("size") or 0),
                        "index": int(exp.get("index") or 0),
                        "proxy": int(exp["proxy"]),
                    }
                    if exp.get("parent") is not None:
                        info["parent"] = int(exp["parent"])
                    sid = proxy_to_socket.get(info["proxy"]) or int(ndata.get("skill") or nid)
                    exp_sockets.setdefault(sid, info)
                    proxy_to_socket.setdefault(info["proxy"], sid)

    out: list[int] = []
    seen: set[int] = set()
    slot_set = set(meta.jewel_slots)
    for raw_id in raw_ex:
        if not isinstance(raw_id, int):
            continue
        if raw_id >= 0x10000:
            if raw_id not in seen:
                seen.add(raw_id)
                out.append(raw_id)
            continue
        ndata = sub_nodes.get(str(raw_id))
        if not isinstance(ndata, dict):
            continue
        exp = ndata.get("expansionJewel")
        if ndata.get("isJewelSocket") or isinstance(exp, dict):
            sock_id = None
            if isinstance(exp, dict) and exp.get("proxy") is not None:
                sock_id = proxy_to_socket.get(int(exp["proxy"]))
            if sock_id is None and raw_id in slot_set:
                sock_id = raw_id
            if sock_id is not None and sock_id not in seen:
                seen.add(sock_id)
                out.append(sock_id)
            continue

        gdata = sub_groups.get(str(ndata.get("group") or ""))
        proxy_raw = gdata.get("proxy") if isinstance(gdata, dict) else None
        if proxy_raw is None:
            continue
        proxy_id = int(proxy_raw)
        parent_sid = proxy_to_socket.get(proxy_id)
        parent_exp = exp_sockets.get(parent_sid) if parent_sid is not None else None
        if not parent_exp:
            continue

        id_bits = 0x10000
        if parent_exp["size"] == 2:
            id_bits += parent_exp["index"] << 6
        elif parent_exp["size"] == 1:
            gp_exp = exp_sockets.get(parent_exp.get("parent", -1))
            if not gp_exp:
                continue
            id_bits += (gp_exp["index"] << 6) + (parent_exp["index"] << 9)
        else:
            continue

        if ndata.get("isKeystone"):
            synth_id = id_bits
        else:
            orbit = int(ndata.get("orbit") or 1)
            size_idx = max(0, min(2, orbit - 1))
            total_idx = 6 if size_idx == 0 else 12
            per_orbit = 6 if size_idx == 0 else 16
            corrected = _translate_orbit_index(int(ndata.get("orbitIndex") or 0), per_orbit, total_idx)
            start_oidx = meta.orbit_offsets.get(proxy_id, {}).get(size_idx, 0)
            template_idx = (corrected - start_oidx) % total_idx
            synth_id = id_bits + (size_idx << 4) + template_idx
        if synth_id not in seen:
            seen.add(synth_id)
            out.append(synth_id)
    return out


def reconcile_import_integrity(
    bundle: dict[str, Any],
    import_res: dict[str, Any],
    *,
    pob_root: Path | None = None,
    tree_version: str = "3_30",
) -> tuple[list[str], int, int]:
    """Compare raw profile hashes & cluster jewels against PoB state; zero false alarms on healthy builds."""
    passives = bundle.get("passiveSkills") if isinstance(bundle.get("passiveSkills"), dict) else {}
    raw_hashes = [h for h in (passives.get("hashes") or []) if isinstance(h, int)]
    raw_ex = [h for h in (passives.get("hashes_ex") or []) if isinstance(h, int)]
    warnings: list[str] = []

    lua_clusters = import_res.get("clusterJewels") if isinstance(import_res, dict) else None
    if isinstance(lua_clusters, list):
        for cj in lua_clusters:
            if isinstance(cj, dict) and cj.get("valid") is False:
                mods_str = "; ".join(str(m) for m in (cj.get("rawMods") or []) if m)
                warnings.append(
                    f"[IMPORT-RECONCILE-WARN] Jewel slot {cj.get('nodeId')} ({cj.get('name') or cj.get('baseName')}): "
                    f"cluster jewel invalid (clusterJewelValid=false); raw mods: {mods_str}"
                )

    alloc_raw = import_res.get("allocatedNodeIds") if isinstance(import_res, dict) else None
    if isinstance(alloc_raw, list) and (raw_hashes or raw_ex):
        alloc_set = {int(n) for n in alloc_raw if isinstance(n, (int, float))}
        resolved_ex = resolve_cluster_hashes(passives, pob_root=pob_root, tree_version=tree_version)
        missing_ex = [nid for nid in resolved_ex if nid not in alloc_set]
        if missing_ex:
            cj_items = [
                it for it in (passives.get("items") or [])
                if isinstance(it, dict) and "Cluster Jewel" in str(it.get("baseType") or "")
            ]
            slot_parts: list[str] = []
            for it in cj_items:
                preview = "; ".join(str(m) for m in (it.get("enchantMods") or []))
                if len(preview) > 120:
                    preview = preview[:117] + "..."
                slot_parts.append(f"x={it.get('x')} ({it.get('baseType')}: {preview})")
            slot_desc = ", ".join(slot_parts) or "unknown cluster slot"
            warnings.append(
                f"[IMPORT-RECONCILE-WARN] {len(missing_ex)}/{len(resolved_ex)} cluster nodes were not allocated "
                f"({slot_desc})."
            )
    dropped_main = import_res.get("droppedHashes") if isinstance(import_res, dict) else None
    if isinstance(dropped_main, list) and dropped_main:
        warnings.append(
            f"[IMPORT-RECONCILE-WARN] {len(dropped_main)} passive node(s) from profile hashes were pruned "
            f"(ids={dropped_main[:8]}): check unallocated mastery/runegraft overrides, jump/radius jewels, or tree connectivity."
        )
    return warnings, len(raw_hashes), len(raw_ex)
