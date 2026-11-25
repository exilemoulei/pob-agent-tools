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
	if want_gid and gid == want_gid then return true end
	if want_sid and sid == want_sid then return true end
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

return {
	rpc_handlers = {
		get_group_gem_states = handle_get_group_gem_states,
		apply_group_gem_states = handle_apply_group_gem_states,
	},
}
