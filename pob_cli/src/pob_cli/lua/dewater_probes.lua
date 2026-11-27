-- Pinnacle dewater V2 read-only provenance probes & scenario envelope (L1 Lua ext).
local calcs, H = require("Modules.Calcs"), require("pob_host")
local now_ms, get_build, M = H.now_ms, H.get_build, {}

local GUARD_NAMES = { ["Molten Shell"] = true, ["Vaal Molten Shell"] = true, ["Steelskin"] = true, ["Immortal Call"] = true, ["Bone Armour"] = true, ["Arcane Cloak"] = true }
local TEMP_CD_PATS = { "molten shell", "steelskin", "immortal call", "bone armour", "arcane cloak", "berserk", "frost shield", "sigil of power", "focus", "unbound avatar" }
local CORPSE_GEMS, CORPSE_ITEMS = { ["desecrate"] = true, ["unearth"] = true }, { ["corpsewalker"] = true, ["aukuna's will"] = true }

local function snum(v, def) local n = tonumber(v); return (n == nil or n ~= n or n == math.huge or n == -math.huge) and (def or 0) or n end
local function primary_dps(out, m_out)
	if (out.FullDPS or 0) > 0 then return out.FullDPS, "FullDPS" end
	local own = out.CombinedDPS or out.TotalDPS or 0
	if own <= 0 then
		local m = (type(out.Minion) == "table" and out.Minion) or m_out or {}
		local md = m.CombinedDPS or m.TotalDPS or 0
		if md > 0 then return md, "MinionCombinedDPS" end
	end
	return own, "CombinedDPS"
end
local function is_non_util_base(b)
	local s = tostring(b or "")
	return s:find("Life Flask", 1, true) ~= nil or s:find("Mana Flask", 1, true) ~= nil or s:find("Hybrid Flask", 1, true) ~= nil
end
local function has_node(build, needle)
	local alloc = build and build.spec and build.spec.allocNodes
	if type(alloc) ~= "table" then return false end
	for _, nd in pairs(alloc) do if type(nd) == "table" and type(nd.dn) == "string" and nd.dn:lower():find(needle, 1, true) then return true end end
	return false
end

