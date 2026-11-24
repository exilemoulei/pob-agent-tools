-- Zero-intrusion upstream PoB kernel RPCs:
--   - M.install_post_bridge(): upgrades node_power to skip redundant FullDPS pass when stat ~= "FullDPS"
--   - M.rpc_handlers.node_power: fast PowerBuilder + BuildPowerReportList (includes Cluster notables)
--   - M.rpc_handlers.power_diff_attribution: CompareTab:ComparePowerBuilder attribution (non-compare_* RPC)
--   - M.rpc_handlers.trade_generate_query: synchronous TradeQueryGenerator coroutine driver

local dkjson = require("dkjson")

local M = {
	_installed = false,
	_cached_tqg = nil,
	_stat_info = nil,
}

local function json_obj(t)
	return setmetatable(t or {}, { __jsontype = "object" })
end

local PSEUDO_HASH_MAP = {
	["3372524247"] = { "pseudo.pseudo_total_fire_resistance", "+#% total to Fire Resistance" },
	["4220027924"] = { "pseudo.pseudo_total_cold_resistance", "+#% total to Cold Resistance" },
	["1671376347"] = { "pseudo.pseudo_total_lightning_resistance", "+#% total to Lightning Resistance" },
	["2923486259"] = { "pseudo.pseudo_total_chaos_resistance", "+#% total to Chaos Resistance" },
	["4080418644"] = { "pseudo.pseudo_total_strength", "+# total to Strength" },
	["3261801346"] = { "pseudo.pseudo_total_dexterity", "+# total to Dexterity" },
	["328541901"] = { "pseudo.pseudo_total_intelligence", "+# total to Intelligence" },
}

local function now_ms()
	if type(HostNowMs) == "function" then
		return HostNowMs()
	end
	if type(GetTime) == "function" then
		return GetTime()
	end
	return 0
end

local function get_build()
	local m = main or __mainObject__
	local b = m and m.modes and m.modes["BUILD"]
	if not b then
		error("No active PoB build loaded", 0)
	end
	if b.buildFlag and b.calcsTab and b.calcsTab.BuildOutput then
		b.calcsTab:BuildOutput()
		b.buildFlag = false
	end
	return b
end

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

function M.install_post_bridge()
	if M._installed then
		return true
	end
	local m = main or __mainObject__
	local b = m and m.modes and m.modes["BUILD"]
	if b and b.calcsTab then
		wrap_calcs_power_builder(b.calcsTab)
	end
	M._installed = true
	return true
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
	for i, row in ipairs(raw_report) do
		if row.pathDist == "Cluster" then
			cluster_count = cluster_count + 1
		end
		report[i] = {
			id = row.id,
			name = row.name,
			type = row.type,
			allocated = row.allocated and true or false,
			power = row.power or 0,
			powerStr = row.powerStr,
			pathPower = row.pathPower or 0,
			pathPowerStr = row.pathPowerStr,
			pathDist = row.pathDist,
			sd = row.sd,
			x = row.x,
			y = row.y,
		}
	end

	return {
		stat = power_stat.stat,
		stats = list_power_stat_names(),
		report = report,
		clusterCount = cluster_count,
		elapsedMs = elapsed_ms,
	}
end

local function decode_share_code_if_needed(xml_or_code)
	if type(xml_or_code) ~= "string" or xml_or_code == "" then
		return nil
	end
	if xml_or_code:match("^%s*<") then
		return xml_or_code
	end
	local ok_b64, b64 = pcall(require, "base64")
	if ok_b64 and b64 and type(b64.decode) == "function" and type(Inflate) == "function" then
		local norm = xml_or_code:gsub("%-", "+"):gsub("_", "/")
		local decoded = b64.decode(norm)
		if decoded then
			local inflated = Inflate(decoded)
			if type(inflated) == "string" and inflated:match("<PathOfBuilding") then
				return inflated
			end
		end
	end
	return xml_or_code
end

