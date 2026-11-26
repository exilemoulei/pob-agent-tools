-- Node power ranking kernel + zero-cost jewel/cluster socket reachability probe.
-- Loaded by upstream_kernels.lua via require("node_power_kernel").

local H = require("pob_host")
local now_ms, get_build = H.now_ms, H.get_build

local M = {}

local function list_power_stat_names()
	local names = {}
	for _, entry in ipairs((data and data.powerStatList) or {}) do
		if entry.stat then
			names[#names + 1] = entry.stat
		end
	end
	return names
end

local function find_power_stat_exact(stat_name)
	for _, entry in ipairs((data and data.powerStatList) or {}) do
		if entry.stat == stat_name then
			return entry
		end
	end
	return nil
end

local function pick_power_stat(build, preferred, strict)
	if preferred and preferred ~= "" then
		local exact = find_power_stat_exact(preferred)
		if exact then
			return exact
		end
		if strict then
			error("unknown power stat: " .. tostring(preferred), 0)
		end
	end
	local out = (build.calcsTab and build.calcsTab.mainOutput) or {}
	if (out.FullDPS or 0) > 0 then
		local full = find_power_stat_exact("FullDPS")
		if full then
			return full
		end
	end
	return find_power_stat_exact("CombinedDPS") or (data and data.powerStatList and data.powerStatList[3])
end

-- Wrap CalcsTab:PowerBuilder so non-FullDPS passes never trigger Calcs.lua's redundant FullDPS TREE pass.
local function wrap_calcs_power_builder(calcs_tab)
	if not calcs_tab or calcs_tab._pob4agents_pb_wrapped then
		return
	end
	local target = getmetatable(calcs_tab)
	local host = (type(target) == "table" and type(target.__index) == "table" and target.__index.PowerBuilder)
		and target.__index
		or calcs_tab
	if host._pob4agents_pb_wrapped or type(host.PowerBuilder) ~= "function" then
		return
	end
	local orig_pb = host.PowerBuilder
	host.PowerBuilder = function(self, ...)
		local b = self and self.build
		local saved_view = b and b.viewMode
		local stat_name = self and self.powerStat and self.powerStat.stat
		if b and stat_name and stat_name ~= "FullDPS" then
			b.viewMode = "CALCS"
		end
		local ok, err = pcall(orig_pb, self, ...)
		if b and saved_view ~= nil then
			b.viewMode = saved_view
		end
		if not ok then
			error(err, 0)
		end
	end
	host._pob4agents_pb_wrapped = true
	calcs_tab._pob4agents_pb_wrapped = true
end

local function sector_label(x, y)
	if type(x) ~= "number" or type(y) ~= "number" then
		return nil
	end
	local deg = math.atan2(y, x) * 180 / math.pi
	if deg >= -150 and deg < -90 then
		return "NW (Witch/Templar)"
	elseif deg >= -90 and deg < -30 then
		return "NE (Witch/Shadow)"
	elseif deg >= -30 and deg < 30 then
		return "E (Shadow/Ranger)"
	elseif deg >= 30 and deg < 90 then
		return "SE (Ranger/Duelist)"
	elseif deg >= 90 and deg < 150 then
		return "SW (Duelist/Marauder)"
	end
	return "W (Marauder/Templar)"
end

local function classify_socket(node)
	if
		node.charm
		or node.charmSocket
		or node.asc
		or node.ascendancyName
		or node.name == "Charm Socket"
		or node.dn == "Charm Socket"
	then
		return nil
	end
	local ej = node.expansionJewel
	if type(ej) ~= "table" then
		return "basic"
	end
	if ej.size == 2 and ej.parent == nil then
		return "large_cluster_outer"
	elseif ej.size == 1 then
		-- Active inner cluster sockets injected by BuildSubgraph keep static <65536 tree IDs with ej.parent ~= nil.
		return "medium_cluster_inner"
	end
	return "small_cluster_inner"
end

local function collect_nearby_sockets(build, max_dist)
	local summary = {
		allocatedTotal = 0,
		allocatedOccupied = 0,
		allocatedEmptyIds = {},
		activeOuterClusters = 0,
	}
	local nearby = {}
	local spec = build and build.spec
	if not spec or type(spec.nodes) ~= "table" then
		return summary, nearby
	end
	local items_tab = build.itemsTab
	local sock_map = items_tab and items_tab.sockets or {}
	local max_basic = max_dist and math.min(math.max(math.floor(max_dist), 3), 8) or 3
	local max_outer = max_dist and math.min(math.max(math.floor(max_dist), 4), 8) or 4

	for id, node in pairs(spec.nodes) do
		if type(id) == "number" and node.type == "Socket" then
			local kind = classify_socket(node)
			if kind then
				local alloc = node.alloc and true or false
				local sock_ctrl = sock_map[id]
				local sel_id = sock_ctrl and tonumber(sock_ctrl.selItemId) or 0
				local occupied = sel_id > 0
				if alloc then
					summary.allocatedTotal = summary.allocatedTotal + 1
					if occupied then
						summary.allocatedOccupied = summary.allocatedOccupied + 1
						local occupant = items_tab and items_tab.items and items_tab.items[sel_id]
						local is_cluster = occupant
							and (
								occupant.clusterJewel ~= nil
								or (
									occupant.jewelData
									and (occupant.jewelData.clusterJewelValid or occupant.jewelData.blightcoreNotables)
								)
							)
						if kind == "large_cluster_outer" and is_cluster then
							summary.activeOuterClusters = summary.activeOuterClusters + 1
						end
					else
						summary.allocatedEmptyIds[#summary.allocatedEmptyIds + 1] = id
						nearby[#nearby + 1] = {
							nodeId = id,
							kind = kind,
							sector = kind == "large_cluster_outer" and sector_label(node.x, node.y) or nil,
							allocated = true,
							occupied = false,
							cost = 0,
							pathIds = {},
							travelStats = {},
						}
					end
				else
					local dist = tonumber(node.pathDist)
					local lim = (kind == "large_cluster_outer") and max_outer or max_basic
					if dist and dist >= 1 and dist <= lim then
						local path_ids, travel_stats, seen_st = {}, {}, {}
						local raw_path = type(node.path) == "table" and node.path or {}
						for j = #raw_path, 1, -1 do
							local pn = raw_path[j]
							if type(pn) == "table" and type(pn.id) == "number" then
								path_ids[#path_ids + 1] = pn.id
								if pn.id ~= id and type(pn.sd) == "table" then
									for _, ln in ipairs(pn.sd) do
										local clean = tostring(ln or ""):gsub("%s*\r?\n%s*", " ")
										if clean ~= "" and not seen_st[clean] and #travel_stats < 4 then
											seen_st[clean] = true
											travel_stats[#travel_stats + 1] = clean
										end
									end
								end
							end
						end
						if #path_ids == 0 then
							path_ids[1] = id
						end
						nearby[#nearby + 1] = {
							nodeId = id,
							kind = kind,
							sector = kind == "large_cluster_outer" and sector_label(node.x, node.y) or nil,
							allocated = false,
							occupied = false,
							cost = math.floor(dist),
							pathIds = path_ids,
							travelStats = travel_stats,
						}
					end
				end
			end
		end
	end
	table.sort(summary.allocatedEmptyIds)
	table.sort(nearby, function(a, b)
		if a.cost ~= b.cost then
			return a.cost < b.cost
		end
		return a.nodeId < b.nodeId
	end)
	return summary, nearby
end

local function handle_node_power(params)
	params = type(params) == "table" and params or {}
	local build = get_build()
	wrap_calcs_power_builder(build.calcsTab)

	local req_stat = params.stat
	local power_stat = pick_power_stat(build, req_stat, req_stat ~= nil and req_stat ~= "")
	if not power_stat then
		error("unknown power stat: " .. tostring(req_stat), 0)
	end

	local max_depth = tonumber(params.maxDepth or params.max_depth)
	local saved_view = build.viewMode
	local saved_depth = build.calcsTab.nodePowerMaxDepth
	if power_stat.stat ~= "FullDPS" then
		build.viewMode = "CALCS"
	end
	build.calcsTab.powerStat = power_stat
	build.calcsTab.nodePowerMaxDepth = max_depth

	local t0 = now_ms()
	local ok, report_or_err = pcall(function()
		if build.spec and build.spec.nodes and type(build.spec.AddMasteryEffectOptionsToNode) == "function" then
			for _, nd in pairs(build.spec.nodes) do
				if nd.type == "Mastery" and not nd.alloc and not nd.allMasteryOptions and nd.overrideType ~= "AlternateMastery" then
					build.spec:AddMasteryEffectOptionsToNode(nd)
				end
			end
		end
		build.calcsTab:PowerBuilder()
		return build.treeTab:BuildPowerReportList(power_stat)
	end)
	build.viewMode = saved_view
	build.calcsTab.nodePowerMaxDepth = saved_depth
	if not ok then
		error(report_or_err, 0)
	end
	local elapsed_ms = now_ms() - t0

	local raw_report = report_or_err or {}
	local report = {}
	local cluster_count = 0
	local used_eff = {}
	local mult = (power_stat and power_stat.powerMultiplier) or 1
	local m_defs = build.spec and build.spec.tree and build.spec.tree.masteryEffects
	local do_tr = type(PobToolsGetTranslate) == "function"
		and PobToolsGetTranslate()
		and type(PobToolsTranslateDisplay) == "function"
	local tr_fn = do_tr and PobToolsTranslateDisplay or nil

	local function translate_lines(lines)
		if not tr_fn or type(lines) ~= "table" then
			return nil
		end
		local out_zh = {}
		for j, ln in ipairs(lines) do
			out_zh[j] = tr_fn(tostring(ln or ""))
		end
		return out_zh
	end

	for i, row in ipairs(raw_report) do
		if row.pathDist == "Cluster" then
			cluster_count = cluster_count + 1
		end
		local r_name, r_sd, eff_id = row.name, row.sd, row.effectId
		local alloc_spec = row.id ~= nil and tostring(row.id) or nil
		local r_name_zh, r_sd_zh = nil, nil
		if row.type == "Mastery" then
			local nd = build.spec and build.spec.nodes and build.spec.nodes[row.id]
			if not eff_id and nd and nd.power and type(nd.power.masteryEffects) == "table" then
				local u = used_eff[row.id] or {}
				used_eff[row.id] = u
				local best_id, best_diff = nil, math.huge
				for eid, ep in pairs(nd.power.masteryEffects) do
					if not u[eid] then
						local d = math.abs(((ep and ep.singleStat) or 0) * mult - (row.power or 0))
						if d < best_diff or (d == best_diff and (not best_id or eid < best_id)) then
							best_id, best_diff = eid, d
						end
					end
				end
				if best_id then
					eff_id = best_id
					u[best_id] = true
				end
			end
			if eff_id then
				alloc_spec = tostring(row.id) .. ":" .. tostring(eff_id)
				local edef = m_defs and m_defs[eff_id]
				local raw_esd = edef and (edef.sd or edef.stats)
				if type(raw_esd) == "table" and #raw_esd > 0 then
					local esd = {}
					for j, ln in ipairs(raw_esd) do
						esd[j] = tostring(ln):gsub("%s*\r?\n%s*", " ")
					end
					r_sd = esd
					local base_nm = tostring((nd and nd.dn) or r_name or "Mastery"):gsub("%s*\r?\n%s*", " ")
					base_nm = base_nm:match("^(.-)%s*:") or base_nm:match("^(.-)%s+/%s+") or base_nm
					r_name = base_nm .. ": " .. table.concat(esd, "; ")
					if tr_fn then
						r_sd_zh = translate_lines(esd)
						local base_zh = tr_fn(base_nm)
						r_name_zh = base_zh .. ": " .. table.concat(r_sd_zh or esd, "; ")
					end
				elseif tr_fn and type(r_name) == "string" then
					r_name_zh = tr_fn(r_name)
					r_sd_zh = translate_lines(r_sd)
				end
			elseif tr_fn and type(r_name) == "string" then
				r_name_zh = tr_fn(r_name)
				r_sd_zh = translate_lines(r_sd)
			end
		elseif tr_fn then
			if type(r_name) == "string" and r_name ~= "" then
				r_name_zh = tr_fn(r_name)
			end
			if type(r_sd) == "table" and #r_sd > 0 then
				r_sd_zh = translate_lines(r_sd)
			end
		end
		report[i] = {
			id = row.id,
			effectId = eff_id,
			allocSpec = alloc_spec,
			name = r_name,
			nameZh = r_name_zh,
			type = row.type,
			allocated = row.allocated and true or false,
			power = row.power or 0,
			powerStr = row.powerStr,
			pathPower = row.pathPower or 0,
			pathPowerStr = row.pathPowerStr,
			pathDist = row.pathDist,
			sd = r_sd,
			sdZh = r_sd_zh,
			x = row.x,
			y = row.y,
		}
	end

	local sock_summary, nearby_sockets = collect_nearby_sockets(build, max_depth)
	return {
		stat = power_stat.stat,
		stats = list_power_stat_names(),
		report = report,
		clusterCount = cluster_count,
		socketSummary = sock_summary,
		nearbySockets = nearby_sockets,
		elapsedMs = elapsed_ms,
	}
end

M.list_power_stat_names = list_power_stat_names
M.find_power_stat_exact = find_power_stat_exact
M.pick_power_stat = pick_power_stat
M.collect_nearby_sockets = collect_nearby_sockets
M.handle_node_power = handle_node_power

return M
