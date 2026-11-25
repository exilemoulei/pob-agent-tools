-- Pure-Lua real-time display translator for Path of Building + PobTools bridge.lua.
-- Supports zh-CN (Data/poe1/zh-rCN), zh-TW (Data/poe1/zh-rTW), and en (pass-through).
local H = require("pob_host")
local M = {}

local LANG_ALIASES = {
	["zh"] = "zh-CN", ["zh-cn"] = "zh-CN", ["zh_cn"] = "zh-CN",
	["zh-rcn"] = "zh-CN", ["zh_rcn"] = "zh-CN", ["cn"] = "zh-CN",
	["chs"] = "zh-CN", ["simplified"] = "zh-CN",
	["zh-tw"] = "zh-TW", ["zh_tw"] = "zh-TW", ["zh-rtw"] = "zh-TW",
	["zh_rtw"] = "zh-TW", ["zh-hk"] = "zh-TW", ["zh_hk"] = "zh-TW",
	["tw"] = "zh-TW", ["cht"] = "zh-TW", ["traditional"] = "zh-TW",
	["en"] = "en", ["en-us"] = "en", ["en_us"] = "en",
	["en-gb"] = "en", ["en_gb"] = "en", ["english"] = "en",
}

local LANG_SUBDIR = {
	["zh-CN"] = "zh-rCN",
	["zh-TW"] = "zh-rTW",
}

-- zh-rCN ships no ascendancy_by_id.json, and its passives.json maps "Assassin" to 暗影 (the
-- Shadow class); ids not listed here fall back to the passives.json name.
local CN_ASC_BY_ID = { Assassin = "刺客" }
local UTF8_CHAR = "[\192-\255][\128-\191]*"

local SELF_DIR = (function()
	local src = debug and debug.getinfo and debug.getinfo(1, "S").source or ""
	local dir = type(src) == "string" and src:sub(1, 1) == "@" and src:sub(2):gsub("\\", "/"):match("^(.*)/[^/]*$")
	return dir or "."
end)()

local engine_root = "."
local active_lang = "zh-CN"
local translate_enabled = true
local draw_override = nil
local dict_cache = {}
local missing_subdirs = {}

local function trim(s)
	return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

function M.normalize_lang(val)
	if type(val) ~= "string" then return nil end
	local k = trim(val):lower()
	return LANG_ALIASES[k]
end

local zj = require("zh_json")
local read_file_bytes, load_entries_into, load_id_table = zj.read_file_bytes, zj.load_entries_into, zj.load_id_table

local han_fold = nil -- false once a lookup failed

local function utf8_chars(str)
	local out, n = {}, 0
	for ch in str:gmatch("[%z\1-\127\192-\255][\128-\191]*") do n = n + 1; out[n] = ch end
	return out
end

local function py_tuple_text(src, name)
	local body = src:match(name .. "%s*=%s*(%b())")
	if not body then return nil end
	local parts = {}
	for piece in body:gmatch('"([^"]*)"') do parts[#parts + 1] = piece end
	return table.concat(parts)
end

-- Traditional -> Simplified map read from the Python package's game_data tables (the same fold
-- `fold_han` uses). Frozen builds ship no .py sources, so this returns nil there.
local function get_han_fold()
	if han_fold ~= nil then return han_fold or nil end
	han_fold = false
	local han_src = read_file_bytes(SELF_DIR .. "/../game_data/tables_han.py")
	local trad = han_src and py_tuple_text(han_src, "_TRAD")
	local simp = han_src and py_tuple_text(han_src, "_SIMP")
	if not trad or not simp then return nil end
	local tc, sc = utf8_chars(trad), utf8_chars(simp)
	if #tc ~= #sc or #tc == 0 then return nil end
	local map = {}
	for i = 1, #tc do map[tc[i]] = sc[i] end
	local tw_src = read_file_bytes(SELF_DIR .. "/../game_data/tables_tw.py")
	local extra = tw_src and tw_src:match("_TRAD_TO_SIMP_MAP%s*=%s*str%.maketrans%(%s*(%b{})")
	for k, v in (extra or ""):gmatch('"([^"]+)"%s*:%s*"([^"]+)"') do map[k] = v end
	han_fold = map
	return map
end

