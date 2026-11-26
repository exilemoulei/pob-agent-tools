-- Zero-residue batch passive path CoW evaluator using PoB's GetMiscCalculator().
-- Mounted in headless_runner.lua (`eval_node_paths` RPC, caps.evalNodePaths = true).

local H = require("pob_host")
local json_obj, now_ms, get_build = H.json_obj, H.now_ms, H.get_build

local M = {}

local STAT_KEYS = {
	"CombinedDPS",
	"FullDPS",
	"TotalDPS",
	"TotalDotDPS",
	"TotalEHP",
	"Life",
	"LifeUnreserved",
	"LifeUnreservedPercent",
	"EnergyShield",
	"Mana",
	"ManaUnreserved",
	"ManaUnreservedPercent",
	"ManaReservationEfficiency",
	"ManaCost",
	"LifeCost",
	"Str",
	"Dex",
	"Int",
	"ReqStr",
	"ReqDex",
	"ReqInt",
	"FireResist",
	"ColdResist",
	"LightningResist",
	"ChaosResist",
	"FireResistMax",
	"ColdResistMax",
	"LightningResistMax",
	"ChaosResistMax",
	"FireResistOverCap",
	"ColdResistOverCap",
	"LightningResistOverCap",
	"ChaosResistOverCap",
	"HitChance",
	"EffectiveSpellSuppressionChance",
	"SpellSuppressionChance",
	"Speed",
	"SkillTriggerRate",
	"TriggerRateCap",
	"EffectiveSourceRate",
	"ActionSpeedMod",
	"Cooldown",
	"PhysicalMaximumHitTaken",
	"FireMaximumHitTaken",
	"ColdMaximumHitTaken",
	"LightningMaximumHitTaken",
	"ChaosMaximumHitTaken",
}

local function finite_num(v)
	if type(v) ~= "number" then
		return nil
	end
	if v ~= v or v == math.huge or v == -math.huge then
		return nil
	end
	return v
end

local function extract_stats(out)
	local res = json_obj({})
	if type(out) ~= "table" then
		return res
	end
	for _, k in ipairs(STAT_KEYS) do
		local v = finite_num(out[k])
		if v ~= nil then
			res[k] = v
		end
	end
	-- Surface any extra trigger/cooldown numeric outputs exposed by PoB for trigger builds.
	for k, v in pairs(out) do
		if type(k) == "string" and res[k] == nil then
			local kl = k:lower()
			if kl:find("trigger", 1, true) or kl:find("cooldown", 1, true) then
				local nv = finite_num(v)
				if nv ~= nil then
					res[k] = nv
				end
			end
		end
	end
	return res
end

local function compute_deltas(base_stats, cand_stats)
	local deltas = json_obj({})
	local seen = {}
	for k, bv in pairs(base_stats) do
		seen[k] = true
		local cv = cand_stats[k]
		if type(cv) == "number" and type(bv) == "number" then
			deltas[k] = cv - bv
		end
	end
	for k, cv in pairs(cand_stats) do
		if not seen[k] and type(cv) == "number" then
			deltas[k] = cv
		end
	end
	return deltas
end

local function build_mastery_effect_node(spec, node, effect)
	local effect_node = {
		id = node.id,
		type = node.type,
		name = node.name,
		sd = {},
	}
	local src_sd = effect.sd or effect.stats or {}
	for i, line in ipairs(src_sd) do
		effect_node.sd[i] = line
	end
	spec.tree:ProcessStats(effect_node)
	return effect_node
end

local function lookup_mastery_effect_id(mastery_map, nid)
	if type(mastery_map) ~= "table" then
		return nil
	end
	local raw = mastery_map[tostring(nid)]
	if raw == nil then
		raw = mastery_map[nid]
	end
	return tonumber(raw)
end

