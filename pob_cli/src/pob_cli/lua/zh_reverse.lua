-- Item text copied from the Chinese game client -> English item text for PoB's parser.
-- Installs _G.PobToolsReverseText, which bridge.lua's normalize_item_text calls for add_item,
-- parse_item_text, item_tooltip and item_edit_begin. pob-zh.exe used to provide it natively;
-- without it Chinese text reached PoB untranslated and its base and mods were not recognised.
-- Built lazily, on the first text with non-ASCII bytes, from Data/poe1/zh-rTW and zh-rCN (read
-- through zh_json.lua, the dictionary reader zh_translate.lua shares):
-- stat templates (both locales), base and unique names, and zh-rTW's item_metadata.json headers.
-- Where one Chinese line has several English readings, the one modLib.parseMod accepts wins.

local zj = require("zh_json")

local M = {}
local root = "."
local R = nil -- false once loading failed, the tables once loaded

-- zh-rCN ships no item_metadata.json; the Simplified client's own words, "zh=en;..."
local CN_HEADERS = "物品类别=Item Class;稀有度=Rarity;品质=Quality;闪避值=Evasion Rating;能量护盾=Energy Shield;"
	.. "护甲=Armour;护甲值=Armour;需求=Requirements;等级=Level;力量=Str;敏捷=Dex;智慧=Int;智力=Int;"
	.. "插槽=Sockets;物品等级=Item Level;堆叠数量=Stack Size;物理伤害=Physical Damage;元素伤害=Elemental Damage;"
	.. "混沌伤害=Chaos Damage;暴击率=Critical Strike Chance;攻击暴击率=Critical Strike Chance;"
	.. "暴击几率=Critical Strike Chance;每秒攻击次数=Attacks per Second;武器范围=Weapon Range;"
	.. "格挡几率=Chance to Block;攻击格挡率=Chance to Block;范围=Radius;仅限=Limited to"
local CN_RARITY = "稀有=Rare;魔法=Magic;传奇=Unique;普通=Normal"
local CN_STATUS = "已腐化=Corrupted;已污染=Corrupted;已复制=Mirrored;未鉴定=Unidentified;已分裂=Split;"
	.. "忆境物品=Synthesised Item;分裂物品=Fractured Item;塑界者物品=Shaper Item;裂界者物品=Elder Item;"
	.. "圣战军王物品=Crusader Item;圣战者物品=Crusader Item;救赎者物品=Redeemer Item;狩猎者物品=Hunter Item;"
	.. "总督军物品=Warlord Item;督军物品=Warlord Item;灼烙总督物品=Searing Exarch Item;吞噬天地物品=Eater of Worlds Item"
local CN_RADIUS = "小=Small;中=Medium;大=Large;非常大=Very Large;巨大=Massive;可变=Variable"

local PH = "{(%d*)[^}]*}"
local NUM = "([%+%-]?%d+%.?%d*%%?)"

local function nows(s) return (s:gsub("%s+", "")) end
local function trim(s) return (s:gsub("^%s+", ""):gsub("%s+$", "")) end
local function has_cjk(s) return s:find("[\128-\255]") ~= nil end
local function skeleton(s) return (s:gsub(PH, ""):gsub("#", ""):gsub("[%d%.%+%-%%%s]", "")) end

local function each_pair(spec, fn)
	for zh, en in spec:gmatch("([^=;]+)=([^;]+)") do fn(zh, en) end
end

-- Only the small structured files (item_metadata.json, synonyms.json) need a full JSON decode;
-- the string maps come from zh_json's shared reader.
local function read_json(path)
	local raw = zj.read_file_bytes(path)
	if not raw then return nil end
	local ok, dk = pcall(require, "dkjson")
	if not ok or type(dk) ~= "table" then return nil end
	local obj = dk.decode(raw)
	return type(obj) == "table" and obj or nil
end