-- zh-rCN lacks affixes, title_words, gem_names_by_id and ggpk_display, so magic and rare item
-- names stayed English. Borrow zh-rTW's tables, folded to Simplified, for keys zh-rCN lacks.
local function add_tw_fallback(d)
	local fold = get_han_fold()
	if not fold then return false end
	local tw = engine_root .. "/Data/poe1/zh-rTW"
	local function fill(dst, src, skip)
		for k, v in pairs(src) do
			if dst[k] == nil and not (skip and skip[k]) then dst[k] = (v:gsub(UTF8_CHAR, fold)) end
		end
	end
	local function read(fn)
		local t = {}
		load_entries_into(tw, fn, t, nil)
		return t
	end
	fill(d.affixes, read("affixes.json"), d.tags)
	fill(d.tags, read("tags.json"))
	fill(d.title_words, read("title_words.json"))
	fill(d.merged, read("ggpk_display.json"))
	local ge, gt = load_id_table(tw, "gem_names_by_id.json")
	if ge then
		d.fb_gem_by_id, d.fb_gem_tpl_by_id = {}, gt
		fill(d.fb_gem_by_id, ge)
	end
	return true
end

local function get_active_dict()
	local subdir = LANG_SUBDIR[active_lang]
	if not subdir then return nil end
	local cached = dict_cache[subdir]
	if cached then return cached end
	if missing_subdirs[subdir] then return nil end

	local dir = engine_root .. "/Data/poe1/" .. subdir
	local d = {
		loaded = false,
		subdir = subdir,
		merged = {},
		items = {},
		uniques = {},
		gems = {},
		passives = {},
		tags = {},
		affixes = {},
		title_words = {},
		gem_by_id = nil,
		gem_tpl_by_id = nil,
		asc_by_id = nil,
		asc_tpl_by_id = nil,
	}

	local any = false
	any = load_entries_into(dir, "ggpk_display.json", nil, d.merged) or any
	any = load_entries_into(dir, "tags.json", d.tags, d.merged) or any
	any = load_entries_into(dir, "items.json", d.items, d.merged) or any
	any = load_entries_into(dir, "gems.json", d.gems, d.merged) or any
	any = load_entries_into(dir, "gem_names.json", d.gems, d.merged) or any
	any = load_entries_into(dir, "gem_names_support.json", d.gems, d.merged) or any
	any = load_entries_into(dir, "gem_names_nonactive.json", d.gems, d.merged) or any
	any = load_entries_into(dir, "ui.json", nil, d.merged) or any
	any = load_entries_into(dir, "pob_manual.json", nil, d.merged) or any
	any = load_entries_into(dir, "passives.json", d.passives, d.merged) or any
	any = load_entries_into(dir, "uniques.json", d.uniques, d.merged) or any
	any = load_entries_into(dir, "monsters.json", nil, d.merged) or any
	load_entries_into(dir, "affixes.json", d.affixes, nil)
	load_entries_into(dir, "title_words.json", d.title_words, nil)
	d.gem_by_id, d.gem_tpl_by_id = load_id_table(dir, "gem_names_by_id.json")
	d.asc_by_id, d.asc_tpl_by_id = load_id_table(dir, "ascendancy_by_id.json")

	if not any then
		missing_subdirs[subdir] = true
		return nil
	end
	local ui_fb = subdir == "zh-rTW" and {
		["Recovery"] = "恢復", ["Reserved"] = "保留", ["Unreserved"] = "未保留", ["Total"] = "總計",
		["Total Base"] = "基礎總計", ["Total Increased"] = "總增加", ["Total More"] = "總更多",
		["Effective Hit Pool"] = "有效生命池", ["Average Hit"] = "平均擊中", ["Hit Chance"] = "命中率",
	} or {
		["Recovery"] = "回复", ["Reserved"] = "保留", ["Unreserved"] = "未保留", ["Total"] = "总计",
		["Total Base"] = "基础总计", ["Total Increased"] = "总提高", ["Total More"] = "总更多",
		["Effective Hit Pool"] = "有效生命池", ["Average Hit"] = "平均击中", ["Hit Chance"] = "命中率",
	}
	for uk, uv in pairs(ui_fb) do
		if d.merged[uk] == nil then d.merged[uk] = uv end
	end
	if subdir == "zh-rCN" then
		if not d.asc_by_id then
			d.asc_by_id, d.asc_tpl_by_id = {}, {}
			for id, zh in pairs(CN_ASC_BY_ID) do d.asc_by_id[id] = zh; d.asc_tpl_by_id[id] = id end
		end
		d.twFallback = add_tw_fallback(d)
	end
	d.loaded = true
	dict_cache[subdir] = d
	return d
