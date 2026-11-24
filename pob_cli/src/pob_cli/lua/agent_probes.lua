-- Zero-intrusion agent-native PoB probes: agent_probe_build, eval_scenario_envelope, compute_grad_build.
local calcs = require("Modules.Calcs")
local M = {}

local RESISTS = { "Fire", "Cold", "Lightning", "Chaos" }
local ATTRS = { "Str", "Dex", "Int" }
local GUARD_NAMES = {
	["Molten Shell"] = true, ["Steelskin"] = true, ["Immortal Call"] = true,
	["Bone Armour"] = true, ["Arcane Cloak"] = true,
}
local RES_MOD_MAP = {
	FireResist = { "Fire" }, ColdResist = { "Cold" }, LightningResist = { "Lightning" },
	ChaosResist = { "Chaos" }, ElementalResist = { "Fire", "Cold", "Lightning" },
}
local RES_MAX_MAP = {
	FireResistMax = { "Fire" }, ColdResistMax = { "Cold" }, LightningResistMax = { "Lightning" },
	ChaosResistMax = { "Chaos" }, ElementalResistMax = { "Fire", "Cold", "Lightning" },
}
local CORE_DIMS = {
	"flat_phys", "flat_fire", "flat_cold", "flat_light", "flat_chaos",
	"inc_damage", "inc_spell_dmg", "inc_attack_dmg", "inc_crit_chance", "crit_multi", "inc_speed",
	"gem_level", "minus_enemy_res", "flat_life", "flat_es", "flat_armour", "flat_evasion",
	"inc_life", "inc_es", "flat_mana",
}
local ALL_DIMS = {}
for i, d in ipairs(CORE_DIMS) do ALL_DIMS[i] = d end
ALL_DIMS[21], ALL_DIMS[22], ALL_DIMS[23], ALL_DIMS[24] = "fire_res", "cold_res", "light_res", "chaos_res"

local function now_ms()
	if type(HostNowMs) == "function" then return HostNowMs() end
	if type(GetTime) == "function" then return GetTime() end
	return 0
end

local function get_build()
	local m = main or __mainObject__
	local b = m and m.modes and m.modes["BUILD"]
	if not b then error("No active PoB build loaded", 0) end
	if b.buildFlag and b.calcsTab and b.calcsTab.BuildOutput then
		b.calcsTab:BuildOutput(); b.buildFlag = false
	end
	return b
end

local function primary_dps(out)
	if (out.FullDPS or 0) > 0 then return out.FullDPS, "FullDPS" end
	return out.CombinedDPS or out.TotalDPS or 0, "CombinedDPS"
end

local function sum_item_attr_res(item, slot_num, mod_db)
	local src_list = item.modList or (item.slotModList and item.slotModList[slot_num])
	local base_attr = { Str = 0, Dex = 0, Int = 0 }
	local inc_attr = { Str = 0, Dex = 0, Int = 0 }
	local base_res = { Fire = 0, Cold = 0, Lightning = 0, Chaos = 0 }
	local base_res_max = { Fire = 0, Cold = 0, Lightning = 0, Chaos = 0 }
	if not src_list then return base_attr, inc_attr, base_res, base_res_max end

	for _, mod in ipairs(src_list) do
		local val = mod.value
		if type(val) == "number" and (mod.flags or 0) == 0 and (mod.keywordFlags or 0) == 0 then
			local cond_ok = true
			for _, tag in ipairs(mod) do
				if tag.type == "Condition" then
					local active = mod_db:GetCondition(tag.var)
					if tag.neg then active = not active end
					if not active then cond_ok = false; break end
				elseif tag.type == "Multiplier" or tag.type == "PerStat" or tag.type == "SlotName" then
					cond_ok = false; break
				end
			end
			if cond_ok then
				local n, t = mod.name, mod.type
				if t == "BASE" then
					if base_attr[n] ~= nil then
						base_attr[n] = base_attr[n] + val
					elseif RES_MOD_MAP[n] then
						for _, e in ipairs(RES_MOD_MAP[n]) do base_res[e] = base_res[e] + val end
					elseif RES_MAX_MAP[n] then
						for _, e in ipairs(RES_MAX_MAP[n]) do base_res_max[e] = base_res_max[e] + val end
					end
				elseif t == "INC" and inc_attr[n] ~= nil then
					inc_attr[n] = inc_attr[n] + val
				end
			end
		end
	end
	return base_attr, inc_attr, base_res, base_res_max
