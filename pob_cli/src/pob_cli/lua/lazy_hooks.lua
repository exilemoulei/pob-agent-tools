-- Zero-intrusion pre-launch lazy loading hooks for Path of Building.
-- Wraps LoadModule / PLoadModule / io.open before Launch.lua without editing upstream PoB:
--   1. Pearl of Tsoatha fast-path avoiding 258k-line stat_descriptions.lua at boot.
--   2. Data/FlavourText -> {} skip.
--   3. Defer Main.lua LoadItems uniqueDB/rareDB/visceraDB coroutine (drainable on demand via ensure_item_dbs).
--   4. Lazy metatable proxies for Data/Crucible and Data/ModScourge.
--   5. Optional .ljbc bytecode loader + on-demand TreeData fallback resolver.

local M = {}

local function strip_trailing_slash(p)
	if not p or p == "" then
		return "."
	end
	local s = p:gsub("\\", "/")
	if s:match("^%a:/$") or s == "/" then
		return s
	end
	return (s:gsub("/+$", ""))
end

local function is_abs_path(p)
	return p:sub(1, 1) == "/" or p:match("^%a:/") ~= nil
end

local function opt_enabled(opts, key, default_val)
	if type(opts) == "table" and opts[key] ~= nil then
		return opts[key] == true
	end
	return default_val
end

M.stats = {
	loaded_modules = 0,
	bytecode_loads = 0,
	hook_flavour_skipped = 0,
	hook_pearl_fastpath_calls = 0,
	hook_stat_desc_real_loads = 0,
	hook_item_dbs_skipped = 0,
	hook_item_dbs_drained = 0,
	hook_crucible_deferred = 0,
	hook_crucible_real_loads = 0,
	hook_scourge_deferred = 0,
	hook_scourge_real_loads = 0,
	tree_fallback_hits = 0,
}

M._saved_load_items = nil
M._item_dbs_drained = false
M._installed = false

function M.ensure_item_dbs()
	if M._item_dbs_drained or not M._saved_load_items then
		return false
	end
	local fn = M._saved_load_items
	M._saved_load_items = nil
	if main and main.onFrameFuncs then
		main.onFrameFuncs["LoadItems"] = fn
		local steps = 0
		while main.onFrameFuncs["LoadItems"] and steps < 50000 do
			main.onFrameFuncs["LoadItems"]()
			steps = steps + 1
		end
	end
	M._item_dbs_drained = true
	M.stats.hook_item_dbs_drained = M.stats.hook_item_dbs_drained + 1
	return true
end

function M.get_stats()
	return M.stats
end