end

local function split_lead_color(text)
	local i = 1
	while true do
		local e = text:match("^%^x%x%x%x%x%x%x()", i) or text:match("^%^%d()", i)
		if not e then break end
		i = e
	end
	return text:sub(1, i - 1), text:sub(i)
end

local function foulborn_prefix_zh()
	return active_lang == "zh-TW" and "穢生 " or "秽生 "
end

local function translate_foulborn(d, s)
	local inner = s:match("^[Ff]oulborn (.+)$")
	if not inner then return nil end
	local u = d.uniques[inner] or d.merged[inner]
	if u and u ~= inner then return foulborn_prefix_zh() .. u end
	return nil
end

local function translate_piece(d, piece, is_base_hint)
	if draw_override and draw_override[piece] then return draw_override[piece] end
	if is_base_hint then
		local clean_b = piece:gsub(" %(.+%)$", "")
		local bh = d.items[clean_b] or d.items[piece] or d.merged[clean_b]
		if bh then return bh end
	end
	local exact = d.uniques[piece] or d.items[piece] or d.gems[piece] or d.merged[piece]
	if exact then return exact end
	local fb = translate_foulborn(d, piece)
	if fb then return fb end
	if piece:find(" Support$", 1) then
		local short = piece:gsub(" Support$", "")
		if d.gems[short] then return d.gems[short] end
	end
	return nil
end

local function translate_display(s)
	if type(s) ~= "string" or s == "" or not translate_enabled or active_lang == "en" then
		return s
	end
	local d = get_active_dict()
	if not d or not d.loaded then return s end

	if draw_override and draw_override[s] then return draw_override[s] end
	if d.merged[s] then return d.merged[s] end

	local lead, rest = split_lead_color(s)
	if rest == "" then return s end
	if draw_override and draw_override[rest] then return lead .. draw_override[rest] end
	if d.merged[rest] then return lead .. d.merged[rest] end

	local lws, core, rws = rest:match("^(%s*)(.-)(%s*)$")
	if not core or core == "" then return s end
	if d.merged[core] then return lead .. lws .. d.merged[core] .. rws end

	local fb = translate_foulborn(d, core)
	if fb then return lead .. lws .. fb .. rws end

	if core:sub(-1) == ":" then
		local stem = core:sub(1, -2)
		if d.merged[stem] then return lead .. lws .. d.merged[stem] .. ":" .. rws end
	end

	if core:find(", ", 1, true) then
		local parts, count = {}, 0
		local start = 1
		while true do
			local si, ei = core:find(", ", start, true)
			if not si then
				count = count + 1; parts[count] = core:sub(start); break
			end
			count = count + 1; parts[count] = core:sub(start, si - 1)
			start = ei + 1
		end
		local changed = false
		for i = 1, count do
			local tr_p = translate_piece(d, parts[i], count == 2 and i == 2)
			if tr_p and tr_p ~= parts[i] then
				parts[i] = tr_p
				changed = true
			end
		end
		if changed then return lead .. lws .. table.concat(parts, ", ") .. rws end
	end

	return s
end

local function gem_by_id(by_id_tbl, tpl_tbl, id, name)
	if id == nil or not by_id_tbl then return nil end
	local sid = tostring(id)
	local by_id = by_id_tbl[sid]
	if not by_id or by_id == "" then return nil end
	local tpl = tpl_tbl and tpl_tbl[sid]
	if not tpl or tpl == name or tpl == (name:gsub(" Support$", "")) then return by_id end
	return nil
end

local function gem_name_zh(id, name)
	if not translate_enabled or active_lang == "en" or type(name) ~= "string" or name == "" then
		return nil
	end
	local d = get_active_dict()
	if not d or not d.loaded then return nil end
	local own = gem_by_id(d.gem_by_id, d.gem_tpl_by_id, id, name)
	if own then return own end
	local hit = d.gems[name] or d.gems[name .. " Support"] or d.gems[(name:gsub(" Support$", ""))]
	if hit and hit ~= name then return hit end
	return gem_by_id(d.fb_gem_by_id, d.fb_gem_tpl_by_id, id, name)
end

