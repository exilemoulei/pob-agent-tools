-- Single-pass direct calc (C1/C3): one CALCULATOR-mode pass whose stats, warnings and points
-- match what get_stats / get_sidebar / get_build_info report after a full MAIN-mode rebuild.
local t_insert, m_huge, m_ceil = table.insert, math.huge, math.ceil

local function sanitize(v)
	local vt = type(v)
	if vt == "number" then
		return (v ~= v and "nan") or (v == m_huge and "inf") or (v == -m_huge and "-inf") or v
	end
	if vt == "boolean" or vt == "string" then return v end
	return nil
end

local function has_full_dps_skills(b)
	local list = b.skillsTab and b.skillsTab.socketGroupList
	if type(list) ~= "table" then return false end
	for i = 1, #list do
		local g = list[i]
		if g and g.enabled and g.includeInFullDPS then return true end
	end
	return false
end

local COST_POOLS = { LifeUnreserved = "LifeCost", ManaUnreserved = "ManaCost", Rage = "RageCost", EnergyShield = "ESCost" }
local PCT_POOLS = { LifeUnreservedPercent = "LifePercentCost", ManaUnreservedPercent = "ManaPercentCost" }

-- calcs.buildOutput fills the *CostWarningList outputs only in MAIN mode (Calcs.lua); RefreshStatList
-- turns them into "You do not have enough Mana to use: ..." lines, so a CALCULATOR pass repeats it.
local function add_cost_warnings(calcs, env, out, mode)
	local cd = GlobalCache and GlobalCache.cachedData and GlobalCache.cachedData[mode]
	if type(cd) ~= "table" or type(cacheSkillUUID) ~= "function" or type(calcs.buildActiveSkill) ~= "function" then return end
	local main = env.player.mainSkill
	for _, skill in ipairs(env.player.activeSkillList or {}) do
		local uuid = cacheSkillUUID(skill, env)
		if not cd[uuid] then calcs.buildActiveSkill(env, mode, skill, uuid) end
		local ce = cd[uuid]
		if ce then
			local name, co = skill.activeEffect.grantedEffect.name, ce.Env.player.output
			out.EnergyShieldProtectsMana = env.modDB:Flag(nil, "EnergyShieldProtectsMana")
			for pool, res in pairs(COST_POOLS) do
				local cost = co[res]
				local total = (out.EnergyShieldProtectsMana and res == "ManaCost" and out.EnergyShield or 0) + (out[pool] or 0)
				if cost and total < cost then
					local raw = pool:gsub("Unreserved$", "")
					local resv = ce.Env.player.mainSkill and ce.Env.player.mainSkill.skillData[raw .. "ReservedPercent"]
					if not resv or (total + m_ceil((out[raw] or 0) * resv / 100)) < cost then
						if main and main.activeEffect.grantedEffect.name == name then out[res .. "Warning"] = true end
						out[res .. "WarningList"] = out[res .. "WarningList"] or {}
						t_insert(out[res .. "WarningList"], name)
					end
				end
			end
			for pool, res in pairs(PCT_POOLS) do
				local cost, key = co[res], res .. "PercentCostWarningList"
				if cost and (out[pool] or 0) < cost then
					out[key] = out[key] or {}
					t_insert(out[key], name)
				end
			end
		end
	end
end

-- The same flattening as bridge get_stats: scalars, plus one level down for table outputs
-- (output.MainHand.Accuracy -> "MainHandAccuracy", output.Minion.TotalDPS -> "MinionTotalDPS").
local function flatten(out)
	local stats = {}
	for k, v in pairs(out) do
		if type(k) == "string" then
			if type(v) == "table" then
				for ck, cv in pairs(v) do
					if type(ck) == "string" then local sv = sanitize(cv); if sv ~= nil then stats[k .. ck] = sv end end
				end
			else
				local sv = sanitize(v)
				if sv ~= nil then stats[k] = sv end
			end
		end
	end
	return stats
end

local function strip_escapes(s)
	return (s:gsub("%^x%x%x%x%x%x%x", ""):gsub("%^%d", ""))
end