local function handle_power_diff_attribution(params)
	params = type(params) == "table" and params or {}
	local build = get_build()
	local raw_xml = params.compareXml or params.compare_xml
	if type(raw_xml) ~= "string" or raw_xml == "" then
		error("power_diff_attribution requires compareXml string", 0)
	end
	local compare_xml = decode_share_code_if_needed(raw_xml)
	local label = params.label or "CompareBuild"
	local power_stat = pick_power_stat(build, params.stat, false)
	local categories = type(params.categories) == "table" and params.categories or {
		treeNodes = true,
		items = true,
		skillGems = true,
		supportGems = true,
		config = true,
	}
	local max_diffs = tonumber(params.limit) or 50

	local t0 = now_ms()
	local compare_tab = new("CompareTab"):CompareTab(build)
	local ok_imp = compare_tab:ImportBuild(compare_xml, label)
	if not ok_imp then
		error("CompareTab:ImportBuild failed", 0)
	end
	local entry = compare_tab:GetActiveCompare()
	if entry and entry.buildFlag then
		entry:Rebuild()
	end
	compare_tab:ComparePowerBuilder(entry, power_stat, categories)
	local elapsed_ms = now_ms() - t0

	local results = compare_tab.comparePowerResults or {}
	table.sort(results, function(a, b)
		return math.abs(a.impact or 0) > math.abs(b.impact or 0)
	end)

	local by_category = {}
	local diffs = {}
	for _, r in ipairs(results) do
		local cat = r.category or "Other"
		by_category[cat] = (by_category[cat] or 0) + 1
		if #diffs < max_diffs then
			diffs[#diffs + 1] = {
				category = cat,
				name = r.name,
				impact = r.impact or 0,
				impactPct = r.impactPercent,
				pathDist = r.pathDist,
			}
		end
	end

	return {
		stat = power_stat and power_stat.stat or "CombinedDPS",
		totalDiffs = #results,
		diffs = diffs,
		byCategory = by_category,
		elapsedMs = elapsed_ms,
	}
end

local function get_stat_info(tqg)
	if M._stat_info then
		return M._stat_info
	end
	local labels, explicit_ids, unique_only = {}, {}, {}
	for _, pair in pairs(PSEUDO_HASH_MAP) do
		labels[pair[1]] = pair[2]
	end
	local ok_ts, trade_stats = pcall(require, "Data.TradeSiteStats")
	if ok_ts and type(trade_stats) == "table" then
		for _, cat in ipairs(trade_stats) do
			for _, entry in ipairs((type(cat) == "table" and cat.entries) or {}) do
				if entry.id and entry.text then
					labels[entry.id] = entry.text
				end
			end
		end
	end
	local md = (tqg and tqg.modData) or {}
	for _, e in pairs(md.Explicit or {}) do
		local tm = e and e.tradeMod
		if tm and tm.id then
			explicit_ids[tm.id] = true
			if tm.text and not labels[tm.id] then labels[tm.id] = tm.text end
		end
	end
	for group_name, group_tbl in pairs(md) do
		if type(group_tbl) == "table" and group_name ~= "Explicit" then
			local is_u = (group_name == "WatchersEye" or group_name == "PassiveNode")
			for _, e in pairs(group_tbl) do
				local tm = e and e.tradeMod
				if tm and tm.id then
					if tm.text and not labels[tm.id] then labels[tm.id] = tm.text end
					if is_u and not explicit_ids[tm.id] then unique_only[tm.id] = true end
				end
			end
		end
	end
	M._stat_info = { labels = labels, unique_only = unique_only }
	return M._stat_info
end

local function is_rare_rollable_stat(stat_id, label, unique_only)
	if type(stat_id) ~= "string" then return false end
	if not (stat_id:match("^explicit%.") or stat_id:match("^crafted%.") or stat_id:match("^fractured%.") or stat_id:match("^pseudo%.")) then
		return false
	end
	if unique_only[stat_id] or stat_id:find("^explicit%.pseudo_timeless_jewel_") or stat_id:find("|", 1, true) then
		return false
	end
	local lbl = tostring(label or ""):lower()
	if lbl == "" then return true end
	local noise_pats = {
		"per power charge", "per frenzy charge", "per endurance charge",
		"minimum power charge", "minimum frenzy charge", "minimum endurance charge",
		"minimum endurance, frenzy", "while affected by ", "in your presence",
	}
	for _, pat in ipairs(noise_pats) do
		if lbl:find(pat, 1, true) then return false end
	end
	if lbl:match("^allocates ") or lbl:match("^1 added passive skill is ") then
		return false
	end
	return true