local function push(tbl, key, val, cap)
	local list = tbl[key]
	if not list then list = {}; tbl[key] = list end
	if #list < cap then
		for _, v in ipairs(list) do if v == val then return end end
		list[#list + 1] = val
	end
end

local function add_stat(r, zh, en)
	if type(zh) ~= "string" or type(en) ~= "string" or zh == "" or en == "" or zh:find("\n", 1, true) then return end
	if zh:find(PH) or zh:find("#", 1, true) then
		local sk = skeleton(zh)
		if sk ~= "" then push(r.tpl, sk, { zh = zh, en = en }, 40) end
	elseif has_cjk(zh) then
		push(r.exact, nows(zh), en, 6)
	end
end

local function add_name(map, zh, en)
	if type(zh) == "string" and type(en) == "string" and zh ~= "" and has_cjk(zh) then
		local k = nows(zh)
		if map[k] == nil then map[k] = en end
	end
end

local function load_meta(r, dir)
	local meta = read_json(dir .. "/item_metadata.json")
	if not meta then return end
	local function pairs_of(list, target)
		for _, p in ipairs(type(list) == "table" and list or {}) do
			if type(p) == "table" and type(p.zh) == "string" and type(p.en) == "string" then target[nows(p.zh)] = p.en end
		end
	end
	pairs_of(meta.headers, r.headers)
	pairs_of(meta.rarity_values, r.rarity)
	pairs_of(meta.item_classes, r.classes)
	pairs_of(meta.status_lines, r.status)
	pairs_of(meta.influence_tags, r.status)
	pairs_of(meta.header_values and meta.header_values.Radius, r.radius)
	for _, p in ipairs(type(meta.composite_prefixes) == "table" and meta.composite_prefixes or {}) do
		if type(p) == "table" and type(p.zh) == "string" and type(p.en) == "string" then
			r.prefixes[#r.prefixes + 1] = { zh = p.zh, en = p.en }
		end
	end
end

local function load_tables()
	if R ~= nil then return R or nil end
	R = false
	local r = {
		exact = {}, tpl = {}, uniques = {}, bases = {}, base_list = {}, headers = {}, rarity = {},
		classes = {}, status = {}, radius = {}, prefixes = {}, syn = {},
	}
	local any = false
	for _, sub in ipairs({ "zh-rTW", "zh-rCN" }) do
		local dir = root .. "/Data/poe1/" .. sub
		-- ggpk_reverse.json first: it names the English line PoB parses when a Chinese line is shared
		for _, f in ipairs({ "ggpk_reverse.json", "stats.json", "ggpk_full.json", "stat_wrappers.json" }) do
			local e = zj.entries(dir, f)
			if e then
				any = true
				for en, zh in pairs(e) do add_stat(r, zh, en) end
			end
		end
		for en, zh in pairs(zj.entries(dir, "items.json") or {}) do add_name(r.bases, zh, en) end
		for en, zh in pairs(zj.entries(dir, "uniques.json") or {}) do add_name(r.uniques, zh, en) end
		load_meta(r, dir)
		local syn = read_json(dir .. "/synonyms.json")
		for _, p in ipairs(syn and type(syn.replacements) == "table" and syn.replacements or {}) do
			if type(p) == "table" and type(p.from) == "string" and type(p.to) == "string" and p.from ~= "" then
				r.syn[#r.syn + 1] = { p.from, p.to }
			end
		end
	end
	if not any then return nil end
	each_pair(CN_HEADERS, function(zh, en) if r.headers[zh] == nil then r.headers[zh] = en end end)
	each_pair(CN_RARITY, function(zh, en) if r.rarity[zh] == nil then r.rarity[zh] = en end end)
	each_pair(CN_STATUS, function(zh, en) if r.status[zh] == nil then r.status[zh] = en end end)
	each_pair(CN_RADIUS, function(zh, en) if r.radius[zh] == nil then r.radius[zh] = en end end)
	for zh, en in pairs(r.bases) do r.base_list[#r.base_list + 1] = { zh, en } end
	-- Longest first, so a magic item's name finds "Two-Stone Ring", not "Ring"
	table.sort(r.base_list, function(a, b) return #a[1] > #b[1] end)
	R = r
	return r
end

local function parses(line)
	if type(modLib) ~= "table" or type(modLib.parseMod) ~= "function" then return nil end
	local ok, list, extra = pcall(modLib.parseMod, line)
	return ok and list ~= nil and extra == nil
end

local function pick(cands)
	if #cands == 0 then return nil end
	for _, c in ipairs(cands) do
		if parses(c) then return c end
	end
	return cands[1]
end

local function lit_pat(t)
	return (t:gsub("[%^%$%(%)%%%.%[%]%*%+%-%?]", "%%%0"):gsub("%s+", "%%s*"))
end

local function compile(zh)
	local out, keys, pos, seq = { "^%s*" }, {}, 1, 0
	while true do
		local s, e, inner = zh:find(PH, pos)
		local h = zh:find("#", pos, true)
		if h and (not s or h < s) then s, e, inner = h, h, "" end
		if not s then break end
		out[#out + 1] = lit_pat(zh:sub(pos, s - 1))
		out[#out + 1] = NUM
		keys[#keys + 1] = inner ~= "" and tonumber(inner) or seq
		seq = seq + 1
		pos = e + 1
	end
	out[#out + 1] = lit_pat(zh:sub(pos)) .. "%s*$"
	return table.concat(out), keys
end

local function render(en, keys, caps)
	local vals = {}
	for i, k in ipairs(keys) do vals[k] = caps[i] end
	local seq = -1
	local function next_val(d)
		seq = seq + 1
		return vals[(d ~= nil and d ~= "") and tonumber(d) or seq]
	end
	local out = en:gsub(PH, next_val):gsub("#", function() return next_val(nil) end)
	if out:find("[{#]") or has_cjk(out) then return nil end
	-- Wiki-style "[Curse|Curses]" markup in the English templates
	return (out:gsub("%[[^%]|]*|([^%]]*)%]", "%1"):gsub("%[([^%]]*)%]", "%1"))
end

local function match_stat(r, core)
	local cands = {}
	for _, en in ipairs(r.exact[nows(core)] or {}) do cands[#cands + 1] = en end
	for _, t in ipairs(r.tpl[skeleton(core)] or {}) do
		if not t.pat then t.pat, t.keys = compile(t.zh) end
		local caps = { core:match(t.pat) }
		if caps[1] ~= nil then
			local line = render(t.en, t.keys, caps)
			if line then cands[#cands + 1] = line end
		end
	end
	return pick(cands)
end

local function translate_mod(r, text)
	local core, suf = text:match("^(.-)%s*(%([%a ]+%))$")
	if not core then core, suf = text, nil end
	-- Advanced copy shows roll ranges, "+70(65-80)"; PoB wants the plain value
	core = trim(core:gsub("(%d)%([%d%.]+%-[%d%.]+%)", "%1"))
	for _, p in ipairs(r.prefixes) do
		if core:sub(1, #p.zh) == p.zh then
			local inner = translate_mod(r, core:sub(#p.zh + 1))
			if inner then return p.en .. inner .. (suf and " " .. suf or "") end
		end
	end
	local en = match_stat(r, core)
	if not en and #r.syn > 0 then
		local alt = core
		for _, p in ipairs(r.syn) do alt = alt:gsub(p[1]:gsub("%p", "%%%0"), (p[2]:gsub("%%", "%%%%"))) end
		if alt ~= core then en = match_stat(r, alt) end
	end
	if not en then return nil end
	return en .. (suf and " " .. suf or "")
end

local function find_base(r, line)
	local k = nows(line)
	if r.bases[k] then return r.bases[k] end
	for _, b in ipairs(r.base_list) do
		if k:find(b[1], 1, true) then return b[2] end
	end
	return nil
end

-- "Key: value" header lines (ASCII or full-width colon); nil when the key is not a known header
local function translate_header(r, t)
	local key, val = t:gsub("\239\188\154", ":"):match("^([^:]+):%s*(.-)%s*$")
	if not key then return nil end
	local en_key = r.headers[nows(key)]
	if not en_key then return nil end
	if val == "" then return en_key .. ":" end
	local v = val
	if en_key == "Rarity" then
		v = r.rarity[nows(val)] or val
	elseif en_key == "Item Class" then
		v = r.classes[nows(val)]
		-- PoB reads the class only to spot Viscera; an untranslated one is better left out
		if not v then return false end
	elseif en_key == "Radius" then
		v = r.radius[nows(val)] or val
	elseif has_cjk(val) then
		v = translate_mod(r, val) or val
	end
	return en_key .. ": " .. v, en_key, v
end

local function reverse_text(raw)
	if type(raw) ~= "string" or not has_cjk(raw) then return raw end
	local r = load_tables()
	if not r then return raw end
	local out, rarity, names_left = {}, nil, 0
	for line in (raw:gsub("\r", "") .. "\n"):gmatch("([^\n]*)\n") do
		local t, res = trim(line), nil
		if t == "--------" then
			names_left, res = 0, t
		elseif names_left > 0 then
			local k = nows(t)
			if names_left == 2 and (rarity == "Rare" or rarity == "Unique") then
				res = r.uniques[k] or r.bases[k] or t
			else
				res = has_cjk(t) and find_base(r, t) or t
			end
			names_left = names_left - 1
		elseif not has_cjk(t) then
			res = t
			local rv = t:match("^Rarity:%s*(%a+)")
			if rv then rarity, names_left = rv, (rv == "Rare" or rv == "Unique") and 2 or 1 end
		elseif t:sub(1, 1) == "{" then
			res = nil -- advanced copy's "{ Prefix Modifier ... }" headers; the mod lines follow
		else
			local h, en_key, v = translate_header(r, t)
			if h == false then
				res = nil
			elseif h then
				res = h
				if en_key == "Rarity" then rarity, names_left = v, (v == "Rare" or v == "Unique") and 2 or 1 end
			else
				res = r.status[nows(t)] or translate_mod(r, t) or t
			end
		end
		if res ~= nil then out[#out + 1] = res end
	end
	while out[#out] == "" do out[#out] = nil end
	return table.concat(out, "\n")
end

function M.install(engine_root)
	root = (type(engine_root) == "string" and engine_root ~= "") and engine_root:gsub("\\", "/") or "."
	R = nil
	_G.PobToolsReverseText = reverse_text
	return true
end

M.reverse_text = reverse_text
return M