-- Point limits come from the string Build.lua EstimatePlayerProgress formats (as bridge
-- get_build_info reads them), so they track PoB's own arithmetic instead of a copy of it.
local function points_from_display(b, used, asc)
	local pd = b.controls and b.controls.pointDisplay
	local pairs_ = {}
	if pd and type(pd.str) == "string" then
		for u, m in strip_escapes(pd.str):gmatch("(%d+)%s*/%s*(%d+)") do pairs_[#pairs_ + 1] = tonumber(m) end
	end
	return { used = used, usedMax = pairs_[1], ascUsed = asc, ascMax = pairs_[2] }
end

return function(get_build)
	return function(params)
		local b = get_build()
		local skip_ehp = type(params) == "table" and params.skipEHP == true
		local calcs = b.calcsTab.calcs
		if b.buildFlag and b.skillsTab and type(b.skillsTab.UpdateSocketGroups) == "function" then b.skillsTab:UpdateSocketGroups() end
		if b.spec and type(b.spec.nodes) == "table" then for _, nd in pairs(b.spec.nodes) do nd.finalModList = nd.modList end end
		if type(wipeGlobalCache) == "function" then wipeGlobalCache() end
		local env, cP, cE, cM = calcs.initEnv(b, "CALCULATOR")
		env.triggerSourceCache = {}
		calcs.perform(env, skip_ehp and true or nil)
		local out = env.player.output
		if has_full_dps_skills(b) then
			if env.player.mainSkill and type(cacheSkillUUID) == "function" then
				local uid = cacheSkillUUID(env.player.mainSkill, env)
				local cd = GlobalCache and GlobalCache.cachedData and GlobalCache.cachedData["CALCULATOR"]
				local me = cd and cd[uid]
				if me and me.Env == env then env.triggerSourceCache[uid] = me end
			end
			local fd = calcs.calcFullDPS(b, "CALCULATOR", {}, { cachedPlayerDB = cP, cachedEnemyDB = cE, cachedMinionDB = cM, env = nil, triggerSourceCache = env.triggerSourceCache })
			out.SkillDPS, out.FullDPS, out.FullDotDPS = fd.skills, fd.combinedDPS, fd.TotalDotDPS
		else out.FullDPS, out.FullDotDPS = 0, 0 end
		if not skip_ehp then add_cost_warnings(calcs, env, out, "CALCULATOR") end
		local mdb = env.modDB
		if mdb and type(mdb.Sum) == "function" then
			out.ExtraPoints = mdb:Sum("BASE", nil, "ExtraPoints")
			local sc = { source = "Tree" }
			out["Spec:LifeInc"], out["Spec:ManaInc"], out["Spec:EnergyShieldInc"] = mdb:Sum("INC", sc, "Life"), mdb:Sum("INC", sc, "Mana"), mdb:Sum("INC", sc, "EnergyShield")
			out["Spec:ArmourInc"], out["Spec:EvasionInc"] = mdb:Sum("INC", sc, "Armour", "ArmourAndEvasion"), mdb:Sum("INC", sc, "Evasion", "ArmourAndEvasion")
		end
		local stats, sflags, warns, pts = flatten(out), {}, {}, nil
		local rf = env.player.mainSkill and env.player.mainSkill.skillFlags
		if type(rf) == "table" then for fk, fv in pairs(rf) do if type(fk) == "string" and type(fv) == "boolean" then sflags[fk] = fv end end end
		local wc = b.controls and b.controls.warnings
		if wc then
			-- RefreshStatList and EstimatePlayerProgress read calcsTab.mainEnv/mainOutput, so point them
			-- at this pass for the call, then put the sidebar back: get_sidebar and get_build_info read
			-- these controls and must keep describing mainOutput, not the last what-if.
			local pe, po, pl = b.calcsTab.mainEnv, b.calcsTab.mainOutput, wc.lines
			local sb, pd = b.controls.statBox and b.controls.statBox.list, b.controls.pointDisplay
			local sb_copy, pd_str, pd_req = {}, pd and pd.str, pd and pd.req
			for i, row in ipairs(sb or {}) do sb_copy[i] = row end
			b.calcsTab.mainEnv, b.calcsTab.mainOutput = env, out
			if skip_ehp then
				wc.lines = {}
				if type(b.EstimatePlayerProgress) == "function" then pcall(b.EstimatePlayerProgress, b) end
			elseif type(b.RefreshStatList) == "function" then
				pcall(b.RefreshStatList, b)
			end
			b.calcsTab.mainEnv, b.calcsTab.mainOutput = pe, po
			if not skip_ehp then
				local tr = (type(PobToolsGetTranslate) == "function" and PobToolsGetTranslate() and type(PobToolsTranslateDisplay) == "function") and PobToolsTranslateDisplay or nil
				for _, wl in ipairs(wc.lines or {}) do
					if type(wl) == "string" and wl ~= "" then warns[#warns + 1] = tr and tr(wl) or wl end
				end
			end
			if b.spec and type(b.spec.CountAllocNodes) == "function" then
				local u, a = b.spec:CountAllocNodes()
				pts = points_from_display(b, u, a)
			end
			wc.lines = pl
			if sb then
				for i = #sb, 1, -1 do sb[i] = nil end
				for i, row in ipairs(sb_copy) do sb[i] = row end
			end
			if pd then pd.str, pd.req = pd_str, pd_req end
		end
		if not pts and b.spec and type(b.spec.CountAllocNodes) == "function" then
			local u, a = b.spec:CountAllocNodes()
			pts = { used = u, ascUsed = a }
		end
		return { stats = stats, skillFlags = sflags, points = pts, warnings = warns }
	end
end