local function ascendancy_name_zh(id, name)
	if not translate_enabled or active_lang == "en" or type(name) ~= "string" or name == "" then
		return nil
	end
	local d = get_active_dict()
	if not d or not d.loaded then return nil end
	if id ~= nil and d.asc_by_id then
		local sid = tostring(id)
		local by_id = d.asc_by_id[sid]
		if by_id and by_id ~= "" then
			local tpl = d.asc_tpl_by_id and d.asc_tpl_by_id[sid]
			if not tpl or tpl == name or sid == name then return by_id end
		end
	end
	local hit = d.passives[name] or d.merged[name]
	if hit and hit ~= name then return hit end
	return nil
end

local function ascendancy_id_of(tree, name)
	local classes = type(tree) == "table" and tree.classes or nil
	if type(classes) ~= "table" or type(name) ~= "string" then return nil end
	for _, cls in pairs(classes) do
		local ascs = type(cls) == "table" and cls.classes or nil
		if type(ascs) == "table" then
			for i, a in pairs(ascs) do
				if i ~= 0 and type(a) == "table" and a.name == name and a.id ~= nil then
					return a.id
				end
			end
		end
	end
	return nil
end

local function translate_title_words(d, title)
	local w1, w2 = title:match("^(%S+)%s+(%S+)$")
	if not w1 or not w2 then return nil end
	local z1 = d.title_words[w1] or d.tags[w1] or d.merged[w1]
	local z2 = d.title_words[w2] or d.tags[w2] or d.merged[w2]
	if z1 and z2 and (z1 ~= w1 or z2 ~= w2) then return z1 .. z2 end
	return nil
end

local function item_name_zh(it)
	if not translate_enabled or active_lang == "en" or type(it) ~= "table" then return nil end
	local d = get_active_dict()
	if not d or not d.loaded then return nil end

	local base_raw = type(it.baseName) == "string" and it.baseName or nil
	local base = base_raw and base_raw:gsub(" %(.+%)$", "") or nil

	if type(it.title) == "string" and it.title ~= "" and base then
		local t_zh = d.uniques[it.title]
			or translate_foulborn(d, it.title)
			or translate_title_words(d, it.title)
			or d.merged[it.title]
			or it.title
		local b_zh = d.items[base] or d.items[base_raw] or d.merged[base] or base
		if t_zh ~= it.title or b_zh ~= base then
			return t_zh .. ", " .. b_zh
		end
		return nil
	end

	if it.title == nil and base then
		local b_zh = d.items[base] or d.items[base_raw] or d.merged[base]
		if not b_zh then return nil end
		local raw_name = type(it.name) == "string" and it.name or base
		local lead, nm = split_lead_color(raw_name)
		if nm == base or nm == base_raw then return lead .. b_zh end
		local si, ei = nm:find(base, 1, true)
		if si then
			local pre_raw, suf_raw = nm:sub(1, si - 1), nm:sub(ei + 1)
			local pre, suf = trim(pre_raw), trim(suf_raw)
			local zp = (pre == "") and "" or (d.affixes[pre] or d.tags[pre] or d.merged[pre])
			local zs = (suf == "") and "" or (d.affixes[suf] or d.tags[suf] or d.merged[suf])
			if zp and zs then
				local pre_gap = (pre ~= "") and (pre_raw:match("(%s*)$") or " ") or ""
				local suf_gap = (suf ~= "") and (suf_raw:match("^(%s*)") or " ") or ""
				return lead .. zp .. pre_gap .. b_zh .. suf_gap .. zs
			end
		end
	end
	return nil
end

local function socket_group_name_map(groups)
	if not translate_enabled or active_lang == "en" then return nil end
	local map, bad = {}, {}
	local function add(en, zh)
		if type(en) ~= "string" or not zh or bad[en] then return end
		if map[en] == nil then map[en] = zh
		elseif map[en] ~= zh then map[en] = nil; bad[en] = true end
	end
	for _, g in ipairs(type(groups) == "table" and groups or {}) do
		for _, gem in ipairs(type(g) == "table" and type(g.gemList) == "table" and g.gemList or {}) do
			local gd = type(gem) == "table" and gem.gemData or nil
			local ge = type(gem) == "table" and (gem.grantedEffect or (type(gd) == "table" and gd.grantedEffect)) or nil
			if type(gd) == "table" and type(gd.name) == "string" then
				local id = gd.grantedEffectId or (type(ge) == "table" and ge.id) or nil
				add(gd.name, gem_name_zh(id, gd.name))
			end
			if type(ge) == "table" and ge.id ~= nil and type(ge.name) == "string" then
				add(ge.name, gem_name_zh(ge.id, ge.name))
			end
		end
	end
	return map
