-- Preserve and apply per-gem calculation state (skillPart, skillStageCount, minion/statSet flags)
-- and socket-group active-skill selection across group/gem mutations.
local host = require("pob_host")
local json_obj = host.json_obj

local GEM_STATE_FIELDS = {
	"skillPart",
	"skillPartCalcs",
	"skillStageCount",
	"skillStageCountCalcs",
	"skillMineCount",
	"skillMineCountCalcs",
	"skillMinion",
	"skillMinionCalcs",
	"skillMinionSkill",
	"skillMinionSkillCalcs",
	"skillMinionItemSet",
	"skillMinionItemSetCalcs",
	"statSet",
	"statSetCalcs",
	"enableGlobal1",
	"enableGlobal2",
}

local function copy_for_json(v)
	if type(v) ~= "table" then return v end
	local out = json_obj({})
	for k, x in pairs(v) do
		local xt = type(x)
		if xt ~= "function" and xt ~= "userdata" and xt ~= "thread" then
			out[k] = x
		end
	end
	return out
end

local function copy_from_json(v)
	if type(v) ~= "table" then return v end
	local out = {}
	for k, x in pairs(v) do out[k] = x end
	return out
end

local function resolve_socket_group(params)
	local group_idx = tonumber(params and params.group)
	if not group_idx or group_idx < 1 or group_idx ~= math.floor(group_idx) then
		error("group must be a positive integer")
	end
	local b = host.get_build()
	local st = b and b.skillsTab
	local g = st and st.socketGroupList and st.socketGroupList[group_idx]
	if not g then
		error("Skill group #" .. tostring(group_idx) .. " not found")
	end
	return b, st, g, group_idx
end

local function active_entry_identity(ds)
	local ae = type(ds) == "table" and ds.activeEffect or nil
	if type(ae) ~= "table" then return nil, nil, nil end
	local src = ae.srcInstance
	local gid = type(src) == "table" and (src.gemId or (src.gemData and src.gemData.id)) or nil
	local nspec = type(src) == "table" and (src.nameSpec or (src.gemData and src.gemData.name)) or nil
	local sid = (ae.grantedEffect and ae.grantedEffect.id) or (type(src) == "table" and src.skillId) or nil
	return gid, nspec, sid
end

local function matches_active_identity(ds, want_gid, want_name, want_sid)
	if not ds then return false end
	local gid, nspec, sid = active_entry_identity(ds)
	-- One gem can grant two skills (a Vaal gem and its base skill): only the skill id tells them apart.
	if want_sid and sid then return sid == want_sid end
	if want_gid and gid == want_gid then return true end
	if type(want_name) == "string" and want_name ~= "" and type(nspec) == "string" then
		if nspec:lower() == want_name:lower() then return true end
	end
	return false
end

local function align_active_skill_index(list, cur_idx, want_gid, want_name, want_sid)
	if type(list) ~= "table" or #list == 0 then return nil end
	if not want_gid and not want_name and not want_sid then return nil end
	local cur = list[cur_idx or 1]
	if matches_active_identity(cur, want_gid, want_name, want_sid) then return nil end
	for i, ds in ipairs(list) do
		if matches_active_identity(ds, want_gid, want_name, want_sid) then return i end
	end
	return nil
end

local function handle_get_group_gem_states(params)
	local _, _, g, group_idx = resolve_socket_group(params)
	local gems = {}
	for i, gem in ipairs(g.gemList or {}) do
		local entry = {
			index = i,
			gemId = gem.gemId or (gem.gemData and gem.gemData.id) or nil,
			nameSpec = gem.nameSpec or (gem.gemData and gem.gemData.name) or nil,
			skillId = gem.skillId
				or (gem.grantedEffect and gem.grantedEffect.id)
				or (gem.gemData and gem.gemData.grantedEffectId)
				or nil,
		}
		for _, field in ipairs(GEM_STATE_FIELDS) do
			local val = gem[field]
			if val ~= nil then entry[field] = copy_for_json(val) end
		end
		gems[i] = entry
	end
	local act_gid, act_name, act_sid = active_entry_identity(
		type(g.displaySkillList) == "table" and g.displaySkillList[g.mainActiveSkill or 1] or nil
	)
	local calcs_gid, calcs_name, calcs_sid = active_entry_identity(
		type(g.displaySkillListCalcs) == "table" and g.displaySkillListCalcs[g.mainActiveSkillCalcs or 1] or nil
	)
	return {
		group = group_idx,
		mainActiveSkill = g.mainActiveSkill,
		mainActiveSkillCalcs = g.mainActiveSkillCalcs,
		mainActiveGemId = act_gid,
		mainActiveNameSpec = act_name,
		mainActiveSkillId = act_sid,
		mainActiveCalcsGemId = calcs_gid,
		mainActiveCalcsNameSpec = calcs_name,
		mainActiveCalcsSkillId = calcs_sid,
		gems = gems,
	}
end