end

local function json_obj(t) return setmetatable(t or {}, { __jsontype = "object" }) end
local function make_set(csv)
	local s = {}
	for k in csv:gmatch("[^|]+") do s[k] = 1 end
	return s
end

local CROSS_TRIG = make_set("cospri's malice|mjolner|poet's pen|spellslinger|asenath's chant|manaforged arrows|maloney's mechanism")
local LINKED_TRIG = {
	["cast on critical strike"] = "atk", ["cast while channelling"] = "chan",
	["prismatic burst"] = "atk", ["shockwave"] = "atk", ["void shockwave"] = "atk", ["falling crystal"] = "atk",
}
local EXCL_TRIG = make_set(
	"automation|autoexertion|call to arms|cast when damage taken|cast when stunned|cast on ward break|"
	.. "cast on death|cast on melee kill|vengeance|riposte|reckoning|counterattack|mark on hit|hextouch|"
	.. "arcanist brand|focus|tempest shield|shattershard|svalinn cast on block|festering resentment cast on block|"
	.. "battlemage's cry|combust|seize the flesh|intuitive link|avenging flame|hex on trap|snipe|trigger craft|"
	.. "hidden blade|unseen strike|kitava's thirst|foulborn kitava's thirst"
)

local function norm_trig(name)
	local s = tostring(name or ""):lower():gsub("^awakened%s+", ""):gsub("%s+support$", "")
	return (s:gsub("^the%s+", ""):gsub("ö", "o"))
end

local function is_driver(sk, mode)
	if not sk or sk.triggeredBy or (sk.skillFlags and sk.skillFlags.disable) then return false end
	local atk = (sk.skillTypes and SkillType and sk.skillTypes[SkillType.Attack]) or (sk.skillFlags and sk.skillFlags.attack) or false
	local ch = (sk.skillTypes and SkillType and sk.skillTypes[SkillType.Channel]) or (sk.skillData and sk.skillData.triggerTime ~= nil) or false
	return (mode == "chan" and ch) or (mode == "atk" and atk) or (mode == nil and (atk or ch))
end

local function is_excl(sk, nt)
	local sd = sk and sk.skillData
	return EXCL_TRIG[nt] or (sd and (sd.triggeredByDamageTaken or sd.triggeredByBrand or sd.triggeredByAutoexertion or sd.triggeredByCraft)) or false
end