end

-- bridge.lua caches tree_data per tree version and ":en"/":zh", so zh-CN and zh-TW would share
-- one copy. The host answers tree_data and asks the bridge under a version alias per language
-- ("3_30@zh-TW", held in main.tree for the call only), so each language gets its own copy. Its
-- output names the real version; translation off (":en" in the bridge) needs no alias.
local function tree_data(params, dispatch)
	params = type(params) == "table" and params or {}
	if not translate_enabled or active_lang == "en" then return dispatch("tree_data", params) end
	local v = params.version
	if type(v) ~= "string" or v == "" then
		local b = H.get_build({ rebuild = false, optional = true })
		v = b and b.spec and b.spec.treeVersion or latestTreeVersion
	end
	local m = type(launch) == "table" and launch.main or nil
	local tree = type(v) == "string" and m and type(m.tree) == "table" and m:LoadTree(v)
	if not tree then return dispatch("tree_data", params) end
	local alias, aliased = v .. "@" .. active_lang, {}
	for k, val in pairs(params) do aliased[k] = val end
	aliased.version = alias
	m.tree[alias] = tree
	local ok, out = pcall(dispatch, "tree_data", aliased)
	m.tree[alias] = nil
	-- A bridge that refuses the alias still answers, with its shared copy.
	if not ok or type(out) ~= "table" then return dispatch("tree_data", params) end
	out.treeVersion = v
	return out
end

function M.set_locale(lang_or_params)
	local raw = type(lang_or_params) == "table" and lang_or_params.lang or lang_or_params
	local norm = M.normalize_lang(raw)
	if not norm then
		error("unsupported locale: " .. tostring(raw) .. " (expected zh-CN, zh-TW, or en)", 0)
	end
	active_lang = norm
	if norm == "en" then
		translate_enabled = false
		return { ok = true, lang = active_lang, translate = false, dictLoaded = false }
	end
	translate_enabled = true
	local subdir = LANG_SUBDIR[norm]
	if subdir then missing_subdirs[subdir] = nil end
	local d = get_active_dict()
	return {
		ok = true,
		lang = active_lang,
		translate = translate_enabled,
		dictLoaded = (d ~= nil and d.loaded == true) or false,
		twFallback = (d ~= nil and d.twFallback == true) or nil,
	}
end

function M.get_locale()
	local subdir = LANG_SUBDIR[active_lang]
	local d = subdir and dict_cache[subdir] or nil
	return {
		ok = true,
		lang = active_lang,
		translate = translate_enabled and true or false,
		dictLoaded = (active_lang ~= "en" and d ~= nil and d.loaded == true) or false,
	}
end

function M.install_pre_bridge(root_path, opts)
	engine_root = H.norm_path(root_path or os.getenv("POB_ENGINE_ROOT") or ".")
	dict_cache = {}
	missing_subdirs = {}
	draw_override = nil

	local init_raw = (type(opts) == "table" and opts.lang) or os.getenv("POB_LANG") or "zh-CN"
	active_lang = M.normalize_lang(init_raw) or "zh-CN"
	translate_enabled = (active_lang ~= "en")

	function _G.PobToolsSetTranslate(on)
		translate_enabled = (on ~= false and on ~= nil)
	end
	function _G.PobToolsGetTranslate()
		return translate_enabled and true or false
	end
	function _G.PobToolsSetDrawOverride(map)
		draw_override = type(map) == "table" and map or nil
	end
	_G.PobToolsTranslateDisplay = translate_display
	_G.PobToolsTranslate = translate_display
	_G.PobToolsGemNameZh = gem_name_zh
	_G.PobToolsAscendancyNameZh = ascendancy_name_zh
	_G.PobToolsAscendancyIdOf = ascendancy_id_of
	_G.PobToolsItemNameZh = item_name_zh
	_G.PobToolsSocketGroupNameMap = socket_group_name_map
	return true
end

M.rpc_handlers = {
	set_locale = function(p) return M.set_locale(type(p) == "table" and p.lang or p) end,
	get_locale = function(_p) return M.get_locale() end,
	tree_data = tree_data,
}

return M