local function handle_apply_group_gem_states(params)
	local b, st, g, group_idx = resolve_socket_group(params)
	local gem_states = params and params.gemStates
	if type(gem_states) == "table" and type(g.gemList) == "table" then
		for _, entry in ipairs(gem_states) do
			local idx = tonumber(entry and entry.index)
			local gem = idx and g.gemList[idx] or nil
			if type(gem) == "table" then
				for _, field in ipairs(GEM_STATE_FIELDS) do
					local val = entry[field]
					if val ~= nil then gem[field] = copy_from_json(val) end
				end
			end
		end
	end
	if params and params.mainActiveSkill ~= nil then
		local mas = tonumber(params.mainActiveSkill)
		if mas and mas >= 1 then g.mainActiveSkill = math.floor(mas) end
	end
	if params and params.mainActiveSkillCalcs ~= nil then
		local masc = tonumber(params.mainActiveSkillCalcs)
		if masc and masc >= 1 then g.mainActiveSkillCalcs = math.floor(masc) end
	end
	if type(st.ProcessSocketGroup) == "function" then
		st:ProcessSocketGroup(g)
	end
	local realigned = false
	if params then
		local new_act = align_active_skill_index(
			g.displaySkillList,
			g.mainActiveSkill,
			params.mainActiveGemId,
			params.mainActiveNameSpec,
			params.mainActiveSkillId
		)
		if new_act and new_act ~= g.mainActiveSkill then
			g.mainActiveSkill = new_act
			realigned = true
		end
		local new_calcs = align_active_skill_index(
			g.displaySkillListCalcs,
			g.mainActiveSkillCalcs,
			params.mainActiveCalcsGemId,
			params.mainActiveCalcsNameSpec,
			params.mainActiveCalcsSkillId
		)
		if new_calcs and new_calcs ~= g.mainActiveSkillCalcs then
			g.mainActiveSkillCalcs = new_calcs
			realigned = true
		end
	end
	if realigned and type(st.ProcessSocketGroup) == "function" then
		st:ProcessSocketGroup(g)
	end
	b.buildFlag = true
	host.frame()
	return { ok = true, group = group_idx }
end

local function parse_shock_from_table(tbl, boss_tag)
	if type(tbl) ~= "table" or type(tbl.rowList) ~= "table" then return 0 end
	local needle = "(" .. tostring(boss_tag or "None") .. ")"
	local best = 0
	for _, row in ipairs(tbl.rowList) do
		if type(row) == "table" and type(row.thresh) == "string" then
			if row.thresh:find(needle, 1, true) then
				local eff = tostring(row.effect or ""):gsub("%^x%x%x%x%x%x%x", ""):gsub("%^%d", "")
				local num = tonumber(eff:match("^%s*([%d%.]+)"))
				if num and num > best then best = num end
			end
		end
	end
	return best
end

local function handle_get_shock_probe(_params)
	local b = host.get_build()
	local tab = b and b.calcsTab
	if not tab or not tab.calcs or type(tab.calcs.buildOutput) ~= "function" then
		return { ok = false, error = "calcs builder unavailable" }
	end
	local mg_idx = b.mainSocketGroup or 1
	local mg = b.skillsTab and b.skillsTab.socketGroupList and b.skillsTab.socketGroupList[mg_idx]
	local orig_sn = tab.input and tab.input.skill_number
	local orig_sm = tab.input and tab.input.showMinion
	local orig_masc = mg and mg.mainActiveSkillCalcs
	if tab.input then
		tab.input.skill_number = mg_idx
		tab.input.showMinion = false
	end
	if mg and mg.mainActiveSkill then
		mg.mainActiveSkillCalcs = mg.mainActiveSkill
	end
	local ok, calcsEnv = pcall(tab.calcs.buildOutput, b, "CALCS")
	if tab.input then
		tab.input.skill_number = orig_sn
		tab.input.showMinion = orig_sm
	end
	if mg then
		mg.mainActiveSkillCalcs = orig_masc
	end
	if not ok or type(calcsEnv) ~= "table" or type(calcsEnv.player) ~= "table" then
		return { ok = false, error = tostring(calcsEnv) }
	end
	local player = calcsEnv.player
	local out = player.output or {}
	local flags = (player.mainSkill and player.mainSkill.skillFlags) or {}
	local cfg_in = calcsEnv.configInput or {}
	local config_shocked = (cfg_in.conditionEnemyShocked and true) or false
	local boss_tag = tostring(cfg_in.enemyIsBoss or "None")
	local bd = player.breakdown or {}
	local computed = parse_shock_from_table(bd.ShockDPS, boss_tag)
	if type(bd.MainHand) == "table" then
		computed = math.max(computed, parse_shock_from_table(bd.MainHand.ShockDPS, boss_tag))
	end
	if type(bd.OffHand) == "table" then
		computed = math.max(computed, parse_shock_from_table(bd.OffHand.ShockDPS, boss_tag))
	end
	local cur_shock = tonumber(out.CurrentShock) or 0
	local shock_min = tonumber(out.ShockMinimum) or 0
	local non_cfg_floor = shock_min
	if not config_shocked and cur_shock > non_cfg_floor then
		non_cfg_floor = cur_shock
	end
	local can_shock = (flags.shock and true) or false
	if non_cfg_floor > computed then
		computed = non_cfg_floor
		if non_cfg_floor >= 5 then can_shock = true end
	end
	return {
		ok = true,
		canShock = can_shock,
		isProxy = (flags.totem or flags.trap or flags.mine or flags.minion) and true or false,
		configDerived = config_shocked,
		computedShockPct = computed,
		maxShockCap = tonumber(out.MaximumShock) or 50,
		currentShock = cur_shock,
		shockChance = tonumber(out.ShockChance) or tonumber(out.ShockChanceOnHit) or 0,
		critChance = tonumber(out.CritChance) or 0,
		speed = tonumber(out.Speed) or 0,
		hitChance = tonumber(out.HitChance) or 100,
		enemyIsBoss = boss_tag,
	}
end

return {
	rpc_handlers = {
		get_group_gem_states = handle_get_group_gem_states,
		apply_group_gem_states = handle_apply_group_gem_states,
		get_shock_probe = handle_get_shock_probe,
	},
}
