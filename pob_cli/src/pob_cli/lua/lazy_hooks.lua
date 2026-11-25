-- Zero-intrusion pre-launch lazy loading hooks for Path of Building.
-- Wraps LoadModule / PLoadModule before Launch.lua without editing upstream PoB:
--   1. Pearl of Tsoatha fast-path avoiding 258k-line stat_descriptions.lua at boot.
--   2. Data/FlavourText -> {} skip.
--   3. Defer Main.lua LoadItems uniqueDB/rareDB/visceraDB coroutine (drainable on demand via ensure_item_dbs).
-- Missing TreeData is not resolved here: Python stages the tree versions an RPC loads before
-- the engine runs it (bridge_trees.py).
-- Data/Crucible and Data/ModScourge load eagerly: PoB walks both with pairs() (Data.lua builds
-- itemMods.Item from Scourge, ItemsTab lists data.crucible), LuaJIT 5.1 has no __pairs, so a lazy
-- proxy looked empty, and together they cost about 10 ms.

local M = {}

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
	hook_flavour_skipped = 0,
	hook_pearl_fastpath_calls = 0,
	hook_stat_desc_real_loads = 0,
	hook_item_dbs_skipped = 0,
	hook_item_dbs_drained = 0,
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

-- game_dir: the PoE1 directory, already normalized by the host (forward slashes, no trailing slash).
function M.install_pre_launch(game_dir, opts)
	if M._installed then
		return M
	end
	opts = type(opts) == "table" and opts or {}

	local disable_all = os.getenv("POB_DISABLE_LAZY_HOOKS") == "1"
	local skip_flavour = (not disable_all) and opt_enabled(opts, "skip_flavour_text", true)
	local lazy_stat_desc = (not disable_all) and opt_enabled(opts, "lazy_stat_descriptions", true)
	local skip_item_dbs = (not disable_all) and opt_enabled(opts, "skip_item_dbs", true)

	local gdir = tostring(game_dir or ".")

	local function file_exists(p)
		local f = io.open(p, "rb")
		if f then
			f:close()
			return true
		end
		return false
	end

	local function resolve_module_path(norm)
		local lua_rel = norm .. ".lua"
		if file_exists(lua_rel) or is_abs_path(norm) then
			return lua_rel
		end
		local lua_full = gdir .. "/" .. lua_rel
		return file_exists(lua_full) and lua_full or lua_rel
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

		local res = { raw_load_module(norm, ...) }

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

	M._installed = true
	return M
end

return M
