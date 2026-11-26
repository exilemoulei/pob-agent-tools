-- Zero-intrusion upstream PoB kernel RPCs:
--   - M.rpc_handlers.node_power: fast PowerBuilder + BuildPowerReportList (includes Cluster notables),
--     skipping the redundant FullDPS pass when stat ~= "FullDPS"
--   - M.rpc_handlers.power_diff_attribution: CompareTab:ComparePowerBuilder attribution (non-compare_* RPC)
--   (Trade kernels live in trade_kernel.lua.)

local H = require("pob_host")
local npk = require("node_power_kernel")
local now_ms, get_build = H.now_ms, H.get_build
local pick_power_stat = npk.pick_power_stat
local handle_node_power = npk.handle_node_power

local M = {}

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

M.rpc_handlers = {
	node_power = handle_node_power,
	power_diff_attribution = handle_power_diff_attribution,
}

return M