end

local function build_slot_constraints(build, slot_name)
	local env = build.calcsTab and build.calcsTab.mainEnv
	local item = env and env.player and env.player.itemList and env.player.itemList[slot_name]
	local mod_db = env and env.player and env.player.modDB
	local lb_attrs = { Str = 0, Dex = 0, Int = 0 }
	if item and mod_db then
		local slot_obj = build.itemsTab and build.itemsTab.slots and build.itemsTab.slots[slot_name]
		local snum = (slot_obj and slot_obj.slotNum) or 1
		local src = item.modList or (item.slotModList and item.slotModList[snum]) or {}
		for _, mod in ipairs(src) do
			if mod.type == "BASE" and type(mod.value) == "number" and (mod.flags or 0) == 0 and (mod.keywordFlags or 0) == 0 and lb_attrs[mod.name] ~= nil then
				lb_attrs[mod.name] = lb_attrs[mod.name] + mod.value
			end
		end
	end
	local attr_deficits, res_deficits = json_obj({}), json_obj({})
	local bricked, bricked_cnt = {}, 0
	local probe_fn = rawget(_G, "__pob4agents_probe_build")
	local ok_p, probe = false, nil
	if type(probe_fn) == "function" then
		ok_p, probe = pcall(probe_fn, {})
	end
	if ok_p and type(probe) == "table" then
		for _, lb in ipairs(probe.loadBearingGear or {}) do
			if lb.slot == slot_name then
				if type(lb.attrDelta) == "table" then lb_attrs = lb.attrDelta end
				bricked_cnt = tonumber(lb.brickedCount) or 0
				bricked = lb.bricked or {}
				for _, b in ipairs(bricked) do
					for a, m in pairs(b.missing or {}) do
						local prev = attr_deficits[a]
						if not prev or (m.deficit or 0) < (prev.deficit or 0) then
							attr_deficits[a] = {
								required = m.required, afterRemoval = m.afterRemoval,
								deficit = m.deficit, minNeeded = math.max(0, (m.required or 0) - (m.afterRemoval or 0)),
							}
						end
					end
				end
				for elem, rd in pairs(lb.resistDeficits or {}) do
					res_deficits[elem] = {
						beforeSlack = rd.beforeSlack, afterSlack = rd.afterSlack,
						lost = rd.lost, minNeeded = math.max(0, -(rd.afterSlack or 0)),
					}
				end
				break
			end
		end
		for a, s in pairs(probe.attrSlack or {}) do
			if (s.slack or 0) < 0 and not attr_deficits[a] then
				attr_deficits[a] = { required = s.required, afterRemoval = s.current, deficit = s.slack, minNeeded = -s.slack }
			end
		end
		for elem, rs in pairs(probe.resistSlack or {}) do
			if (rs.overcapSlack or 0) < 0 and not res_deficits[elem] then
				res_deficits[elem] = { beforeSlack = rs.overcapSlack, afterSlack = rs.overcapSlack, lost = 0, minNeeded = -rs.overcapSlack }
			end
		end
	end
	return {
		slot = slot_name,
		equippedItem = item and (item.name or item.title) or nil,
		isLoadBearing = (bricked_cnt > 0) or (next(attr_deficits) ~= nil) or (next(res_deficits) ~= nil),
		loadBearingAttrs = lb_attrs,
		attrDeficits = attr_deficits,
		resistDeficits = res_deficits,
		brickedCount = bricked_cnt,
		bricked = bricked,
	}
end