function M.install_pre_launch(game_dir, opts)
	if M._installed then
		return M
	end
	opts = type(opts) == "table" and opts or {}

	local disable_all = os.getenv("POB_DISABLE_LAZY_HOOKS") == "1"
	local skip_flavour = (not disable_all) and opt_enabled(opts, "skip_flavour_text", true)
	local lazy_stat_desc = (not disable_all) and opt_enabled(opts, "lazy_stat_descriptions", true)
	local skip_item_dbs = (not disable_all) and opt_enabled(opts, "skip_item_dbs", true)
	local lazy_legacy_mods = (not disable_all) and opt_enabled(opts, "lazy_legacy_mods", true)
	local bytecode_mode = opt_enabled(opts, "bytecode_mode", os.getenv("POB_BYTECODE_MODE") == "1")

	local gdir = strip_trailing_slash(tostring(game_dir or "."))
	local ondemand_dir = opts.ondemand_tree_dir or os.getenv("POB_ONDEMAND_TREE_DIR")
	if type(ondemand_dir) == "string" and ondemand_dir ~= "" then
		ondemand_dir = strip_trailing_slash(ondemand_dir)
	else
		ondemand_dir = nil
	end
	local tree_fallback_fn = type(opts.tree_fallback_fn) == "function" and opts.tree_fallback_fn or nil

	local raw_io_open = io.open
	local function file_exists(p)
		local f = raw_io_open(p, "rb")
		if f then
			f:close()
			return true
		end
		return false
	end

	local attempted_trees = {}
	local function is_tree_data_script(rel)
		local low = rel:lower()
		return low:match("^treedata/") ~= nil
			and (low:match("%.lua$") ~= nil or low:match("%.json$") ~= nil)
	end

	local function resolve_tree_fallback(rel)
		if not is_tree_data_script(rel) then
			return nil
		end
		-- 1. Already staged in on-demand tree directory
		if ondemand_dir then
			local od = ondemand_dir .. "/" .. rel
			if file_exists(od) then
				M.stats.tree_fallback_hits = M.stats.tree_fallback_hits + 1
				return od
			end
		end
		local tree_ver = rel:match("^TreeData/([%w_%-]+)/")
		if not tree_ver then
			return nil
		end
		-- 2. Custom callback passed via opts.tree_fallback_fn
		if tree_fallback_fn then
			local ok, resolved = pcall(tree_fallback_fn, rel, tree_ver, gdir)
			if ok and type(resolved) == "string" and file_exists(resolved) then
				M.stats.tree_fallback_hits = M.stats.tree_fallback_hits + 1
				return resolved
			end
		end
		-- 3. Native host Python callback (_G.__POB_ENSURE_TREE) fetching SHA1-verified tree files
		if not attempted_trees[tree_ver] and type(_G.__POB_ENSURE_TREE) == "function" then
			attempted_trees[tree_ver] = true
			pcall(_G.__POB_ENSURE_TREE, tree_ver)
			if file_exists(rel) then
				M.stats.tree_fallback_hits = M.stats.tree_fallback_hits + 1
				return rel
			end
			local full = gdir .. "/" .. rel
			if file_exists(full) then
				M.stats.tree_fallback_hits = M.stats.tree_fallback_hits + 1
				return full
			end
		end
		return nil
	end

	local function resolve_module_path(norm)
		local is_abs = is_abs_path(norm)
		if bytecode_mode then
			local bc_rel = norm .. ".ljbc"
			if file_exists(bc_rel) then
				M.stats.bytecode_loads = M.stats.bytecode_loads + 1
				return bc_rel
			end
			if not is_abs then
				local bc_full = gdir .. "/" .. bc_rel
				if file_exists(bc_full) then
					M.stats.bytecode_loads = M.stats.bytecode_loads + 1
					return bc_full
				end
			end
		end

		local lua_rel = norm .. ".lua"
		if file_exists(lua_rel) then
			return lua_rel
		end
		if not is_abs then
			local lua_full = gdir .. "/" .. lua_rel
			if file_exists(lua_full) then
				return lua_full
			end
			local fb = resolve_tree_fallback(lua_rel)
			if fb then
				return fb
			end
		end
		return lua_rel
	end

	-- Intercept read-mode io.open for TreeData fallback and optional .ljbc files
	io.open = function(path, mode)
		local m = mode or "r"
		if type(path) == "string" and not m:find("[wa+]") then
			local norm = path:gsub("\\", "/")
			local rel = norm
			local prefix = gdir .. "/"
			if norm:sub(1, #prefix) == prefix then
				rel = norm:sub(#prefix + 1)
			end
			if is_tree_data_script(rel) and not file_exists(path) then
				local fb = resolve_tree_fallback(rel)
				if fb then
					return raw_io_open(fb, m)
				end
			end
			if bytecode_mode and rel:match("%.lua$") and not file_exists(path) then
				local bc_rel = rel:gsub("%.lua$", ".ljbc")
				if file_exists(bc_rel) then
					return raw_io_open(bc_rel, "rb")
				end
				local bc_full = gdir .. "/" .. bc_rel
				if file_exists(bc_full) then
					return raw_io_open(bc_full, "rb")
				end
			end
		end
		return raw_io_open(path, mode)
	end

	-- Optional .ljbc package loader for require()
	if bytecode_mode and type(package.loaders) == "table" then
		table.insert(package.loaders, 2, function(modname)
			local rel = tostring(modname):gsub("%.", "/")
			local cands = {
				"lua/" .. rel .. ".ljbc",
				"lua/" .. rel .. "/init.ljbc",
				rel .. ".ljbc",
				rel .. "/init.ljbc",
				gdir .. "/lua/" .. rel .. ".ljbc",
				gdir .. "/" .. rel .. ".ljbc",
			}
			for _, p in ipairs(cands) do
				if file_exists(p) then
					local fn, err = loadfile(p)
					if fn then
						M.stats.bytecode_loads = M.stats.bytecode_loads + 1
						return fn
					end
					return "\n\tbytecode load error: " .. tostring(err)
				end
			end
		end)
	end

	local function raw_load_module(name, ...)
		local norm = tostring(name):gsub("\\", "/"):gsub("%.lua$", "")
		local path = resolve_module_path(norm)
		M.stats.loaded_modules = M.stats.loaded_modules + 1
		local func, err = loadfile(path)
		if not func then
			error("LoadModule() error loading '" .. path .. "': " .. tostring(err))
		end
		return func(...)
	end

	local function make_lazy_table(mod_name, stat_key_real)
		local real_tbl = nil
		local function ensure()
			if not real_tbl then
				M.stats[stat_key_real] = M.stats[stat_key_real] + 1
				real_tbl = raw_load_module(mod_name)
			end
			return real_tbl
		end
		return setmetatable({}, {
			__index = function(_, k)
				return ensure()[k]
			end,
		})
	end

	-- Exact Pearl of Tsoatha fast-path avoiding 258k-line Data/StatDescriptions/stat_descriptions.lua
	local function make_lazy_stat_describer()
		local real_describer = nil
		local pearl_supports = nil
		local pearl_slot_names = { "Helmet", "Gloves", "Boots", "Passive Tree" }
		return function(stats, scope_name, quality)
			if scope_name == "stat_descriptions" and type(stats) == "table" then
				local slot_idx = stats.local_pearl_random_support_gem_2_slot_index
				local gem_idx = stats.local_pearl_random_support_gem_2_index
				local lvl = stats.local_pearl_random_support_gem_2_level
				if slot_idx and gem_idx and lvl then
					local only_pearl = true
					for k, v in pairs(stats) do
						if k ~= "local_pearl_random_support_gem_2_slot_index"
							and k ~= "local_pearl_random_support_gem_2_index"
							and k ~= "local_pearl_random_support_gem_2_level" then
							if (type(v) == "number" and v ~= 0)
								or (type(v) == "table" and (v.min ~= 0 or v.max ~= 0)) then
								only_pearl = false
								break
							end
						end
					end
					if only_pearl then
						M.stats.hook_pearl_fastpath_calls = M.stats.hook_pearl_fastpath_calls + 1
						if not pearl_supports then
							local ok_req, tbl = pcall(require, "Data.PearlSupports")
							if ok_req and type(tbl) == "table" then
								pearl_supports = tbl
							else
								pearl_supports = raw_load_module("Data/PearlSupports")
							end
						end
						local gem = pearl_supports[gem_idx]
						local gem_text = gem and gem.baseItemName and gem.baseItemName:gsub(" Support", "") or ""
						local slot_name = pearl_slot_names[slot_idx] or tostring(slot_idx)
						local line
						if slot_idx == 4 then
							line = string.format(
								"Skills granted by your %s are Supported by level %d %s",
								slot_name, lvl, gem_text
							)
						else
							line = string.format(
								"Skills Socketed in your %s are Supported by level %d %s",
								slot_name, lvl, gem_text
							)
						end
						return { line }, { [line] = "local_pearl_random_support_gem_2_level" }
					end
				end
			end
			if not real_describer then
				M.stats.hook_stat_desc_real_loads = M.stats.hook_stat_desc_real_loads + 1
				real_describer = raw_load_module("Modules/StatDescriber")
			end
			return real_describer(stats, scope_name, quality)
		end
	end

	local function hooked_load_module(name, ...)
		local norm = tostring(name):gsub("\\", "/"):gsub("%.lua$", "")
		if skip_flavour and norm == "Data/FlavourText" then
			M.stats.hook_flavour_skipped = M.stats.hook_flavour_skipped + 1
			return {}
		end
		if lazy_stat_desc and norm == "Modules/StatDescriber" then
			return make_lazy_stat_describer()
		end
		if lazy_legacy_mods and norm == "Data/Crucible" then
			M.stats.hook_crucible_deferred = M.stats.hook_crucible_deferred + 1
			return make_lazy_table("Data/Crucible", "hook_crucible_real_loads")
		end
		if lazy_legacy_mods and norm == "Data/ModScourge" then
			M.stats.hook_scourge_deferred = M.stats.hook_scourge_deferred + 1
			return make_lazy_table("Data/ModScourge", "hook_scourge_real_loads")
		end

		local res = { raw_load_module(norm, ...) }

		if lazy_legacy_mods and norm == "Modules/Data" and data and data.itemMods and data.itemMods.Item then
			if not getmetatable(data.itemMods.Item) then
				setmetatable(data.itemMods.Item, {
					__index = function(_, k)
						return data.itemMods.Scourge[k]
					end,
				})
			end
		end

		if skip_item_dbs and norm == "Modules/Main" and main and main.Init then
			local orig_init = main.Init
			main.Init = function(self, ...)
				local r = orig_init(self, ...)
				if self.onFrameFuncs and self.onFrameFuncs["LoadItems"] then
					M._saved_load_items = self.onFrameFuncs["LoadItems"]
					self.onFrameFuncs["LoadItems"] = nil
					M.stats.hook_item_dbs_skipped = M.stats.hook_item_dbs_skipped + 1
				end
				if self.uniqueDB then
					self.uniqueDB.loading = nil
				end
				if self.rareDB then
					self.rareDB.loading = nil
				end
				if self.visceraDB then
					self.visceraDB.loading = nil
				end
				return r
			end
		end

		return unpack(res)
	end

	LoadModule = hooked_load_module

	PLoadModule = function(mod_name, ...)
		local ret = { pcall(hooked_load_module, mod_name, ...) }
		if ret[1] then
			table.remove(ret, 1)
			return nil, unpack(ret)
		end
		return ret[2]
	end

	_G.__POB_LAZY_STATS = M.stats
	M._installed = true
	return M
end

return M