local function probe_flasks(build, mod_db)
	local itab = build.itemsTab or {}
	local slots, items = itab.slots or {}, itab.items or {}
	local left_c = math.floor(snum(mod_db and mod_db:Sum("BASE", nil, "LeftActiveMagicUtilityFlasks"), 0))
	local right_c = math.floor(snum(mod_db and mod_db:Sum("BASE", nil, "RightActiveMagicUtilityFlasks"), 0))
	local util_off = (mod_db and mod_db:Flag(nil, "UtilityFlasksDoNotApplyToPlayer")) and true or false
	local magic_util, empty_cnt = {}, 0
	for i = 1, 5 do
		local sn = "Flask " .. i
		local sl = slots[sn]
		local it = sl and sl.selItemId and sl.selItemId > 0 and items[sl.selItemId] or nil
		if not it then empty_cnt = empty_cnt + 1
		elseif it.type == "Flask" and it.rarity == "MAGIC" and not is_non_util_base(it.baseName) then magic_util[#magic_util + 1] = sn end
	end
	local mb_set, mb_list = {}, {}
	if not util_off and (left_c > 0 or right_c > 0) then
		for i = 1, math.min(left_c, #magic_util) do mb_set[magic_util[i]] = true end
		for i = #magic_util, math.max(#magic_util - right_c + 1, 1), -1 do if right_c > 0 then mb_set[magic_util[i]] = true end end
		for i = 1, 5 do local sn = "Flask " .. i; if mb_set[sn] then mb_list[#mb_list + 1] = sn end end
	end
	local dur_inc, rate_inc = snum(mod_db and mod_db:Sum("INC", nil, "FlaskDuration"), 0), snum(mod_db and mod_db:Sum("INC", nil, "FlaskRecoveryRate"), 0)
	local used_inc, gain_inc = snum(mod_db and mod_db:Sum("INC", nil, "FlaskChargesUsed"), 0), snum(mod_db and mod_db:Sum("INC", nil, "FlaskChargesGained"), 0)
	local gen_all, gen_life = snum(mod_db and mod_db:Sum("BASE", nil, "FlaskChargesGenerated"), 0), snum(mod_db and mod_db:Sum("BASE", nil, "LifeFlaskChargesGenerated"), 0)
	local gen_mana, gen_util = snum(mod_db and mod_db:Sum("BASE", nil, "ManaFlaskChargesGenerated"), 0), snum(mod_db and mod_db:Sum("BASE", nil, "UtilityFlaskChargesGenerated"), 0)
	local gen_empty = snum(mod_db and mod_db:Sum("BASE", nil, "FlaskChargesGeneratedPerEmptyFlask"), 0) * empty_cnt
	local no_cons = math.min(snum(mod_db and mod_db:Sum("BASE", nil, "FlaskChanceNotConsumeCharges"), 0), 100)
	local life_nr, mana_nr = (mod_db and mod_db:Flag(nil, "LifeFlaskEffectNotRemoved")) and true or false, (mod_db and mod_db:Flag(nil, "ManaFlaskEffectNotRemoved")) and true or false
	local out_flasks = {}
	for i = 1, 5 do
		local sn = "Flask " .. i
		local sl = slots[sn]
		local it = sl and sl.selItemId and sl.selItemId > 0 and items[sl.selItemId] or nil
		if it then
			local base, fdata = it.base or {}, it.flaskData or {}
			local fbase = base.flask or {}
			local is_tinc = (it.type == "Tincture") or (base.type == "Tincture") or tostring(it.baseName or ""):lower():find("tincture", 1, true) ~= nil
			local is_life, is_mana = fbase.life and true or false, fbase.mana and true or false
			local is_util = (not is_tinc) and (not is_life) and (not is_mana)
			local rlow = tostring(it.raw or ""):lower()
			local blocked = rlow:find("gains no charges during", 1, true) ~= nil or rlow:find("cannot gain charges during", 1, true) ~= nil
			local has_up, f_dur = is_util, snum(fdata.duration, 0) * (1 + dur_inc / 100)
			if is_life and (fdata.lifeEffectNotRemoved or life_nr) then
				has_up = true
				local lr_i, lr_m = snum(mod_db and mod_db:Sum("INC", nil, "FlaskLifeRecoveryRate"), 0), snum(mod_db and mod_db:More(nil, "FlaskLifeRecoveryRate"), 1)
				local inst = snum(fdata.instantPerc, 0) + snum(mod_db and mod_db:Sum("BASE", nil, "LifeFlaskInstantRecovery"), 0)
				f_dur = (inst >= 100) and 0 or (f_dur / math.max(0.01, 1 + rate_inc / 100) / math.max(0.01, 1 + lr_i / 100) / math.max(0.01, lr_m))
			elseif is_mana and (fdata.manaEffectNotRemoved or mana_nr) then
				has_up = true
				local mr_i = snum(mod_db and mod_db:Sum("INC", nil, "FlaskManaRecoveryRate"), 0)
				local inst = snum(fdata.instantPerc, 0) + snum(mod_db and mod_db:Sum("BASE", nil, "ManaFlaskInstantRecovery"), 0)
				f_dur = (inst >= 100) and 0 or (f_dur / math.max(0.01, 1 + rate_inc / 100) / math.max(0.01, 1 + mr_i / 100))
			end
			local gmod = snum(fdata.gainMod, 1) * (1 + gain_inc / 100)
			local r3 = (gen_all + (is_life and gen_life or 0) + (is_mana and gen_mana or 0) + (is_util and gen_util or 0)) * gmod
			local r5, used = gen_empty * gmod, snum(fdata.chargesUsed, 0) * (1 + used_inc / 100)
			local min_p, avg_p = 0, 0
			if has_up and used > 0 and f_dur > 0 and not blocked then
				local min_ch = (f_dur - (f_dur % 3)) * r3 + (f_dur - (f_dur % 5)) * r5
				min_p = (no_cons >= 100) and 100 or math.min(min_ch / used * 100, 100)
				avg_p = (no_cons >= 100) and 100 or math.min((r3 + r5) * f_dur / (used * math.max(1e-4, (100 - no_cons) / 100)) * 100, 100)
			end
			out_flasks[#out_flasks + 1] = {
				slot = sn, active = (sl and sl.active) and true or false, title = it.title or it.name or sn,
				baseName = it.baseName or "", rarity = it.rarity or "", isTincture = is_tinc, isLife = is_life,
				isMana = is_mana, isUtility = is_util, isMagicUtility = (is_util and it.rarity == "MAGIC") and true or false,
				magebloodControlled = mb_set[sn] and true or false, chargeGainBlocked = blocked,
				duration = f_dur, chargesUsed = used, chargesPerSec = r3 + r5, minUptimePct = min_p, avgUptimePct = avg_p,
				sustained = (not is_tinc) and (not blocked) and has_up and (min_p >= 100 or no_cons >= 100),
			}
		end
	end
	return {
		leftMagicUtility = left_c, rightMagicUtility = right_c, utilityFlasksDisabled = util_off,
		controlledSlots = mb_list, emptyFlaskSlots = empty_cnt, lifeFlaskEffectNotRemoved = life_nr,
		manaFlaskEffectNotRemoved = mana_nr, hasBloodsoakedBlade = has_node(build, "bloodsoaked blade"),
		hasSpikedConcoction = ((mod_db and mod_db:Flag(nil, "Condition:CanHaveAlchemistGenius")) or has_node(build, "spiked concoction")) and true or false,
	}, out_flasks
end

local function probe_corpses_and_cd(build, env, out)
	local corpses, cd_skills, seen = {}, {}, {}
	local function add_c(l) if not seen[l] then seen[l] = true; corpses[#corpses + 1] = l end end
	for _, grp in ipairs((build.skillsTab and build.skillsTab.socketGroupList) or {}) do
		if grp.enabled ~= false and not tostring(grp.slot or ""):find("Swap", 1, true) then
			for _, gm in ipairs(grp.gemList or {}) do
				local nm = gm.enabled ~= false and tostring(gm.nameSpec or (gm.gemData and gm.gemData.name) or "") or ""
				if CORPSE_GEMS[nm:lower()] then add_c(nm) end
			end
		end
	end
	for sn, sl in pairs((build.itemsTab and build.itemsTab.slots) or {}) do
		if sl and sl.selItemId and sl.selItemId > 0 and not tostring(sn):find("Swap", 1, true) then
			local it = build.itemsTab.items[sl.selItemId]
			local t = it and tostring(it.title or it.name or "") or ""
			if CORPSE_ITEMS[t:lower()] then add_c(t) end
		end
	end
	local mana_u = snum(out and out.ManaUnreserved, 0)
	for _, sk in ipairs((env and env.player and env.player.activeSkillList) or {}) do
		local nm = (sk.activeEffect and sk.activeEffect.grantedEffect and sk.activeEffect.grantedEffect.name) or ""
		local low = nm:lower()
		local is_vaal = low:find("^vaal ") ~= nil
		local is_t = is_vaal
		if not is_t then for _, p in ipairs(TEMP_CD_PATS) do if low:find(p, 1, true) then is_t = true; break end end end
		if is_t and not (sk.skillFlags and sk.skillFlags.disable) then
			local sd, mods, cfg = sk.skillData or {}, sk.skillModList, sk.skillCfg
			local dur = snum(sd.duration, 0) * ((mods and snum(calcLib.mod(mods, cfg, "Duration", "PrimaryDuration", "SkillAndDamagingAilmentDuration"), 1)) or 1)
			local cd = snum(sd.cooldown, 0) / math.max(0.01, (mods and snum(calcLib.mod(mods, cfg, "CooldownRecovery"), 1)) or 1)
			local e = {
				name = nm, isVaal = is_vaal, isGuard = GUARD_NAMES[nm] and true or false,
				isAutomated = sk.triggeredBy ~= nil, duration = dur, cooldown = cd,
				dutyCyclePct = (dur > 0 and cd > 0) and (dur / (dur + cd) * 100) or 0,
			}
			if low == "arcane cloak" and mana_u > 0 then e.manaSpend = math.floor(mana_u * 0.64 + 0.5) end
			cd_skills[#cd_skills + 1] = e
		end
	end
	return corpses, cd_skills
end

local function probe_flags(env, mod_db, enemy_db, out)
	local ms = env and env.player and env.player.mainSkill
	local mf, mc = (ms and ms.skillFlags) or {}, ms and ms.skillCfg or nil
	local max_r = snum(out.MaximumRage, 0)
	if max_r <= 0 and mod_db then max_r = math.floor(snum(mod_db:Sum("BASE", mc, "MaximumRage"), 0) * snum(mod_db:More(mc, "MaximumRage"), 1)) end
	local elvl, boss = snum(env and env.enemyLevel, 84), tostring((env and env.configInput and env.configInput.enemyIsBoss) or "None")
	local is_b, eth = boss ~= "None", 0
	if data and data.monsterAilmentThresholdTable then
		local ml = (elvl >= 66) and snum((is_b and data.mapLevelBossLifeMult or data.mapLevelLifeMult)[elvl], 1) or 1
		local ma = (elvl >= 66 and is_b) and snum((data.mapLevelBossAilmentMult or {})[elvl], 1) or 1
		eth = snum(data.monsterAilmentThresholdTable[elvl], 0) * (is_b and 7.68 or 1) * ml * (enemy_db and snum(enemy_db:More(nil, "Life"), 1) or 1) * ma * (enemy_db and snum(enemy_db:More(nil, "AilmentThreshold"), 1) or 1)
	end
	local mins, maxs = {}, {}
	for _, a in ipairs({ "Shock", "Chill", "Scorch", "Brittle", "Sap" }) do
		local mv = snum(out[a .. "Minimum"], 0); if mv <= 0 and mod_db then mv = snum(mod_db:Sum("BASE", nil, a .. "Minimum"), 0) end
		mins[a], maxs[a] = mv, snum(out["Maximum" .. a], 0)
	end
	local spd, mcst = snum(out.Speed, 0), snum(out.ManaCost, 0)
	local mrec = snum(out.ManaRegenRecovery, 0) + snum(out.ManaLeechGainRate, 0)
	if mrec <= 0 then mrec = math.max(0, snum(out.NetManaRegen, 0) + mcst * spd) end
	return {
		enemyIsBoss = boss, enemyLevel = elvl, enemyThreshold = eth,
		canGainRage = (mod_db and mod_db:Flag(nil, "Condition:CanGainRage")) and true or false,
		maximumRage = max_r, rageRegen = snum(mod_db and mod_db:Sum("BASE", nil, "RageRegen"), 0),
		canBeElusive = (mod_db and mod_db:Flag(nil, "Condition:CanBeElusive")) and true or false,
		canWither = (mod_db and mod_db:Flag(nil, "Condition:CanWither")) and true or false,
		shockCanStack = (mod_db and mod_db:Flag(nil, "ShockCanStack")) and true or false,
		shockStacksMax = snum(mod_db and (mod_db:Override(nil, "ShockStacksMax") or mod_db:Sum("BASE", nil, "ShockStacksMax")), 50),
		enduranceChargesDuration = snum(out.EnduranceChargesDuration, 10), minimums = mins, maximums = maxs,
		mainSkill = {
			name = (ms and ms.activeEffect and ms.activeEffect.grantedEffect and ms.activeEffect.grantedEffect.name) or "",
			isProxy = (mf.totem or mf.trap or mf.mine or mf.minion) and true or false,
			isMelee = (mf.melee and true) or false, isAttack = (mf.attack and true) or false, isSpell = (mf.spell and true) or false,
			speed = spd, hitChance = snum(out.HitChance, 100), critChance = snum(out.EffectiveCritChance or out.CritChance, 0),
			averageHit = snum(out.AverageHit or out.AverageDamage, 0),
			igniteChance = math.max(snum(out.IgniteChance, 0), snum(out.EnemyIgniteChance, 0)),
			igniteDps = snum(out.IgniteDPS, 0), igniteDuration = snum(out.IgniteDuration, 4),
			bleedChance = snum(out.BleedChance, 0), bleedDps = snum(out.BleedDPS, 0), bleedDuration = snum(out.BleedDuration, 5),
			manaCost = mcst, manaSpendPerSec = mcst * spd, sustainableManaRecovPerSec = mrec,
			manaUnreserved = snum(out.ManaUnreserved, 0), lifeUnreservedPct = snum(out.LifeUnreservedPercent, 100),
		},
	}
end

local function handle_get_dewater_provenance(_params)
	local ok, res = pcall(function()
		local build, t0 = get_build(), now_ms()
		local env = build.calcsTab and build.calcsTab.mainEnv
		if type(env) ~= "table" or type(env.player) ~= "table" then return { ok = false, error = "mainEnv unavailable" } end
		local mod_db, out = env.player.modDB, env.player.output or {}
		local mb, flasks = probe_flasks(build, mod_db)
		local corpses, cd_skills = probe_corpses_and_cd(build, env, out)
		return { ok = true, mageblood = mb, flasks = flasks, corpseSources = corpses, cooldownSkills = cd_skills, flags = probe_flags(env, mod_db, env.enemyDB, out), elapsedMs = now_ms() - t0 }
	end)
	return ok and res or { ok = false, error = tostring(res) }
end

local ENVELOPE_SCENARIOS = {
	{ name = "Raw", flasks = true, guards = true, overlay = { enemyIsBoss = "None", conditionKilledRecently = true, conditionHitRecently = true, buffOnslaught = true, usePowerCharges = true, useFrenzyCharges = true, useEnduranceCharges = true } },
	{ name = "PinnacleSustained", flasks = false, guards = false, overlay = { enemyIsBoss = "Pinnacle", conditionKilledRecently = false, buffOnslaught = false } },
	{ name = "Burst", flasks = true, guards = true, overlay = { enemyIsBoss = "Pinnacle", conditionKilledRecently = false, conditionHitRecently = true, buffOnslaught = true, usePowerCharges = true, useFrenzyCharges = true, useEnduranceCharges = true } },
	{ name = "WorstHit", flasks = false, guards = false, overlay = { enemyIsBoss = "Uber", enemyDamageRollRange = 100, conditionKilledRecently = false, buffOnslaught = false } },
}

local function handle_eval_scenario_envelope(_params)
	local build = get_build()
	local cfg_in, itab = build.configTab.input, build.itemsTab
	local saved_cfg, saved_fl, saved_g = {}, {}, {}
	for k, v in pairs(cfg_in) do saved_cfg[k] = v end
	for i = 1, 5 do local sl = itab.slots["Flask " .. i]; if sl then saved_fl[i] = sl.active end end
	for gi, grp in ipairs(build.skillsTab.socketGroupList or {}) do
		for gemi, gem in ipairs(grp.gemList or {}) do if GUARD_NAMES[gem.nameSpec] then saved_g[#saved_g + 1] = { gi = gi, gemi = gemi, enabled = gem.enabled } end end
	end
	local function set_fg(fl_on, g_on)
		for i = 1, 5 do
			local sl = itab.slots["Flask " .. i]; local it = sl and sl.selItemId and sl.selItemId > 0 and itab.items[sl.selItemId]
			if it and it.base and it.base.subType == "Utility" then sl.active = fl_on end
		end
		for _, g in ipairs(saved_g) do build.skillsTab.socketGroupList[g.gi].gemList[g.gemi].enabled = g_on end
	end
	local function reset_cfg(ov)
		for k in pairs(cfg_in) do cfg_in[k] = nil end; for k, v in pairs(saved_cfg) do cfg_in[k] = v end; for k, v in pairs(ov or {}) do cfg_in[k] = v end
	end
	local t0, out_sc = now_ms(), {}
	local ok, err = pcall(function()
		for _, sc in ipairs(ENVELOPE_SCENARIOS) do
			local ts = now_ms(); reset_cfg(sc.overlay); set_fg(sc.flasks, sc.guards); build.configTab:BuildModList()
			local sc_env = calcs.buildOutput(build, "MAIN"); local out = (sc_env.player and sc_env.player.output) or {}
			local dps, dk = primary_dps(out, sc_env.minion and sc_env.minion.output)
			out_sc[sc.name] = { dps = dps, dpsKey = dk, combinedDps = out.CombinedDPS or 0, fullDps = out.FullDPS or 0, totalEhp = out.TotalEHP or 0, physMaxHit = out.PhysicalMaximumHitTaken or 0, eleMinMaxHit = math.min(out.FireMaximumHitTaken or 0, out.ColdMaximumHitTaken or 0, out.LightningMaximumHitTaken or 0), chaosMaxHit = out.ChaosMaximumHitTaken or 0, elapsedMs = now_ms() - ts }
		end
	end)
	reset_cfg(); for i, st in pairs(saved_fl) do local sl = itab.slots["Flask " .. i]; if sl then sl.active = st end end
	for _, g in ipairs(saved_g) do build.skillsTab.socketGroupList[g.gi].gemList[g.gemi].enabled = g.enabled end
	build.configTab:BuildModList(); build.calcsTab:BuildOutput()
	if not ok then error(err, 0) end
	local rd, pd = (out_sc.Raw and out_sc.Raw.dps) or 0, (out_sc.PinnacleSustained and out_sc.PinnacleSustained.dps) or 0
	return { scenarios = out_sc, inflationRatio = (pd > 0) and (rd / pd) or 0, elapsedMs = now_ms() - t0 }
end

rawset(_G, "__pob4agents_eval_scenario_envelope", handle_eval_scenario_envelope)
M.rpc_handlers = { get_dewater_provenance = handle_get_dewater_provenance, eval_scenario_envelope = handle_eval_scenario_envelope }
return M