local function analyze_trigger_cliffs(env)
	local cliffs, seen, group_count, group_drv = {}, {}, {}, {}
	local out = env.player.output or {}
	local tick_t = (data and data.misc and data.misc.ServerTickTime) or 0.033
	local tick_r = (data and data.misc and data.misc.ServerTickRate) or (1 / tick_t)
	local list = env.player.activeSkillList or {}
	for _, sk in ipairs(list) do
		if sk.socketGroup and not (sk.skillFlags and sk.skillFlags.disable) then
			if sk.triggeredBy then
				local nt = norm_trig(sk.triggeredBy.grantedEffect and sk.triggeredBy.grantedEffect.name)
				if not is_excl(sk, nt) then
					local gk = tostring(sk.socketGroup) .. ":" .. nt
					group_count[gk] = (group_count[gk] or 0) + 1
				end
			elseif is_driver(sk) then
				group_drv[sk.socketGroup] = true
			end
		end
	end
	local cache = GlobalCache and GlobalCache.cachedData and (GlobalCache.cachedData[env.mode] or GlobalCache.cachedData["MAIN"])
	local function drv_sp(sk, mode)
		if mode == "chan" and sk.skillData and (sk.skillData.triggerTime or 0) > 0 then return 1 / sk.skillData.triggerTime end
		if cache and type(cacheSkillUUID) == "function" then
			local e = cache[cacheSkillUUID(sk, env)]
			local sp = e and (e.HitSpeed or e.Speed)
			if sp and sp > 0 then return sp end
		end
		return (sk == env.player.mainSkill and (out.Speed or 0) > 0) and out.Speed or 0
	end
	local function find_source_aps(trig_sk, nt)
		local mode = LINKED_TRIG[nt] or (CROSS_TRIG[nt] and "atk" or nil)
		if trig_sk == env.player.mainSkill and (out.EffectiveSourceRate or 0) > 0 then return out.EffectiveSourceRate end
		for _, sk in ipairs(list) do
			if sk.socketGroup == trig_sk.socketGroup and sk ~= trig_sk and is_driver(sk, mode) then
				local sp = drv_sp(sk, mode)
				if sp > 0 then return sp end
			end
		end
		if CROSS_TRIG[nt] then
			if is_driver(env.player.mainSkill, "atk") and (out.Speed or 0) > 0 then return out.Speed end
			local best = 0
			for _, sk in ipairs(list) do
				if is_driver(sk, "atk") then best = math.max(best, drv_sp(sk, "atk")) end
			end
			return best
		end
		return 0
	end

	for _, skill in ipairs(list) do
		if skill.triggeredBy and not (skill.skillFlags and skill.skillFlags.disable) then
			local ge = skill.activeEffect and skill.activeEffect.grantedEffect
			local sname = (ge and ge.name) or "TriggeredSkill"
			local tname = (skill.triggeredBy.grantedEffect and skill.triggeredBy.grantedEffect.name) or "Trigger"
			local nt = norm_trig(tname)
			local key = tostring(skill.socketGroup) .. ":" .. sname
			if not seen[key] and not is_excl(skill, nt) and (CROSS_TRIG[nt] or LINKED_TRIG[nt] ~= nil or group_drv[skill.socketGroup]) then
				seen[key] = true
				local lv = skill.triggeredBy.level or 1
				local levels = skill.triggeredBy.grantedEffect and skill.triggeredBy.grantedEffect.levels
				local trig_cd = (levels and levels[lv] and levels[lv].cooldown) or 0
				local sk_cd = (skill.skillData and skill.skillData.cooldown) or 0
				local icdr = calcLib.mod(skill.skillModList, skill.skillCfg, "CooldownRecovery") or 1
				local add_cd = skill.skillModList:Sum("BASE", skill.skillCfg, "CooldownRecovery") or 0
				local cd_ov = skill.skillModList:Override(skill.skillCfg, "CooldownRecovery")
				local add_cast = skill.skillModList:Flag(skill.skillCfg, "SpellCastTimeAddedToCooldownIfTriggered") and ((ge and ge.castTime) or 0) or 0
				local act_cd = cd_ov or math.max(trig_cd + add_cast, sk_cd + add_cd)
				if act_cd > 0 then
					local eff_cd = cd_ov or (act_cd / icdr)
					local cur_t = math.ceil(eff_cd * tick_r)
					local tick_cd = cur_t / tick_r
					local cap = 1 / tick_cd
					local nxt_t = math.max(1, cur_t - 1)
					local nxt_cd = nxt_t / tick_r
					local cur_icdr = math.floor((icdr - 1) * 100 + 0.5)
					local ext_icdr, tot_icdr = nil, nil
					if not cd_ov and cur_t > 1 then
						tot_icdr = math.ceil((act_cd / nxt_cd - 1) * 100)
						while math.ceil((act_cd / (1 + tot_icdr / 100)) * tick_r) >= cur_t do tot_icdr = tot_icdr + 1 end
						ext_icdr = tot_icdr - cur_icdr
					end
					local linked = group_count[tostring(skill.socketGroup) .. ":" .. nt] or 1
					local src_aps = find_source_aps(skill, nt)
					local max_aps = cap * linked
					cliffs[#cliffs + 1] = {
						skillName = sname, triggerName = tname, baseActionCd = act_cd,
						currentIcdrPct = cur_icdr, effectiveCd = eff_cd, serverTickTime = tick_t,
						currentTicks = cur_t, tickRoundedCd = tick_cd, triggerRateCap = cap,
						nextTicks = nxt_t, nextTriggerRateCap = 1 / nxt_cd,
						totalIcdrPctNeeded = tot_icdr, extraIcdrPctNeeded = ext_icdr,
						linkedTriggeredSpells = linked, sourceAps = src_aps, maxSafeAps = max_aps,
						apsHeadroom = max_aps - src_aps, overspeedCliffExceeded = src_aps > max_aps,
						reportedSkillTriggerRate = (skill == env.player.mainSkill) and out.SkillTriggerRate
							or (skill.skillData and skill.skillData.triggerRate) or nil,
					}
				end
			end
		end
	end
	return cliffs
