-- Read-only PoB mechanics interactions probe (`scan_unsupported_mods`, `inspect_mod_interactions`).
-- All PoB internal lookups are wrapped in pcall for 2.67.2+ dual-engine compatibility.
local bit = require("bit")
local H = require("pob_host")
local now_ms, get_build = H.now_ms, H.get_build
local M = {}

local MOD_SECTIONS = {
	{ "buff", "buffModLines" },
	{ "enchant", "enchantModLines" },
	{ "scourge", "scourgeModLines" },
	{ "implicit", "implicitModLines" },
	{ "explicit", "explicitModLines" },
	{ "crucible", "crucibleModLines" },
}

local function strip_clean(s)
	if type(s) ~= "string" then return "" end
	local clean = (type(StripEscapes) == "function") and StripEscapes(s)
		or s:gsub("%^x%x%x%x%x%x%x", ""):gsub("%^%d", "")
	return (clean:gsub("%s*[\r\n]+%s*", " "):match("^%s*(.-)%s*$") or "")
end

local function is_ignorable_line(clean)
	if clean == "" then return true end
	if clean:match("^%-+$") then return true end
	if clean:find("Requires Class", 1, true) then return true end
	return false
end

local function decode_bitmask(mask, enum_tbl, skip_keys)
	local out = {}
	if type(mask) ~= "number" or mask == 0 or type(enum_tbl) ~= "table" then return out end
	for name, val in pairs(enum_tbl) do
		if type(name) == "string" and type(val) == "number" and val > 0
			and not (skip_keys and skip_keys[name])
			and bit.band(val, val - 1) == 0
			and bit.band(mask, val) ~= 0 then
			out[#out + 1] = name
		end
	end
	table.sort(out)
	return out
end

local function reverse_skill_type_map()
	local rev = {}
	if type(SkillType) == "table" then
		for name, id in pairs(SkillType) do
			if type(name) == "string" and type(id) == "number" then rev[id] = name end
		end
	end
	return rev
end

local function sanitize_val(v, depth, serialize_mod_fn)
	depth = depth or 0
	local ty = type(v)
	if ty == "number" or ty == "boolean" or ty == "string" then return v end
	if ty ~= "table" or depth >= 3 then return nil end
	if type(v.name) == "string" and type(v.type) == "string" and serialize_mod_fn then
		return serialize_mod_fn(v, depth + 1)
	end
	local out = {}
	for k, x in pairs(v) do
		if type(k) == "string" or type(k) == "number" then
			if k == "mod" and type(x) == "table" and serialize_mod_fn then
				out[k] = serialize_mod_fn(x, depth + 1)
			else
				local sv = sanitize_val(x, depth + 1, serialize_mod_fn)
				if sv ~= nil then out[k] = sv end
			end
		end
	end
	return out
end

local function serialize_tag(tag, st_rev)
	if type(tag) ~= "table" then return nil end
	local out = {}
	for k, v in pairs(tag) do
		if type(k) == "string" then
			local tv = type(v)
			if tv == "string" or tv == "number" or tv == "boolean" then
				out[k] = v
			elseif tv == "table" then
				local arr = {}
				for _, item in pairs(v) do
					if type(item) == "string" or type(item) == "number" or type(item) == "boolean" then
						arr[#arr + 1] = item
					end
				end
				if #arr > 0 then
					if type(arr[1]) == "string" then table.sort(arr) end
					out[k] = arr
				end
			end
		end
	end
	if out.type == "SkillType" then
		if type(out.skillType) == "number" and st_rev[out.skillType] then
			out.skillTypeName = st_rev[out.skillType]
		end
		if type(tag.skillTypeList) == "table" then
			local names, seen = {}, {}
			for _, id in pairs(tag.skillTypeList) do
				local nm = type(id) == "number" and st_rev[id] or nil
				if nm and not seen[nm] then
					seen[nm] = true
					names[#names + 1] = nm
				end
			end
			if #names > 0 then
				table.sort(names)
				out.skillTypeNames = names
			end
		end
	end
	return out
end

local function serialize_mod(m, depth, st_rev)
	if type(m) ~= "table" then return nil end
	depth = depth or 0
	st_rev = st_rev or reverse_skill_type_map()
	local flags = tonumber(m.flags) or 0
	local kw_flags = tonumber(m.keywordFlags) or 0
	local kw_match_all_bit = (type(KeywordFlag) == "table" and tonumber(KeywordFlag.MatchAll)) or 0
	local kw_match_all = kw_match_all_bit ~= 0 and bit.band(kw_flags, kw_match_all_bit) ~= 0
	local tags = {}
	for i = 1, #m do
		local st = serialize_tag(m[i], st_rev)
		if st then tags[#tags + 1] = st end
	end
	local val = sanitize_val(m.value, depth, function(sub, d) return serialize_mod(sub, d, st_rev) end)
	return {
		name = tostring(m.name or ""), type = tostring(m.type or ""), value = val,
		flags = flags, flagNames = decode_bitmask(flags, ModFlag),
		keywordFlags = kw_flags, keywordFlagNames = decode_bitmask(kw_flags, KeywordFlag, { MatchAll = true }),
		keywordMatchAll = kw_match_all, source = m.source and tostring(m.source) or nil, tags = tags,
	}
end

local function variant_active(item, modLine)
	if type(item) ~= "table" or type(modLine) ~= "table" then return true end
	if type(item.GetModLineVariantCount) == "function" then
		local ok, cnt = pcall(item.GetModLineVariantCount, item, modLine)
		if ok and type(cnt) == "number" then return cnt > 0 end
	end
	if type(item.CheckModLineVariant) == "function" then
		local ok, res = pcall(item.CheckModLineVariant, item, modLine)
		if ok then return res and true or false end
	end
	return true
end

local function collect_equipped_slots(build, include_inactive)
	local out = {}
	local items_tab = build.itemsTab
	if type(items_tab) ~= "table" or type(items_tab.slots) ~= "table" then return out end
	local active_ws = (items_tab.activeItemSet and items_tab.activeItemSet.useSecondWeaponSet) and 2 or 1
	local alloc_nodes = (build.spec and build.spec.allocNodes) or nil
	local names = {}
	for slot_name in pairs(items_tab.slots) do
		if type(slot_name) == "string" then names[#names + 1] = slot_name end
	end
	table.sort(names)
	for _, slot_name in ipairs(names) do
		local slot = items_tab.slots[slot_name]
		if type(slot) == "table" and tonumber(slot.selItemId) and tonumber(slot.selItemId) > 0 then
			local item = items_tab.items and items_tab.items[slot.selItemId]
			if type(item) == "table" then
				local swapped_out = slot.weaponSet and tonumber(slot.weaponSet) ~= active_ws
				local unalloc_jewel = slot.nodeId and alloc_nodes and not alloc_nodes[slot.nodeId]
				local hidden_slot = false
				if type(slot.IsShown) == "function" then
					local ok_sh, sh = pcall(slot.IsShown, slot)
					if ok_sh and sh == false then hidden_slot = true end
				end
				local is_flask = (slot_name:find("^Flask ") ~= nil)
					or (type(slot.controls) == "table" and slot.controls.activate ~= nil)
				local is_inactive = slot.inactive or swapped_out or unalloc_jewel or hidden_slot
				local is_active = (not is_inactive) and not (is_flask and not slot.active)
				if include_inactive or not is_inactive then
					out[#out + 1] = {
						slot = slot_name,
						item = item,
						active = is_active and true or false,
					}
				end
			end
		end
	end
	return out
end

local function append_custom_block(lines, text, title, src)
	for raw in text:gmatch("([^\n]*)\n?") do
		local clean = strip_clean(raw)
		if not is_ignorable_line(clean) and not clean:match("^#") and not clean:match("^//") then
			lines[#lines + 1] = { rawLine = clean, blockTitle = title, modSource = src }
		end
	end
end

local function collect_custom_mod_lines(build)
	local lines = {}
	local cfg_tab = build.configTab
	if type(cfg_tab) ~= "table" then return lines end
	local active_set = cfg_tab.configSets and cfg_tab.configSets[cfg_tab.activeConfigSetId]
	local has_block_text = false
	if type(active_set) == "table" and type(active_set.customModsList) == "table" then
		for _, block in ipairs(active_set.customModsList) do
			if type(block) == "table" and block.enabled ~= false and type(block.text) == "string" and #block.text > 0 then
				has_block_text = true
				local title = tostring(block.title or "Default")
				append_custom_block(lines, block.text, title, "Custom:" .. title)
			end
		end
	end
	if not has_block_text then
		local input = (type(active_set) == "table" and active_set.input) or cfg_tab.input
		local cm = type(input) == "table" and input.customMods
		if type(cm) == "string" and #cm > 0 then append_custom_block(lines, cm, "Default", "Custom") end
	end
	return lines
end

local function parse_raw_mod_line(clean, source, section, st_rev)
	local mods_out, unsupported, extra_str = {}, false, nil
	if type(modLib) == "table" and type(modLib.parseMod) == "function" then
		local ok, mods, extra = pcall(modLib.parseMod, clean)
		if not ok or not mods or extra then
			unsupported = true
			extra_str = strip_clean(type(extra) == "string" and extra or clean)
		end
		if ok and type(mods) == "table" then
			for _, m in ipairs(mods) do
				local sm = serialize_mod(m, 0, st_rev)
				if sm then
					if not sm.source and source then sm.source = source end
					mods_out[#mods_out + 1] = sm
				end
			end
		end
	else
		unsupported, extra_str = true, clean
	end
	return { rawLine = clean, section = section, unsupported = unsupported, extra = extra_str, mods = mods_out, source = source }
end

local function inspect_item_mod_lines(item, slot_name, st_rev)
	local out, mod_src = {}, tostring(item.modSource or ("Item:" .. slot_name))
	for _, sec in ipairs(MOD_SECTIONS) do
		local mod_lines = item[sec[2]]
		if type(mod_lines) == "table" then
			for _, ml in ipairs(mod_lines) do
				if type(ml) == "table" and not ml.disabled and variant_active(item, ml) then
					local clean = strip_clean(ml.line)
					if not is_ignorable_line(clean) then
						local extra_clean = (ml.extra ~= nil) and strip_clean(ml.extra) or ""
						if ml.extra ~= nil and extra_clean ~= "" then
							out[#out + 1] = { rawLine = clean, section = sec[1], unsupported = true, extra = extra_clean, mods = {}, source = mod_src }
						else
							local mods_out = {}
							if type(ml.modList) == "table" then
								for _, m in ipairs(ml.modList) do
									local sm = serialize_mod(m, 0, st_rev)
									if sm then
										if not sm.source then sm.source = mod_src end
										mods_out[#mods_out + 1] = sm
									end
								end
							end
							out[#out + 1] = { rawLine = clean, section = sec[1], unsupported = false, extra = nil, mods = mods_out, source = mod_src }
						end
					end
				end
			end
		end
	end
	return out
end

local function union_dot_flags(ms, out, mod_flags, kw_flags)
	local kw_match_all = (type(KeywordFlag) == "table" and tonumber(KeywordFlag.MatchAll)) or 0
	local kw_mask = bit.bnot(kw_match_all)
	local dot_mf, dot_kf = 0, 0
	local function add_mf(k)
		if type(ModFlag) == "table" and type(ModFlag[k]) == "number" then dot_mf = bit.bor(dot_mf, ModFlag[k]) end
	end
	local function add_kf(k)
		if type(KeywordFlag) == "table" and type(KeywordFlag[k]) == "number" then
			dot_kf = bit.bor(dot_kf, bit.band(KeywordFlag[k], kw_mask))
		end
	end
	local function add_cfg(c)
		if type(c) ~= "table" then return end
		if type(c.flags) == "number" then dot_mf = bit.bor(dot_mf, c.flags) end
		if type(c.keywordFlags) == "number" then dot_kf = bit.bor(dot_kf, bit.band(c.keywordFlags, kw_mask)) end
	end
	local ig = tonumber(out.IgniteDPS) or 0
	local po = tonumber(out.PoisonDPS) or 0
	local bl = tonumber(out.BleedDPS) or 0
	local dc = tonumber(out.DecayDPS) or 0
	local td = math.max(tonumber(out.TotalDotDPS) or 0, tonumber(out.TotalDot) or 0)
	if td > 0 or ig > 0 or po > 0 or bl > 0 or dc > 0 then
		add_mf("Dot")
		if td > 0 or dc > 0 then
			add_cfg(ms and ms.dotCfg)
			for _, dt in ipairs({ "Physical", "Fire", "Cold", "Lightning", "Chaos" }) do
				if (tonumber(out[dt .. "Dot"]) or 0) > 0 then
					add_kf(dt); add_kf(dt .. "Dot")
					add_cfg(ms and ms["dot" .. dt .. "Cfg"])
				end
			end
		end
		if dc > 0 then add_kf("Chaos"); add_kf("ChaosDot") end
	end
	if ig > 0 then
		add_mf("Ailment"); add_kf("Ignite"); add_kf("FireDot"); add_kf("Ailment"); add_kf("Fire")
		add_cfg(ms and ms.igniteCfg); add_cfg(ms and ms.OHigniteCfg)
	end
	if po > 0 then
		add_mf("Ailment"); add_kf("Poison"); add_kf("Ailment"); add_kf("Chaos")
		add_cfg(ms and ms.poisonCfg); add_cfg(ms and ms.OHpoisonCfg)
	end
	if bl > 0 then
		add_mf("Ailment"); add_kf("Bleed"); add_kf("PhysicalDot"); add_kf("Ailment"); add_kf("Physical")
		add_cfg(ms and ms.bleedCfg); add_cfg(ms and ms.OHbleedCfg)
	end
	return bit.bor(mod_flags, dot_mf), bit.bor(kw_flags, dot_kf), dot_mf, dot_kf
end

local function extract_skill_snapshot(ms, out, st_rev)
	local ge = ms and ms.activeEffect and ms.activeEffect.grantedEffect or {}
	local cfg = (ms and ms.skillCfg) or {}
	local sf = (ms and ms.skillFlags) or {}
	local st = (ms and ms.skillTypes) or {}
	local base_mf = tonumber(cfg.flags) or 0
	local base_kf = tonumber(cfg.keywordFlags) or 0
	local mod_flags, kw_flags, dot_mf, dot_kf = union_dot_flags(ms, out or {}, base_mf, base_kf)
	local sf_list, st_list = {}, {}
	for k, v in pairs(sf) do if type(k) == "string" and v == true then sf_list[#sf_list + 1] = k end end
	table.sort(sf_list)
	for id, v in pairs(st) do if v and st_rev[id] then st_list[#st_list + 1] = st_rev[id] end end
	table.sort(st_list)
	local dmg_types = {}
	for _, dc in ipairs({
		{ "Physical", "Physical", "BleedDPS" },
		{ "Fire", "Fire", "IgniteDPS" },
		{ "Cold", "Cold", nil },
		{ "Lightning", "Lightning", nil },
		{ "Chaos", "Chaos", "PoisonDPS" },
	}) do
		local nm, st_key, dot_key = dc[1], dc[2], dc[3]
		local has_hit = (tonumber((out or {})[nm .. "HitAverage"]) or 0) > 0
			or (tonumber((out or {})[nm .. "Max"]) or 0) > 0
			or (tonumber((out or {})[nm .. "Average"]) or 0) > 0
		local has_dot = (dot_key and (tonumber((out or {})[dot_key]) or 0) > 0)
			or (tonumber((out or {})[nm .. "Dot"]) or 0) > 0
		local has_st = type(SkillType) == "table" and SkillType[st_key] and st[SkillType[st_key]]
		if has_hit or has_dot or has_st then dmg_types[#dmg_types + 1] = nm end
	end
	return {
		skillName = tostring(ge.name or cfg.skillName or ""),
		skillId = tostring(ge.id or ""),
		castTime = tonumber(ge.castTime) or 0,
		speed = tonumber((out or {}).Speed) or 0,
		skillFlags = sf_list,
		skillTypes = st_list,
		mainModFlags = mod_flags,
		mainModFlagNames = decode_bitmask(mod_flags, ModFlag),
		dotModFlags = dot_mf,
		dotModFlagNames = decode_bitmask(dot_mf, ModFlag),
		offhandModFlags = ms and ms.weapon2Cfg and tonumber(ms.weapon2Cfg.flags) or nil,
		mainKeywordFlags = kw_flags,
		mainKeywordFlagNames = decode_bitmask(kw_flags, KeywordFlag, { MatchAll = true }),
		dotKeywordFlags = dot_kf,
		dotKeywordFlagNames = decode_bitmask(dot_kf, KeywordFlag, { MatchAll = true }),
		damageTypes = dmg_types,
	}
end

local function check_mod_flag(ms, mod_db, cfg, flag_name)
	local sml = ms and ms.skillModList
	if sml and type(sml.Flag) == "function" then
		local ok1, r1 = pcall(sml.Flag, sml, cfg, flag_name)
		if ok1 and r1 then return true end
		local ok2, r2 = pcall(sml.Flag, sml, nil, flag_name)
		if ok2 and r2 then return true end
	end
	if mod_db and type(mod_db.Flag) == "function" then
		local ok3, r3 = pcall(mod_db.Flag, mod_db, cfg, flag_name)
		if ok3 and r3 then return true end
		local ok4, r4 = pcall(mod_db.Flag, mod_db, nil, flag_name)
		if ok4 and r4 then return true end
	end
	return false
end

local function extract_main_skill(build, st_rev)
	local env = (build.calcsTab and build.calcsTab.mainEnv) or {}
	local player = env.player or {}
	local ms = player.mainSkill
	local cfg = (ms and ms.skillCfg) or {}
	local sd = (ms and ms.skillData) or {}
	local snap = extract_skill_snapshot(ms, player.output or {}, st_rev)
	local is_minion = (env.minion ~= nil) or (ms and ms.skillFlags and ms.skillFlags.minion == true) or false
	snap.isMinion = is_minion
	local ss = (ms and ms.summonSkill) or (env.minion and env.minion.mainSkill and env.minion.mainSkill.summonSkill)
	local ss_ge = ss and ss.activeEffect and ss.activeEffect.grantedEffect
	local ss_name = (ss_ge and ss_ge.name) or cfg.summonSkillName
	if type(ss_name) == "string" and ss_name ~= "" then snap.summonSkillName = ss_name end
	if env.minion and env.minion.mainSkill then
		snap.minionSkill = extract_skill_snapshot(env.minion.mainSkill, env.minion.output or {}, st_rev)
	end
	snap.conversions = {
		spellDamageAppliesToAttacks = check_mod_flag(ms, player.modDB, cfg, "SpellDamageAppliesToAttacks"),
		castSpeedAppliesToAttacks = check_mod_flag(ms, player.modDB, cfg, "CastSpeedAppliesToAttacks"),
		minionDamageAppliesToPlayer = check_mod_flag(ms, player.modDB, cfg, "MinionDamageAppliesToPlayer"),
		minionAttackSpeedAppliesToPlayer = check_mod_flag(ms, player.modDB, cfg, "MinionAttackSpeedAppliesToPlayer"),
		minionCastSpeedAppliesToPlayer = check_mod_flag(ms, player.modDB, cfg, "MinionCastSpeedAppliesToPlayer"),
		clawDamageAppliesToUnarmed = check_mod_flag(ms, player.modDB, cfg, "ClawDamageAppliesToUnarmed"),
		dotIsSpell = sd.dotIsSpell and true or false,
		dotIsAttack = sd.dotIsAttack and true or false,
	}
	return snap
end

local function collect_keystones(build, st_rev)
	local out = {}
	local alloc = (build.spec and build.spec.allocNodes) or {}
	for _, node in pairs(alloc) do
		if type(node) == "table" and node.type == "Keystone" and node.dn then
			local mods = {}
			if type(node.modList) == "table" then
				for _, m in ipairs(node.modList) do
					local sm = serialize_mod(m, 0, st_rev)
					if sm then mods[#mods + 1] = sm end
				end
			end
			local stats = {}
			for _, s in ipairs(node.sd or {}) do
				local cl = strip_clean(s)
				if cl ~= "" then stats[#stats + 1] = cl end
			end
			out[#out + 1] = { name = strip_clean(node.dn), stats = stats, mods = mods }
		end
	end
	table.sort(out, function(a, b) return a.name < b.name end)
	return out
end

local function handle_scan_unsupported_mods(params)
	local build, t0 = get_build(), now_ms()
	local include_inactive = type(params) == "table" and params.includeInactive == true
	local items_out, custom_out = {}, {}
	local ok_eq, eq_slots = pcall(collect_equipped_slots, build, include_inactive)
	if ok_eq and type(eq_slots) == "table" then
		for _, entry in ipairs(eq_slots) do
			local item = entry.item
			local item_name = strip_clean(item.title or item.name or "")
			local base_name = strip_clean(item.baseName or "")
			local mod_src = tostring(item.modSource or ("Item:" .. entry.slot))
			for _, sec in ipairs(MOD_SECTIONS) do
				local mod_lines = item[sec[2]]
				if type(mod_lines) == "table" then
					for _, ml in ipairs(mod_lines) do
						if type(ml) == "table" and not ml.disabled and variant_active(item, ml) and ml.extra ~= nil then
							local clean, extra = strip_clean(ml.line), strip_clean(ml.extra)
							if not is_ignorable_line(clean) and extra ~= "" then
								items_out[#items_out + 1] = {
									slot = entry.slot, itemName = item_name, baseName = base_name, section = sec[1],
									rawLine = clean, extra = extra, modSource = mod_src, active = entry.active,
								}
							end
						end
					end
				end
			end
		end
	end
	local ok_cm, cm_lines = pcall(collect_custom_mod_lines, build)
	if ok_cm and type(cm_lines) == "table" then
		for _, cm in ipairs(cm_lines) do
			local ok_p, mods, extra = pcall(function() return modLib.parseMod(cm.rawLine) end)
			if not ok_p or not mods or extra then
				custom_out[#custom_out + 1] = {
					slot = "ConfigTab", itemName = "Custom Modifiers (" .. cm.blockTitle .. ")",
					blockTitle = cm.blockTitle, section = "customMods", rawLine = cm.rawLine,
					extra = strip_clean(type(extra) == "string" and extra or cm.rawLine),
					modSource = cm.modSource, active = true,
				}
			end
		end
	end
	return { items = items_out, customMods = custom_out, totalCount = #items_out + #custom_out, elapsedMs = now_ms() - t0 }
end

local function handle_inspect_mod_interactions(params)
	local build, t0 = get_build(), now_ms()
	params = type(params) == "table" and params or {}
	local st_rev = reverse_skill_type_map()
	local ok_ms, main_skill = pcall(extract_main_skill, build, st_rev)
	if not ok_ms then main_skill = { skillName = "", skillFlags = {}, mainModFlags = 0, mainKeywordFlags = 0, damageTypes = {} } end
	local res = { mainSkill = main_skill, lines = {}, elapsedMs = 0 }
	local has_lines = type(params.lines) == "table" and #params.lines > 0
	local has_slot = type(params.slot) == "string" and params.slot ~= ""
	if has_lines then
		for _, raw in ipairs(params.lines) do
			local clean = strip_clean(tostring(raw or ""))
			if clean ~= "" then res.lines[#res.lines + 1] = parse_raw_mod_line(clean, "input", "input", st_rev) end
		end
	end
	if has_slot then
		local items_tab, want_low = build.itemsTab, params.slot:lower()
		local matched_name, matched_slot = nil, nil
		if type(items_tab) == "table" and type(items_tab.slots) == "table" then
			if items_tab.slots[params.slot] then
				matched_name, matched_slot = params.slot, items_tab.slots[params.slot]
			else
				for sn, s in pairs(items_tab.slots) do
					if type(sn) == "string" and sn:lower() == want_low then matched_name, matched_slot = sn, s; break end
				end
			end
		end
		if not matched_slot then error("Equipment slot not found: " .. tostring(params.slot), 0) end
		local item = matched_slot.selItemId and items_tab.items and items_tab.items[matched_slot.selItemId]
		res.slot = matched_name
		if type(item) == "table" then
			res.itemName = strip_clean(item.title or item.name or "")
			res.baseName = strip_clean(item.baseName or "")
			local ok_il, slot_lines = pcall(inspect_item_mod_lines, item, matched_name, st_rev)
			res.slotLines = ok_il and slot_lines or {}
		else
			res.itemName, res.baseName, res.slotLines = "", "", {}
		end
	end
	if (not has_lines and not has_slot) or params.scanEquipped == true then
		local equipped_out = {}
		local ok_eq, eq_slots = pcall(collect_equipped_slots, build, params.includeInactive == true)
		if ok_eq and type(eq_slots) == "table" then
			for _, entry in ipairs(eq_slots) do
				local ok_il, s_lines = pcall(inspect_item_mod_lines, entry.item, entry.slot, st_rev)
				equipped_out[#equipped_out + 1] = {
					slot = entry.slot, itemName = strip_clean(entry.item.title or entry.item.name or ""),
					baseName = strip_clean(entry.item.baseName or ""), active = entry.active,
					lines = ok_il and s_lines or {},
				}
			end
		end
		res.equippedSlots = equipped_out
		local cm_out = {}
		local ok_cm, cm_lines = pcall(collect_custom_mod_lines, build)
		if ok_cm and type(cm_lines) == "table" then
			for _, cm in ipairs(cm_lines) do
				local parsed = parse_raw_mod_line(cm.rawLine, cm.modSource, "customMods", st_rev)
				parsed.blockTitle = cm.blockTitle
				cm_out[#cm_out + 1] = parsed
			end
		end
		res.customModLines = cm_out
		local ok_ks, keystones = pcall(collect_keystones, build, st_rev)
		res.keystones = ok_ks and keystones or {}
	end
	res.elapsedMs = now_ms() - t0
	return res
end

M.rpc_handlers = {
	scan_unsupported_mods = handle_scan_unsupported_mods,
	inspect_mod_interactions = handle_inspect_mod_interactions,
}
return M
