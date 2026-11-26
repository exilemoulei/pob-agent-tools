-- Read-only PoB map-modifier signal probe (`map_regex_probe`) for `pob map-regex`.
local calcs = require("Modules.Calcs")
local H = require("pob_host")
local now_ms, get_build = H.now_ms, H.get_build
local M = {}

local MOVEMENT_SKILLS = {
	["Leap Slam"] = true, ["Shield Charge"] = true, ["Whirling Blades"] = true,
	["Frostblink"] = true, ["Flame Dash"] = true, ["Dash"] = true,
	["Blink Arrow"] = true, ["Lightning Warp"] = true, ["Bodyswap"] = true,
	["Flame Dash of Return"] = true, ["Frostblink of Wintry Blast"] = true,
	["Ambush"] = true, ["Flicker Strike"] = true, ["Charged Dash"] = true,
	["Cyclone"] = true,
}

local function avg_pair(lo, hi)
	local a, b = tonumber(lo) or 0, tonumber(hi) or 0
	if a <= 0 and b <= 0 then return 0 end
	return (a + b) * 0.5
end

local function skill_name(sk)
	local ge = sk and sk.activeEffect and sk.activeEffect.grantedEffect
	return (ge and ge.name) or ""
end

local function is_proxy_skill(sk, env)
	local sf = sk and sk.skillFlags or {}
	return (sk == (env and env.player and env.player.mainSkill) and env.minion ~= nil)
		or sf.minion or sf.totem or sf.trap or sf.mine or false
end

local function cannot_reflect(sk)
	local ml, cfg = sk and sk.skillModList, sk and sk.skillCfg
	if not ml then return false end
	return ml:Flag(cfg, "DamageCannotBeReflected") or false
end

local function reflect_taken_pct(output, mod_db, dmg_type)
	if mod_db:Flag(nil, "CannotTakeReflectedDamage") then return 0 end
	if dmg_type == "Physical" and mod_db:Flag(nil, "CannotTakeReflectedPhysicalDamage") then return 0 end
	if dmg_type ~= "Physical" and mod_db:Flag(nil, "CannotTakeReflectedElementalDamage") then return 0 end
	local raw = output[dmg_type .. "TakenReflect"]
	if type(raw) == "number" then return math.max(raw * 100, 0) end
	local inc = mod_db:Sum("INC", nil, "ReflectedDamageTaken", dmg_type .. "ReflectedDamageTaken")
	if dmg_type ~= "Physical" then inc = inc + mod_db:Sum("INC", nil, "ElementalReflectedDamageTaken") end
	return math.max(100 + inc, 0)
end

local function single_hit_avg(t, dmg_type, fallback_cc)
	if type(t) ~= "table" then return 0 end
	local hit = tonumber(t[dmg_type .. "HitAverage"]) or 0
	local crit = tonumber(t[dmg_type .. "CritAverage"]) or 0
	local raw_cc = tonumber(t.CritChance)
	if raw_cc == nil then raw_cc = fallback_cc end
	if hit > 0 or crit > 0 then
		if raw_cc ~= nil then
			local cc = math.min(math.max(raw_cc, 0), 100) / 100
			return hit * (1 - cc) + crit * cc
		end
		return hit > 0 and hit or crit
	end
	local p = avg_pair(t[dmg_type .. "Min"], t[dmg_type .. "Max"])
	if p > 0 then return p end
	return avg_pair(t[dmg_type .. "MinBase"], t[dmg_type .. "MaxBase"])
end

local function hit_avg(tbl, dmg_type)
	local top_cc = tonumber(tbl.CritChance)
	local top = single_hit_avg(tbl, dmg_type, top_cc)
	if top > 0 then return top end
	local mh = single_hit_avg(tbl.MainHand, dmg_type, top_cc)
	local oh = single_hit_avg(tbl.OffHand, dmg_type, top_cc)
	if mh > 0 and oh > 0 then return (mh + oh) * 0.5 end
	return math.max(mh, oh)
end

local function extract_cached_output(entry)
	if type(entry) ~= "table" then return nil end
	if type(entry.Env) == "table" and type(entry.Env.player) == "table" and type(entry.Env.player.output) == "table" then
		return entry.Env.player.output
	end
	return entry
end