local function postprocess_trade_query(tqg, captured_query, params)
	local info = get_stat_info(tqg)
	local labels, unique_only = info.labels, info.unique_only
	local rarity = tostring(params.rarity or "nonunique"):lower()
	local is_nonunique = (rarity ~= "unique")
	local max_filters = math.max(1, math.min(32, tonumber(params.maxFilters or params.max_filters) or 22))
	if not captured_query then
		return "", 0, 65, 0, {}, json_obj({})
	end
	local tf = captured_query.filters and captured_query.filters.type_filters and captured_query.filters.type_filters.filters
	if tf then
		if rarity == "any" then tf.rarity = nil
		elseif rarity ~= "" then tf.rarity = { option = rarity } end
	end

	local impact_map = {}
	for _, mw in ipairs((tqg and tqg.modWeights) or {}) do
		local id = mw.tradeModId
		local imp = mw.meanStatDiff or math.abs(mw.weight or 0)
		if id then
			impact_map[id] = math.max(impact_map[id] or 0, imp)
			local h = id:match("stat_(%d+)")
			if h and PSEUDO_HASH_MAP[h] then
				local pid = PSEUDO_HASH_MAP[h][1]
				impact_map[pid] = math.max(impact_map[pid] or 0, imp)
			end
		end
	end

	local stat_group = captured_query.stats and captured_query.stats[1]
	local raw_filters = (stat_group and stat_group.filters) or {}
	local orig_min = (stat_group and stat_group.value and stat_group.value.min) or 0
	local orig_imp_sum, pool = 0, {}
	for _, f in ipairs(raw_filters) do
		local w = (f.value and f.value.weight) or 0
		local imp = impact_map[f.id] or math.abs(w)
		orig_imp_sum = orig_imp_sum + imp
		if (not is_nonunique) or is_rare_rollable_stat(f.id, labels[f.id], unique_only) then
			pool[#pool + 1] = { entry = f, weight = w, impact = imp }
		end
	end
	table.sort(pool, function(a, b)
		if a.impact == b.impact then return math.abs(a.weight) > math.abs(b.weight) end
		return a.impact > b.impact
	end)
	local kept, kept_imp_sum = {}, 0
	for i = 1, math.min(max_filters, #pool) do
		kept[i] = pool[i]
		kept_imp_sum = kept_imp_sum + pool[i].impact
	end
	if #kept < #raw_filters and #kept > 0 and orig_min > 0 and stat_group and stat_group.value then
		local ratio = (orig_imp_sum > 0 and kept_imp_sum > 0) and (kept_imp_sum / orig_imp_sum) or (#kept / #raw_filters)
		stat_group.value.min = orig_min * math.min(1.0, math.max(0.25, ratio))
	end
	table.sort(kept, function(a, b)
		local aw, bw = math.abs(a.weight), math.abs(b.weight)
		if aw == bw then return a.impact > b.impact end
		return aw > bw
	end)

	local final_filters, stat_labels = {}, json_obj({})
	for i, k in ipairs(kept) do
		final_filters[i] = k.entry
		stat_labels[k.entry.id] = labels[k.entry.id] or k.entry.id
	end
	if stat_group then stat_group.filters = final_filters end
	local extra_filters = 0
	for gi = 2, #(captured_query.stats or {}) do
		local g = captured_query.stats[gi]
		for _, f in ipairs((g and g.filters) or {}) do
			extra_filters = extra_filters + 1
			if f.id then stat_labels[f.id] = labels[f.id] or f.id end
		end
	end

	local has_misc = (captured_query.filters and captured_query.filters.misc_filters) and 3 or 0
	local max_first = math.max(1, math.floor((160 - 62 - has_misc) / 4) - extra_filters)
	while #final_filters > max_first do
		local drop = final_filters[#final_filters]
		final_filters[#final_filters] = nil
		if drop and drop.id then stat_labels[drop.id] = nil end
	end
	local top_filters = {}
	for i = 1, math.min(15, #final_filters) do
		local fid = kept[i].entry.id
		top_filters[i] = { id = fid, label = stat_labels[fid], weight = kept[i].weight }
	end
	local complexity = 54 + 4 + 4 + has_misc + 4 * (#final_filters + extra_filters)
	local min_weight = (stat_group and stat_group.value and stat_group.value.min) or 0
	return dkjson.encode(captured_query), #final_filters, complexity, min_weight, top_filters, stat_labels
end

local function handle_trade_generate_query(params)
	params = type(params) == "table" and params or {}
	local build = get_build()
	local slot_name = params.slotName or params.slot_name
	if type(slot_name) ~= "string" or slot_name == "" then
		error("trade_generate_query requires slotName", 0)
	end
	local slot = build.itemsTab and build.itemsTab.slots and build.itemsTab.slots[slot_name]
	if not slot and slot_name == "Jewel 1" and build.itemsTab and build.itemsTab.sockets then
		for _, jslot in pairs(build.itemsTab.sockets) do
			if not jslot.inactive then slot = jslot; slot_name = jslot.slotName or slot_name; break end
		end
	end
	if not slot then
		error("Slot not found: " .. tostring(slot_name), 0)
	end

	local t0 = now_ms()
	local query_tab_stub = { itemsTab = build.itemsTab, pbLeague = params.league or "Standard" }
	local tqg
	if params.reuseModData ~= false and M._cached_tqg then
		tqg = M._cached_tqg
		tqg.queryTab = query_tab_stub
		tqg.itemsTab = build.itemsTab
	else
		tqg = new("TradeQueryGenerator"):TradeQueryGenerator(query_tab_stub)
		M._cached_tqg = tqg
	end
	tqg.tradeTypeIndex = 1
	get_stat_info(tqg)

	local power_stat = pick_power_stat(build, params.stat, false)
	local stat_weights = {
		{ stat = power_stat.stat, label = power_stat.label, weightMult = 1.0, transform = power_stat.transform },
	}
	if params.includeEhp ~= false then
		local ehp_stat = find_power_stat_exact("TotalEHP")
		if ehp_stat then
			stat_weights[#stat_weights + 1] = {
				stat = ehp_stat.stat, label = ehp_stat.label, weightMult = tonumber(params.ehpWeight) or 0.5,
			}
		end
	end

	local captured_query, captured_err = nil, nil
	tqg.requesterContext = { slotName = slot_name }
	tqg.requesterCallback = function(_ctx, query_table, err_msg)
		captured_query, captured_err = query_table, err_msg
	end

	local options = {
		includeCorrupted = params.includeCorrupted == true,
		includeMirrored = false,
		includeTalisman = false,
		includeScourge = false,
		includeEldritch = "Remove",
		jewelType = params.jewelType or "Base",
		statWeights = stat_weights,
	}
	local is_nonunique = tostring(params.rarity or "nonunique"):lower() ~= "unique"
	local saved_implicit = tqg.modData and tqg.modData.Implicit
	if is_nonunique and tqg.modData then tqg.modData.Implicit = {} end
	local ok_q, q_err = pcall(function()
		tqg:StartQuery(slot, options)
		local frames = 0
		while tqg.calcContext and tqg.calcContext.co do
			frames = frames + 1
			tqg:OnFrame()
			if frames >= 20000 then
				tqg.calcContext = nil
				error("TradeQueryGenerator exceeded frame guard", 0)
			end
		end
	end)
	if tqg.modData and saved_implicit ~= nil then tqg.modData.Implicit = saved_implicit end
	if not ok_q then error(q_err, 0) end
	local elapsed_ms = now_ms() - t0

	local query_json, filter_count, complexity, min_weight, top_filters, stat_labels =
		postprocess_trade_query(tqg, captured_query, params)
	local slot_constraints = build_slot_constraints(build, slot_name)

	return {
		slotName = slot_name,
		stat = power_stat.stat,
		queryJson = query_json,
		filters = filter_count,
		complexity = complexity,
		complexityWithinLimit = complexity <= 160,
		minWeight = min_weight,
		statLabels = stat_labels,
		topFilters = top_filters,
		slotConstraints = slot_constraints,
		error = captured_err,
		elapsedMs = elapsed_ms,
	}
end

M.rpc_handlers = {
	node_power = handle_node_power,
	power_diff_attribution = handle_power_diff_attribution,
	trade_generate_query = handle_trade_generate_query,
}

return M