end

local function handle_agent_probe_build(_params)
	local build, t0 = get_build(), now_ms()
	local env = build.calcsTab.mainEnv
	local output = (env and env.player and env.player.output) or {}
	local mod_db = env.player.modDB
	local resist_slack, res_total, res_cap, res_inc = {}, {}, {}, {}
	local max_cap = (data and data.misc and data.misc.MaxResistCap) or 90
	for _, elem in ipairs(RESISTS) do
		local is_ele = elem ~= "Chaos"
		local cap = mod_db:Override(nil, elem .. "ResistMax")
			or math.min(max_cap, mod_db:Sum("BASE", nil, elem .. "ResistMax", is_ele and "ElementalResistMax" or nil))
		local tot = output[elem .. "ResistTotal"] or 0
		res_cap[elem], res_total[elem] = math.modf(cap), tot
		res_inc[elem] = math.max(calcLib.mod(mod_db, nil, elem .. "Resist", is_ele and "ElementalResist" or nil), 0)
		resist_slack[elem] = { final = output[elem .. "Resist"] or 0, total = tot, cap = res_cap[elem], overcapSlack = tot - res_cap[elem] }
	end
	local attr_cur = { Str = output.Str or 0, Dex = output.Dex or 0, Int = output.Int or 0 }
	local attr_req = { Str = output.ReqStr or 0, Dex = output.ReqDex or 0, Int = output.ReqInt or 0 }
	local attr_slack, attr_scale = {}, {}
	for _, a in ipairs(ATTRS) do
		attr_slack[a] = { current = attr_cur[a], required = attr_req[a], slack = attr_cur[a] - attr_req[a] }
		attr_scale[a] = math.max(calcLib.mod(mod_db, nil, a), 0.01)
	end
	local resource_slack = {
		life = output.Life or 0, lifeUnreserved = output.LifeUnreserved or 0, lifeUnreservedPct = output.LifeUnreservedPercent or 0,
		mana = output.Mana or 0, manaUnreserved = output.ManaUnreserved or 0, manaUnreservedPct = output.ManaUnreservedPercent or 0,
		energyShield = output.EnergyShield or 0,
	}
	local req_mult = calcLib.mod(mod_db, nil, "GlobalAttributeRequirements")
	local ignore_req = mod_db:Flag(nil, "IgnoreAttributeRequirements")
	local req_table = env.requirementsTable or {}
	local load_bearing, slots_inspected = {}, 0

	for slot_name, item in pairs(env.player.itemList or {}) do
		if not slot_name:match("^Flask") and not slot_name:match("^Tincture") then
			slots_inspected = slots_inspected + 1
			local slot_obj = build.itemsTab.slots[slot_name]
			local b_attr, i_attr, b_res, b_res_max = sum_item_attr_res(item, (slot_obj and slot_obj.slotNum) or 1, mod_db)
			local new_attr, bricked = {}, {}
			for _, a in ipairs(ATTRS) do
				local post_scale = math.max(attr_scale[a] - (i_attr[a] / 100), 0)
				new_attr[a] = math.floor(math.max(0, (attr_cur[a] / attr_scale[a]) - b_attr[a]) * post_scale + 1e-6)
			end
			if not ignore_req then
				for _, req in ipairs(req_table) do
					if not (req.source == "Item" and req.sourceSlot == slot_name) then
						local missing = nil
						for _, a in ipairs(ATTRS) do
							local r_val = (req[a] and req[a] > 0) and math.floor(req[a] * req_mult) or 0
							if attr_cur[a] >= r_val and r_val > new_attr[a] then
								missing = missing or {}
								missing[a] = { required = r_val, afterRemoval = new_attr[a], deficit = new_attr[a] - r_val }
							end
						end
						if missing then
							local sg = req.sourceGem
							local label = (req.source == "Item" and req.sourceItem and (req.sourceItem.name .. " [" .. (req.sourceSlot or "") .. "]"))
								or (req.source == "Gem" and sg and (sg.nameSpec .. " " .. tostring(sg.level) .. "/" .. tostring(sg.quality))) or "Unknown"
							bricked[#bricked + 1] = { source = req.source, name = label, missing = missing }
						end
					end
				end
			end
			local res_deficits = json_obj({})
			for _, elem in ipairs(RESISTS) do
				if b_res[elem] > 0 then
					local post_tot = math.modf(res_total[elem] - b_res[elem] * res_inc[elem])
					local post_cap = res_cap[elem] - b_res_max[elem]
					if post_tot < post_cap then
						res_deficits[elem] = { beforeSlack = res_total[elem] - res_cap[elem], afterSlack = post_tot - post_cap, lost = b_res[elem] }
					end
				end
			end
			if #bricked > 0 or next(res_deficits) ~= nil then
				load_bearing[#load_bearing + 1] = {
					slot = slot_name, itemName = item.name or item.title or "Item",
					attrDelta = b_attr, brickedCount = #bricked, bricked = bricked, resistDeficits = res_deficits,
				}
			end
		end
	end
	table.sort(load_bearing, function(a, b) return a.slot < b.slot end)
	return {
		resistSlack = resist_slack, attrSlack = attr_slack, resourceSlack = resource_slack,
		loadBearingGear = load_bearing, triggerCliffs = analyze_trigger_cliffs(env),
		slotsInspected = slots_inspected, elapsedMs = now_ms() - t0,
	}
end

local ENVELOPE_SCENARIOS = {
	{ name = "Raw", flasks = true, guards = true, overlay = {
		enemyIsBoss = "None", conditionKilledRecently = true, conditionHitRecently = true,
		buffOnslaught = true, usePowerCharges = true, useFrenzyCharges = true, useEnduranceCharges = true,
	} },
	{ name = "PinnacleSustained", flasks = false, guards = false, overlay = {
		enemyIsBoss = "Pinnacle", conditionKilledRecently = false, buffOnslaught = false,
	} },
	{ name = "Burst", flasks = true, guards = true, overlay = {
		enemyIsBoss = "Pinnacle", conditionKilledRecently = false, conditionHitRecently = true,
		buffOnslaught = true, usePowerCharges = true, useFrenzyCharges = true, useEnduranceCharges = true,
	} },
	{ name = "WorstHit", flasks = false, guards = false, overlay = {
		enemyIsBoss = "Uber", enemyDamageRollRange = 100, conditionKilledRecently = false, buffOnslaught = false,
	} },
}

local function handle_eval_scenario_envelope(_params)
	local build = get_build()
	local cfg_input, items_tab = build.configTab.input, build.itemsTab
	local saved_cfg, saved_flasks, saved_guards = {}, {}, {}
	for k, v in pairs(cfg_input) do saved_cfg[k] = v end
	for i = 1, 5 do
		local slot = items_tab.slots["Flask " .. i]
		if slot then saved_flasks[i] = slot.active end
	end
	for gi, grp in ipairs(build.skillsTab.socketGroupList or {}) do
		for gemi, gem in ipairs(grp.gemList or {}) do
			if GUARD_NAMES[gem.nameSpec] then
				saved_guards[#saved_guards + 1] = { gi = gi, gemi = gemi, enabled = gem.enabled }
			end
		end
	end
	local function set_flasks_guards(fl_on, grd_on)
		for i = 1, 5 do
			local slot = items_tab.slots["Flask " .. i]
			local it = slot and slot.selItemId and slot.selItemId > 0 and items_tab.items[slot.selItemId]
			if it and it.base and it.base.subType == "Utility" then slot.active = fl_on end
		end
		for _, g in ipairs(saved_guards) do
			build.skillsTab.socketGroupList[g.gi].gemList[g.gemi].enabled = grd_on
		end
	end
	local function restore_all()
		for k in pairs(cfg_input) do cfg_input[k] = nil end
		for k, v in pairs(saved_cfg) do cfg_input[k] = v end
		for i, st in pairs(saved_flasks) do
			if items_tab.slots["Flask " .. i] then items_tab.slots["Flask " .. i].active = st end
		end
		for _, g in ipairs(saved_guards) do
			build.skillsTab.socketGroupList[g.gi].gemList[g.gemi].enabled = g.enabled
		end
		build.configTab:BuildModList()
		build.calcsTab:BuildOutput()
	end

	local t0, out_scenarios = now_ms(), {}
	local ok, err = pcall(function()
		for _, sc in ipairs(ENVELOPE_SCENARIOS) do
			local ts = now_ms()
			for k in pairs(cfg_input) do cfg_input[k] = nil end
			for k, v in pairs(saved_cfg) do cfg_input[k] = v end
			for k, v in pairs(sc.overlay) do cfg_input[k] = v end
			set_flasks_guards(sc.flasks, sc.guards)
			build.configTab:BuildModList()
			local out = (calcs.buildOutput(build, "MAIN").player.output) or {}
			local dps, dps_key = primary_dps(out)
			out_scenarios[sc.name] = {
				dps = dps, dpsKey = dps_key, combinedDps = out.CombinedDPS or 0, fullDps = out.FullDPS or 0,
				totalEhp = out.TotalEHP or 0, physMaxHit = out.PhysicalMaximumHitTaken or 0,
				eleMinMaxHit = math.min(out.FireMaximumHitTaken or 0, out.ColdMaximumHitTaken or 0, out.LightningMaximumHitTaken or 0),
				chaosMaxHit = out.ChaosMaximumHitTaken or 0, elapsedMs = now_ms() - ts,
			}
		end
	end)
	restore_all()
	if not ok then error(err, 0) end

	local raw_dps = (out_scenarios.Raw and out_scenarios.Raw.dps) or 0
	local pin_dps = (out_scenarios.PinnacleSustained and out_scenarios.PinnacleSustained.dps) or 0
	return { scenarios = out_scenarios, inflationRatio = (pin_dps > 0) and (raw_dps / pin_dps) or 0, elapsedMs = now_ms() - t0 }
end

local FLAT_ELEM_SPECS = {
	flat_phys = { "PhysicalMin", "PhysicalMax" }, flat_fire = { "FireMin", "FireMax" },
	flat_cold = { "ColdMin", "ColdMax" }, flat_light = { "LightningMin", "LightningMax" },
	flat_chaos = { "ChaosMin", "ChaosMax" },
}
local DEF_MOD_SPECS = {
	flat_life = { "Life", "BASE", 50 }, flat_es = { "EnergyShield", "BASE", 50 },
	flat_armour = { "Armour", "BASE", 200 }, flat_evasion = { "Evasion", "BASE", 200 },
	inc_life = { "Life", "INC", 10 }, inc_es = { "EnergyShield", "INC", 10 },
	flat_mana = { "Mana", "BASE", 50 }, fire_res = { "FireResist", "BASE", 20 },
	cold_res = { "ColdResist", "BASE", 20 }, light_res = { "LightningResist", "BASE", 20 },
	chaos_res = { "ChaosResist", "BASE", 20 },
}

local function build_probe_mods(dim_name, is_minion)
	local ml = new("ModList"):ModList()
	local function add_off(name, mtype, val, flags)
		if is_minion then
			ml:NewMod("MinionModifier", "LIST", { mod = modLib.createMod(name, mtype, val, "GradProbe", flags or 0) }, "GradProbe")
		else
			ml:NewMod(name, mtype, val, "GradProbe", flags or 0)
		end
	end
	if FLAT_ELEM_SPECS[dim_name] then
		local pair = FLAT_ELEM_SPECS[dim_name]
		add_off(pair[1], "BASE", 10); add_off(pair[2], "BASE", 10)
		return ml, 10
	elseif DEF_MOD_SPECS[dim_name] then
		local sp = DEF_MOD_SPECS[dim_name]
		ml:NewMod(sp[1], sp[2], sp[3], "GradProbe")
		return ml, sp[3]
	elseif dim_name == "inc_damage" then add_off("Damage", "INC", 10); return ml, 10
	elseif dim_name == "inc_spell_dmg" then add_off("Damage", "INC", 10, ModFlag.Spell); return ml, 10
	elseif dim_name == "inc_attack_dmg" then add_off("Damage", "INC", 10, ModFlag.Attack); return ml, 10
	elseif dim_name == "inc_crit_chance" then add_off("CritChance", "INC", 20); return ml, 20
	elseif dim_name == "crit_multi" then add_off("CritMultiplier", "BASE", 10); return ml, 10
	elseif dim_name == "inc_speed" then add_off("Speed", "INC", 10); return ml, 10
	elseif dim_name == "gem_level" then
		ml:NewMod("GemProperty", "LIST", { keywordList = { "skill" }, key = "level", value = 1 }, "GradProbe")
		return ml, 1
	elseif dim_name == "minus_enemy_res" then
		add_off("ElementalPenetration", "BASE", 5); add_off("ChaosPenetration", "BASE", 5)
		return ml, 5
	end
	return ml, 1
end

local function handle_compute_grad_build(params)
	params = type(params) == "table" and params or {}
	local build = get_build()
	local main_env = build.calcsTab.mainEnv
	local main_skill = main_env and main_env.player and main_env.player.mainSkill
	local is_minion = (main_env and main_env.minion ~= nil) or (main_skill and main_skill.skillFlags and main_skill.skillFlags.minion) or false
	local ehp_weight = tonumber(params.ehpWeight or params.ehp_weight) or 0.5
	local dims = (params.includeResists == true or params.include_resists == true) and ALL_DIMS or CORE_DIMS
	local t0 = now_ms()
	local calc_func, calc_base = calcs.getMiscCalculator(build)
	local base_dps, dps_key = primary_dps(calc_base)
	local req_stat = params.stat
	local target_stat = (type(req_stat) == "string" and req_stat ~= "") and req_stat or dps_key
	local use_full_dps = (target_stat == "FullDPS") or (dps_key == "FullDPS")
	local base_ehp = calc_base.TotalEHP or 1
	local base_stat = (target_stat == dps_key) and base_dps or (calc_base[target_stat] or base_dps)
	local safe_dps, safe_ehp = (base_dps > 0) and base_dps or 1, (base_ehp > 0) and base_ehp or 1
	local weights = {}
	for i, dim_name in ipairs(dims) do
		local ml, unit = build_probe_mods(dim_name, is_minion)
		local out = calc_func({ addNodes = { [{ id = 991000 + i, modList = ml }] = true } }, use_full_dps)
		local new_dps = (dps_key == "FullDPS" and (out.FullDPS or 0))
			or (out[dps_key] or out.CombinedDPS or out.TotalDPS or 0)
		local new_ehp = out.TotalEHP or 0
		local new_stat = (target_stat == dps_key) and new_dps or (out[target_stat] or new_dps)
		local rel_dps, rel_ehp = ((new_dps - base_dps) / unit) / safe_dps, ((new_ehp - base_ehp) / unit) / safe_ehp
		weights[dim_name] = {
			dStatPerUnit = (new_stat - base_stat) / unit, dDpsPerUnit = (new_dps - base_dps) / unit,
			dEhpPerUnit = (new_ehp - base_ehp) / unit, relDpsPerUnit = rel_dps, relEhpPerUnit = rel_ehp,
			scorePerUnit = rel_dps + ehp_weight * rel_ehp,
		}
	end
	return {
		stat = target_stat, baseStat = base_stat, baseDps = base_dps, baseEhp = base_ehp,
		ehpWeight = ehp_weight, dimCount = #dims, isMinion = is_minion, weights = weights, elapsedMs = now_ms() - t0,
	}
end

rawset(_G, "__pob4agents_probe_build", handle_agent_probe_build)

M.rpc_handlers = {
	agent_probe_build = handle_agent_probe_build,
	eval_scenario_envelope = handle_eval_scenario_envelope,
	compute_grad_build = handle_compute_grad_build,
}

return M