local function cached_skill_hits(sk, env, main_out)
	local src = nil
	if sk == env.player.mainSkill then
		src = main_out
	else
		local mode = env.mode or "MAIN"
		local uuid = (type(cacheSkillUUID) == "function") and cacheSkillUUID(sk, env) or nil
		if uuid and GlobalCache and GlobalCache.cachedData then
			for _, m in ipairs({ mode, "MAIN", "CALCS" }) do
				local bucket = GlobalCache.cachedData[m]
				if type(bucket) == "table" and bucket[uuid] then
					src = extract_cached_output(bucket[uuid])
					if src then break end
				end
			end
			if not src and mode ~= "CALCULATOR" and type(calcs.buildActiveSkill) == "function" and env.build then
				local ok_calc, res_out = pcall(calcs.buildActiveSkill, env, mode, sk, uuid, { uuid })
				if ok_calc then
					local bucket = GlobalCache.cachedData[mode]
					src = (type(bucket) == "table" and extract_cached_output(bucket[uuid]))
						or (type(res_out) == "table" and res_out or nil)
				end
			end
		end
	end
	if type(src) == "table" then
		return hit_avg(src, "Physical"), hit_avg(src, "Fire"), hit_avg(src, "Cold"),
			hit_avg(src, "Lightning"), hit_avg(src, "Chaos")
	end
	return 0, 0, 0, 0, 0
end