local function resolve_candidate_override(spec, mode, node_ids, mastery_map)
	local add_nodes = nil
	local remove_nodes = nil
	local count = 0
	local m_defs = spec.tree and spec.tree.masteryEffects or {}

	for _, raw_id in ipairs(node_ids) do
		local nid = tonumber(raw_id)
		if nid then
			local nd = spec.nodes and spec.nodes[nid]
			if not nd then
				return nil, "unknown nodeId: " .. tostring(nid), 0
			end
			count = count + 1
			if mode == "remove" then
				local alloc_nd = spec.allocNodes and spec.allocNodes[nid]
				if alloc_nd then
					remove_nodes = remove_nodes or {}
					remove_nodes[alloc_nd] = true
				end
			else
				local eff_id = lookup_mastery_effect_id(mastery_map, nid)
				if nd.type == "Mastery" and eff_id then
					local edef = m_defs[eff_id]
					if not edef then
						return nil, "unknown mastery effectId: " .. tostring(eff_id), 0
					end
					local eff_node = build_mastery_effect_node(spec, nd, edef)
					add_nodes = add_nodes or {}
					add_nodes[eff_node] = true
					-- In CalcSetup.lua, env.spec.allocNodes overwrites addNodes unless removed too.
					local cur_alloc = spec.allocNodes and spec.allocNodes[nid]
					if cur_alloc then
						remove_nodes = remove_nodes or {}
						remove_nodes[cur_alloc] = true
					end
				elseif not (spec.allocNodes and spec.allocNodes[nid]) then
					add_nodes = add_nodes or {}
					add_nodes[nd] = true
				end
			end
		end
	end

	local ov = {}
	if add_nodes then
		ov.addNodes = add_nodes
	end
	if remove_nodes then
		ov.removeNodes = remove_nodes
	end
	return ov, nil, count
end

local function handle_eval_node_paths(params)
	params = type(params) == "table" and params or {}
	local build = get_build()
	local spec = build and build.spec
	local calcs_tab = build and build.calcsTab
	if not spec or not calcs_tab or type(calcs_tab.GetMiscCalculator) ~= "function" then
		error("eval_node_paths requires an active build with GetMiscCalculator", 0)
	end

	local candidates = params.candidates
	if type(candidates) ~= "table" then
		error("eval_node_paths requires candidates array", 0)
	end

	local t0 = now_ms()
	local calc_func, calc_base = calcs_tab:GetMiscCalculator()
	local use_full_dps
	if params.useFullDPS ~= nil then
		use_full_dps = params.useFullDPS == true
	else
		use_full_dps = ((calc_base and calc_base.FullDPS) or 0) > 0
	end

	local base_stats = extract_stats(calc_base)
	local results = {}

	for idx, cand in ipairs(candidates) do
		local c_key = tostring((type(cand) == "table" and cand.key) or ("cand_" .. tostring(idx)))
		local mode = tostring((type(cand) == "table" and cand.mode) or "remove"):lower()
		if mode ~= "add" and mode ~= "remove" then
			results[#results + 1] = {
				key = c_key,
				mode = mode,
				error = "invalid mode (expected 'add' or 'remove')",
			}
		else
			local node_ids = (type(cand) == "table" and type(cand.nodeIds) == "table") and cand.nodeIds or {}
			local mastery_map = (type(cand) == "table" and type(cand.masteryEffects) == "table")
					and cand.masteryEffects
				or nil
			local ov, ov_err, n_cnt = resolve_candidate_override(spec, mode, node_ids, mastery_map)
			if ov_err then
				results[#results + 1] = {
					key = c_key,
					mode = mode,
					nodeCount = n_cnt,
					error = ov_err,
				}
			else
				local ok, out_or_err = pcall(function()
					if not ov.addNodes and not ov.removeNodes then
						return calc_base
					end
					return calc_func(ov, use_full_dps)
				end)
				if not ok then
					results[#results + 1] = {
						key = c_key,
						mode = mode,
						nodeCount = n_cnt,
						error = tostring(out_or_err),
					}
				else
					local cand_stats = extract_stats(out_or_err)
					results[#results + 1] = {
						key = c_key,
						mode = mode,
						nodeCount = n_cnt,
						outputStats = cand_stats,
						deltas = compute_deltas(base_stats, cand_stats),
					}
				end
			end
		end
	end

	return {
		useFullDPS = use_full_dps,
		baseStats = base_stats,
		results = results,
		evaluatedCount = #results,
		elapsedMs = now_ms() - t0,
	}
end