local function handle_map_regex_probe(_params)
	local build, t0 = get_build(), now_ms()
	local env = build.calcsTab.mainEnv or {}
	local player = env.player or {}
	local out = player.output or {}
	local mod_db = player.modDB
	local ms = player.mainSkill
	local msf = (ms and ms.skillFlags) or {}

	local p_avg, f_avg, c_avg, l_avg, ch_avg = 0, 0, 0, 0, 0
	if ms then p_avg, f_avg, c_avg, l_avg, ch_avg = cached_skill_hits(ms, env, out) end
	local main_skill = {
		name = skill_name(ms),
		avgPhys = p_avg, avgFire = f_avg, avgCold = c_avg, avgLight = l_avg, avgChaos = ch_avg,
		speed = out.Speed or 0, manaCost = out.ManaCost or 0, cooldown = out.SkillCooldown or 0,
		critChance = out.CritChance or 0, combinedDps = out.CombinedDPS or 0,
		totalDps = out.TotalDPS or 0, igniteDps = out.IgniteDPS or 0,
		poisonDps = out.PoisonDPS or 0, bleedDps = out.BleedDPS or 0,
		cannotBeReflected = cannot_reflect(ms),
		isMinion = (env.minion ~= nil) or msf.minion or false,
		isTotem = msf.totem or false, isTrap = msf.trap or false, isMine = msf.mine or false,
		isAttack = msf.attack or false, isSpell = msf.spell or false, isArea = msf.area or false,
		hitChance = out.HitChance or 100,
		hitsCannotBeEvaded = (ms and ms.skillModList and ms.skillModList:Flag(ms.skillCfg, "CannotBeEvaded")) or false,
	}

	local triggered, movement, active_names, aura_names, curse_names = {}, {}, {}, {}, {}
	local seen_active, seen_aura, seen_curse = {}, {}, {}
	for _, sk in ipairs(player.activeSkillList or {}) do
		local sf = sk.skillFlags or {}
		if not sf.disable then
			local nm = skill_name(sk)
			if nm ~= "" and not seen_active[nm] then
				seen_active[nm] = true; active_names[#active_names + 1] = nm
			end
			local st = sk.skillTypes or {}
			if SkillType and st[SkillType.Aura] and not st[SkillType.AppliesCurse] and nm ~= "" and not seen_aura[nm] then
				seen_aura[nm] = true; aura_names[#aura_names + 1] = nm
			end
			if SkillType and st[SkillType.AppliesCurse] and nm ~= "" and not seen_curse[nm] then
				seen_curse[nm] = true; curse_names[#curse_names + 1] = nm
			end
			if sk.triggeredBy and sk ~= ms then
				local tp, tf, tc, tl, tch = cached_skill_hits(sk, env, out)
				triggered[#triggered + 1] = {
					name = nm,
					triggerName = (sk.triggeredBy.grantedEffect and sk.triggeredBy.grantedEffect.name) or "",
					avgPhys = tp, avgFire = tf, avgCold = tc, avgLight = tl, avgChaos = tch,
					cannotBeReflected = cannot_reflect(sk), isProxy = is_proxy_skill(sk, env),
				}
			end
			if MOVEMENT_SKILLS[nm] and sk ~= ms then
				local mp, mf, mc, ml_hit, mch = cached_skill_hits(sk, env, out)
				movement[#movement + 1] = {
					name = nm, avgPhys = mp, avgFire = mf, avgCold = mc, avgLight = ml_hit, avgChaos = mch,
					cannotBeReflected = cannot_reflect(sk),
				}
			end
		end
	end

	local keystones, asc_nodes = {}, {}
	for _, node in pairs((build.spec and build.spec.allocNodes) or {}) do
		if type(node) == "table" and node.dn then
			if node.type == "Keystone" then keystones[#keystones + 1] = node.dn
			elseif node.ascendancyName and node.type ~= "AscendancyStart" then asc_nodes[#asc_nodes + 1] = node.dn end
		end
	end

	local uniques, has_enduring_mana = {}, false
	for slot_name, item in pairs(player.itemList or {}) do
		if type(item) == "table" then
			if item.rarity == "UNIQUE" and item.title and item.title ~= "" then
				uniques[#uniques + 1] = item.title
			end
			if slot_name:match("^Flask") and item.base and item.base.subType == "Mana" then
				local txt = tostring(item.name or "") .. "\n" .. tostring(item.raw or "")
				if txt:lower():find("enduring", 1, true) then has_enduring_mana = true end
			end
		end
	end

	local cfg = (build.configTab and build.configTab.input) or {}
	local probe_fn = rawget(_G, "__pob4agents_probe_build")
	local cliffs = {}
	if type(probe_fn) == "function" then
		local ok_p, p_res = pcall(probe_fn, {})
		if ok_p and type(p_res) == "table" and type(p_res.triggerCliffs) == "table" then
			cliffs = p_res.triggerCliffs
		end
	end

	local defence = {
		life = out.Life or 0, lifeUnreserved = out.LifeUnreserved or 0,
		lifeUnreservedPct = out.LifeUnreservedPercent or 100, es = out.EnergyShield or 0,
		mana = out.Mana or 0, manaUnreserved = out.ManaUnreserved or 0,
		netLifeRegen = out.NetLifeRegen or 0, lifeRegen = out.LifeRegenRecovery or out.LifeRegen or 0,
		lifeDegen = out.LifeDegen or 0, netEsRegen = out.NetEnergyShieldRegen or 0,
		esRegen = out.EnergyShieldRegenRecovery or out.EnergyShieldRegen or 0,
		netManaRegen = out.NetManaRegen or 0, lifeLeechRate = out.LifeLeechRate or 0,
		manaLeechRate = out.ManaLeechRate or 0, esLeechRate = out.EnergyShieldLeechRate or 0,
		manaGainOnHit = out.ManaOnHit or 0, armour = out.Armour or 0, evasion = out.Evasion or 0,
		blockChance = out.EffectiveBlockChance or 0, spellBlockChance = out.EffectiveSpellBlockChance or 0,
		spellSuppressionChance = out.EffectiveSpellSuppressionChance or 0,
		spellSuppressionOvercap = out.SpellSuppressionChanceOverCap or 0,
		fireMaxRes = out.FireResistMax or 75, coldMaxRes = out.ColdResistMax or 75, lightMaxRes = out.LightningResistMax or 75,
		fireOvercap = out.FireResistOverCap or 0, coldOvercap = out.ColdResistOverCap or 0, lightOvercap = out.LightningResistOverCap or 0,
		physTakenReflect = mod_db and reflect_taken_pct(out, mod_db, "Physical") or 100,
		fireTakenReflect = mod_db and reflect_taken_pct(out, mod_db, "Fire") or 100,
		coldTakenReflect = mod_db and reflect_taken_pct(out, mod_db, "Cold") or 100,
		lightTakenReflect = mod_db and reflect_taken_pct(out, mod_db, "Lightning") or 100,
		curseEffectOnSelf = out.CurseEffectOnSelf or 100,
		critExtraDamageReduction = out.CritExtraDamageReduction or 0,
		shockAvoid = out.ShockAvoidChance or 0,
		unaffectedByShock = (mod_db and (mod_db:Flag(nil, "UnaffectedByShock") or mod_db:Flag(nil, "ShockImmune"))) or false,
		unaffectedByBurningGround = (mod_db and mod_db:Flag(nil, "UnaffectedByBurningGround")) or false,
		actionSpeedMin = (mod_db and mod_db:Max(nil, "MinimumActionSpeed")) or 0,
		physMaxHit = out.PhysicalMaximumHitTaken or 0,
	}

	return {
		mainSkill = main_skill, triggeredSkills = triggered, movementSkills = movement,
		defence = defence, keystones = keystones, ascendancyNodes = asc_nodes, uniques = uniques,
		pantheon = { major = tostring(cfg.pantheonMajorGod or ""), minor = tostring(cfg.pantheonMinorGod or "") },
		charges = {
			usePower = (out.PowerCharges or 0) > 0 or cfg.usePowerCharges == true,
			useFrenzy = (out.FrenzyCharges or 0) > 0 or cfg.useFrenzyCharges == true,
			useEndurance = (out.EnduranceCharges or 0) > 0 or cfg.useEnduranceCharges == true,
			maxPower = out.PowerChargesMax or 3, maxFrenzy = out.FrenzyChargesMax or 3, maxEndurance = out.EnduranceChargesMax or 3,
			minPower = out.PowerChargesMin or 0, minFrenzy = out.FrenzyChargesMin or 0, minEndurance = out.EnduranceChargesMin or 0,
		},
		auras = { count = #aura_names, names = aura_names },
		flasks = { hasEnduringMana = has_enduring_mana },
		curses = { count = #curse_names, names = curse_names },
		triggerCliffs = cliffs, activeSkillNames = active_names,
		elapsedMs = now_ms() - t0,
	}
end

M.rpc_handlers = { map_regex_probe = handle_map_regex_probe }
return M