local function collect_mana_reservation_info()
	local ok, build = pcall(get_build)
	local env = ok and build and build.calcsTab and build.calcsTab.mainEnv
	if type(env) ~= "table" or type(env.player) ~= "table" then
		return nil
	end
	local mod_db = env.player.modDB
	if type(mod_db) ~= "table" or type(mod_db.Sum) ~= "function" then
		return nil
	end
	local mre_inc = finite_num(mod_db:Sum("INC", nil, "ManaReservationEfficiency", "ReservationEfficiency")) or 0
	local mre_more = finite_num(mod_db:More(nil, "ManaReservationEfficiency", "ReservationEfficiency")) or 1
	local rows = {}
	local st_has_res = SkillType and SkillType.HasReservation
	local st_becomes_cost = SkillType and SkillType.ReservationBecomesCost

	for _, sk in ipairs(env.player.activeSkillList or {}) do
		local s_types = sk.skillTypes or {}
		local s_data = sk.skillData or {}
		local s_flags = sk.skillFlags or {}
		if
			((st_has_res and s_types[st_has_res]) or s_data.triggeredByAutoexertion)
			and not (st_becomes_cost and s_types[st_becomes_cost])
			and not s_flags.disable
		then
			local sm = sk.skillModList
			local cfg = sk.skillCfg
			local is_bm = sm and sm:Flag(cfg, "BloodMagicReserved") and not s_data.requiresManaReservation
			if sm and not is_bm then
				local mult_raw = sm:More(cfg, "SupportManaMultiplier") or 1
				local mult = math.floor(mult_raw * 10000) / 10000
				local gel = sk.activeEffect and sk.activeEffect.grantedEffectLevel
				local base_flat = s_data.manaReservationFlat or (gel and gel.manaReservationFlat) or 0
				if sm:Flag(cfg, "ManaCostGainAsReservation") and gel and gel.cost then
					base_flat = (sm:Sum("BASE", cfg, "ManaCostBase") or 0) + (gel.cost.Mana or 0)
				end
				local base_pct = s_data.manaReservationPercent or (gel and gel.manaReservationPercent) or 0
				local forced_flat = finite_num(s_data.ManaReservationFlatForced)
				local forced_pct = finite_num(s_data.ManaReservationPercentForced)
				if base_flat ~= 0 or base_pct ~= 0 or forced_flat ~= nil or forced_pct ~= nil then
					local res_more = sm:More(cfg, "ManaReserved") or 1
					local gen_more = sm:More(cfg, "Reserved") or 1
					local inc = sm:Sum("INC", cfg, "ManaReserved", "Reserved") or 0
					local eff = math.max(sm:Sum("INC", cfg, "ManaReservationEfficiency", "ReservationEfficiency") or 0, -100)
					local eff_more = sm:More(cfg, "ManaReservationEfficiency", "ReservationEfficiency") or 1
					local mod_flat = 0
					local bf_val = math.modf(base_flat * mult)
					if bf_val ~= 0 then
						mod_flat = bf_val + math.modf(bf_val * inc / 100)
						mod_flat = mod_flat + math.modf(mod_flat * (res_more - 1))
						mod_flat = mod_flat + math.modf(mod_flat * (gen_more - 1))
					end
					local mod_pct = 0
					local bp_val = math.modf(base_pct * 100 * mult)
					if bp_val ~= 0 then
						mod_pct = bp_val + math.modf(bp_val * inc / 100)
						mod_pct = mod_pct + math.modf(mod_pct * (res_more - 1))
						mod_pct = mod_pct + math.modf(mod_pct * (gen_more - 1))
					end
					local mult_cnt = 1
					if sk.activeMineCount then
						mult_cnt = mult_cnt * sk.activeMineCount
					end
					if cfg and cfg.skillName == "Blood Sacrament" and sk.activeStageCount then
						mult_cnt = mult_cnt * (sk.activeStageCount + 1)
					end
					local ge = sk.activeEffect and sk.activeEffect.grantedEffect
					rows[#rows + 1] = {
						skillName = (ge and ge.name) or "Skill",
						modifiedFlat = mod_flat,
						modifiedPercent = mod_pct,
						efficiency = eff,
						efficiencyMore = eff_more,
						forcedFlat = forced_flat,
						forcedPercent = forced_pct,
						multCount = mult_cnt,
					}
				end
			end
		end
	end
	return {
		mreInc = mre_inc,
		mreMore = mre_more,
		reservations = rows,
	}
end

function M.enrich_stats(out)
	if type(out) ~= "table" or type(out.stats) ~= "table" then
		return
	end
	local info = collect_mana_reservation_info()
	if not info then
		return
	end
	if out.stats.ManaReservationEfficiency == nil then
		out.stats.ManaReservationEfficiency = info.mreInc
	end
	out.manaReservationEfficiency = info.mreInc
	out.manaReservationEfficiencyMore = info.mreMore
	out.manaReservations = info.reservations
end

function M.enrich_agent_probe(out)
	if type(out) ~= "table" or type(out.resourceSlack) ~= "table" then
		return
	end
	local info = collect_mana_reservation_info()
	if not info then
		return
	end
	out.resourceSlack.manaReservationEfficiency = info.mreInc
	out.resourceSlack.manaReservationEfficiencyMore = info.mreMore
	out.resourceSlack.manaReservations = info.reservations
end

M.rpc_handlers = {
	eval_node_paths = handle_eval_node_paths,
}

return M
