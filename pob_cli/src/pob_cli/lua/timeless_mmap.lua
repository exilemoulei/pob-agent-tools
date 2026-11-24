-- Cross-platform zero-copy mmap fallback for PoB timeless jewel LUTs (Win32 MapViewOfFile + POSIX mmap)
-- plus PRNG seed grouping and the PoB FindTimelessJewel driver.

local ffi = require("ffi")
local t_sort, t_concat, s_fmt = table.sort, table.concat, string.format

local M = {}

local IS_WIN = (ffi.os == "Windows")
local kernel32 = nil

if IS_WIN then
	pcall(ffi.cdef, [[
		int MultiByteToWideChar(unsigned int, unsigned long, const char *, int, uint16_t *, int);
		void *CreateFileW(const uint16_t *, unsigned long, unsigned long, void *, unsigned long, unsigned long, void *);
		int GetFileSizeEx(void *, int64_t *);
		void *CreateFileMappingW(void *, void *, unsigned long, unsigned long, unsigned long, const uint16_t *);
		void *MapViewOfFile(void *, unsigned long, unsigned long, unsigned long, size_t);
		int UnmapViewOfFile(const void *);
		int CloseHandle(void *);
	]])
	local ok, lib = pcall(ffi.load, "kernel32")
	if ok then kernel32 = lib end
else
	pcall(ffi.cdef, [[
		int pob_tl_open(const char *, int) asm("open");
		int64_t pob_tl_lseek(int, int64_t, int) asm("lseek");
		void *pob_tl_mmap(void *, size_t, int, int, int, int64_t) asm("mmap");
		int pob_tl_munmap(void *, size_t) asm("munmap");
		int pob_tl_close(int) asm("close");
	]])
end

local MAP_FAILED = ffi.cast("void *", -1) -- also Win32's INVALID_HANDLE_VALUE

local cfg, mapped_cache = {}, {}
local gv_offsets_cache, gv_seed_size_cache, gv_end_cache, map_key

local function utf8_to_wide(s)
	local n = kernel32.MultiByteToWideChar(65001, 0, s, -1, nil, 0)
	local buf = n > 0 and ffi.new("uint16_t[?]", n)
	return buf and kernel32.MultiByteToWideChar(65001, 0, s, -1, buf, n) > 0 and buf or nil
end

local function as_view(raw_ptr, size, path, release)
	local closed = false
	local function cleanup() if not closed then closed = true; release() end end
	return {
		ptr = ffi.cast("const uint8_t *", raw_ptr), size = size, path = path,
		_anchor = ffi.gc(ffi.new("uint8_t[1]"), cleanup), close = cleanup,
	}
end

function M.map_file(path)
	if type(path) ~= "string" or path == "" then return nil, "empty path" end
	if IS_WIN then
		if not kernel32 then return nil, "kernel32 unavailable" end
		local wpath = utf8_to_wide(path)
		if not wpath then return nil, "utf8 conversion failed" end
		local hfile = kernel32.CreateFileW(wpath, 0x80000000, 0x00000001, nil, 3, 0x80, nil)
		if hfile == nil or hfile == MAP_FAILED then return nil, "CreateFileW failed" end
		local fsize = ffi.new("int64_t[1]")
		if kernel32.GetFileSizeEx(hfile, fsize) == 0 or fsize[0] <= 0 then
			kernel32.CloseHandle(hfile); return nil, "empty or unreadable file"
		end
		local size = tonumber(fsize[0])
		local hmap = kernel32.CreateFileMappingW(hfile, nil, 0x02, 0, 0, nil)
		if hmap == nil then kernel32.CloseHandle(hfile); return nil, "CreateFileMappingW failed" end
		local raw_ptr = kernel32.MapViewOfFile(hmap, 0x0004, 0, 0, 0)
		if raw_ptr == nil then
			kernel32.CloseHandle(hmap); kernel32.CloseHandle(hfile); return nil, "MapViewOfFile failed"
		end
		return as_view(raw_ptr, size, path, function()
			kernel32.UnmapViewOfFile(raw_ptr); kernel32.CloseHandle(hmap); kernel32.CloseHandle(hfile)
		end)
	end
	local fd = ffi.C.pob_tl_open(path, 0)
	if fd < 0 then return nil, "open failed" end
	local fsize = tonumber(ffi.C.pob_tl_lseek(fd, 0, 2))
	if not fsize or fsize <= 0 then ffi.C.pob_tl_close(fd); return nil, "empty or unreadable file" end
	local raw_ptr = ffi.C.pob_tl_mmap(nil, fsize, 1, 2, fd, 0)
	ffi.C.pob_tl_close(fd)
	if raw_ptr == nil or raw_ptr == MAP_FAILED then return nil, "mmap failed" end
	return as_view(raw_ptr, fsize, path, function() ffi.C.pob_tl_munmap(raw_ptr, fsize) end)
end

function M.configure(opts)
	opts = opts or {}
	cfg.game_dir, cfg.cache_dir = opts.game_dir or cfg.game_dir, opts.cache_dir or cfg.cache_dir
end

local function file_exists(path)
	local f = path and io.open(path, "rb")
	if f then f:close() end
	return f and true or false
end

local function resolve_game_dir()
	if cfg.game_dir and cfg.game_dir ~= "" then return cfg.game_dir end
	if type(GetScriptPath) == "function" then
		local sp = GetScriptPath(); if sp and sp ~= "" then return sp end
	end
	return "."
end

-- Keyed by NodeIndexMapping.lua's SHA-1 like timeless/worker.py: a table read against another PoB's node index
-- gives wrong mods silently.
local function resolve_cache_dir()
	if map_key == nil then
		map_key = false
		local f = io.open(resolve_game_dir() .. "/Data/TimelessJewelData/NodeIndexMapping.lua", "rb")
		local raw = f and f:read("*a"); if f then f:close() end
		local ok, sha1 = pcall(require, "sha1")
		if raw and ok and type(sha1) == "table" and sha1.sha1 then map_key = sha1.sha1(raw):sub(1, 16) end
	end
	-- bridge_client sets POB_CACHE_DIR to the cache timeless/worker.py fills
	local env_dir = os.getenv("POB_CACHE_DIR")
	local base = cfg.cache_dir or (env_dir and env_dir ~= "" and env_dir .. "/timeless_lut")
	return map_key and base and (base .. "/" .. map_key) or nil
end

local function mtime(path)
	local h = type(NewFileSearch) == "function" and NewFileSearch(path)
	return h and h:GetFileModifiedTime() or nil
end

-- mtime of the compressed table (0 if unknown, nil if none); fetched parts count once `.complete` exists.
local function zip_mtime(dir, p, need_marker)
	p = dir .. "/" .. p
	if file_exists(p .. ".zip") then return mtime(p .. ".zip") or 0 end
	if file_exists(p .. ".zip.part0") and (not need_marker or file_exists(p .. ".zip.complete")) then
		return mtime(p .. ".zip.part*") or 0
	end
end

-- As PoB's own loader decides: a .bin counts only when it is newer than its compressed source.
local function fresh(bin, src_m)
	if not file_exists(bin) then return false end
	local bm = src_m and mtime(bin)
	return not src_m or not bm or bm > src_m
end

local function read_compressed(p)
	local parts, f = {}, io.open(p .. ".zip", "rb")
	if f then local raw = f:read("*a"); f:close(); return (raw and #raw > 0) and raw or nil end
	while true do
		local pf = io.open(p .. ".zip.part" .. #parts, "rb")
		if not pf then break end
		parts[#parts + 1] = pf:read("*a"); pf:close()
	end
	return (#parts > 0) and t_concat(parts) or nil
end

local function resolve_or_inflate_bin(stem)
	local gdir, cdir = resolve_game_dir() .. "/Data/TimelessJewelData", resolve_cache_dir()
	local gm = zip_mtime(gdir, stem, false)
	if fresh(gdir .. "/" .. stem .. ".bin", gm) then return gdir .. "/" .. stem .. ".bin" end
	if not cdir then return nil end
	-- the installed PoB's own compressed table wins over a fetched one
	local src_m, out_bin = gm or zip_mtime(cdir, stem, true), cdir .. "/" .. stem .. ".bin"
	if fresh(out_bin, src_m) then return out_bin end
	if not src_m or type(Inflate) ~= "function" then return nil end
	local comp = read_compressed((gm and gdir or cdir) .. "/" .. stem)
	if not comp then return nil end
	if type(MakeDir) == "function" then pcall(MakeDir, cdir) end
	local raw = Inflate(comp)
	if not raw or #raw == 0 then return nil end
	-- per-process name: parallel engines never share a temp file
	local tmp_bin = out_bin .. "." .. tostring({}):match("%x+$") .. ".tmp"
	local wf = io.open(tmp_bin, "wb")
	if not wf then return nil end
	local ok_w = wf:write(raw)
	local ok_c = wf:close()
	if not ok_w or not ok_c then os.remove(tmp_bin); return nil end
	os.remove(out_bin)
	if not os.rename(tmp_bin, out_bin) then os.remove(tmp_bin); return file_exists(out_bin) and out_bin or nil end
	return out_bin
end

-- Walkers check `size` first: reading past a truncated mapping crashes LuaJIT instead of raising.
local function read_u16(ptr, off) return ptr[off] + ptr[off + 1] * 256, off + 2 end
local function skip_mod(ptr, off, size)
	if off >= size then return nil end
	local cnt = ptr[off]; off = off + 1
	for _ = 1, cnt do
		if off + 3 > size then return nil end
		off = off + 3 + ptr[off + 2] * 2
	end
	return off <= size and off or nil
end
local function skip_sock(ptr, off, size)
	if off >= size then return nil end
	local cnt = ptr[off]; off = off + 1
	for _ = 1, cnt do off = skip_mod(ptr, off + 2, size); if not off then return nil end end
	return off
end
local function skip_asc(ptr, off, size)
	if off >= size then return nil end
	off = off + 1 + ptr[off] * 2
	return off <= size and off or nil
end

local function convert_gid(jtype, lid)
	local m = data and data.nodeIDList and data.nodeIDList.localIdToGlobalId
	return (m and m[jtype] and m[jtype][lid] ~= nil) and m[jtype][lid] or lid
end

-- Only reached on records parse_abyss_index walked within bounds.
local function read_abyss_mod(ptr, off, jtype)
	local cnt, out = ptr[off], {}
	off = off + 1
	for i = 1, cnt do
		local ctype, lid, scnt = ptr[off], ptr[off + 1], ptr[off + 2]
		off = off + 3
		local rolls = {}
		for s = 1, scnt do
			local r; r, off = read_u16(ptr, off)
			rolls[s] = (r >= 32768) and (r - 65536) or r
		end
		out[i] = { type = ctype, id = convert_gid(jtype, lid), rolls = rolls }
	end
	return out, off
end

local function parse_abyss_index(view, jtype)
	local ptr, size = view.ptr, view.size
	if size < 14 then return nil end
	local fmt = ffi.string(ptr, 4)
	if fmt ~= "ABYS" and fmt ~= "ABYN" then return nil end
	local ver, stored_jt = ptr[4], ptr[5]
	local smin, off = read_u16(ptr, 6)
	local smax; smax, off = read_u16(ptr, off)
	local sinc; sinc, off = read_u16(ptr, off)
	if ver ~= 1 or stored_jt ~= jtype or sinc == 0 or smax < smin or (smax - smin) % sinc ~= 0 then return nil end
	local scnt = (smax - smin) / sinc + 1
	local idx = {
		format = fmt, seedMin = smin, seedMax = smax, seedInc = sinc, seedCount = scnt, size = size,
		blockOffsets = {}, seedOffsets = {},
	}
	local cnt, skip = nil, skip_mod
	if fmt == "ABYS" then
		cnt, skip = ptr[off], skip_sock; off = off + 2
	else
		cnt, off = read_u16(ptr, off)
	end
	if off + cnt * 2 > size then return nil end
	local ids = {}
	for i = 1, cnt do ids[i], off = read_u16(ptr, off) end
	for i = 1, cnt do
		idx.blockOffsets[ids[i]] = off
		for _ = 1, scnt do off = skip(ptr, off, size); if not off then return nil end end
	end
	if fmt == "ABYN" and off + 6 <= size and ffi.string(ptr + off, 4) == "ASCS" then
		local acnt; acnt, off = read_u16(ptr, off + 4)
		idx.ascendancyOffsets = {}
		for _ = 1, acnt do
			if off >= size or off + 1 + ptr[off] > size then return nil end
			local nlen = ptr[off]
			local aname = ffi.string(ptr + off + 1, nlen); off = off + 1 + nlen
			idx.ascendancyOffsets[aname] = off
			for _ = 1, scnt do off = skip_asc(ptr, off, size); if not off then return nil end end
		end
	end
	return idx
end

local function build_gv_offsets()
	if gv_offsets_cache then return gv_offsets_cache, gv_seed_size_cache, gv_end_cache end
	local n_nodes = data.nodeIDList.size
	local ss = data.timelessJewelSeedMax[1] - data.timelessJewelSeedMin[1] + 1
	local by_idx = {}
	for nid, info in pairs(data.nodeIDList) do
		if type(nid) == "number" and type(info) == "table" then by_idx[info.index] = info.size end
	end
	local offs, cur = {}, n_nodes * ss
	for i = 0, n_nodes - 1 do offs[i] = cur; cur = cur + (by_idx[i] or 0) end
	gv_offsets_cache, gv_seed_size_cache, gv_end_cache = offs, ss, cur
	return offs, ss, cur
end

-- nil when the table does not fit this PoB's data.
local function check_table(entry, jtype)
	local view, nl = entry.view, data.nodeIDList
	local abyss = (jtype >= 7 and jtype <= 11) or (jtype > 11 and view.size >= 4 and ffi.string(view.ptr, 2) == "AB")
	if abyss then
		entry.abyss = parse_abyss_index(view, jtype)
		return entry.abyss and entry or nil
	end
	if not nl then return nil end
	if jtype == 1 then return (select(3, build_gv_offsets()) <= view.size) and entry or nil end
	local smin, smax = data.timelessJewelSeedMin[jtype], data.timelessJewelSeedMax[jtype]
	return (smin and smax and nl.sizeNotable * (smax - smin + 1) == view.size) and entry or nil
end

-- Indexed by NodeIndexMapping, not tree version: one mapping per type serves all trees.
function M.ensure_mapped(jtype)
	local cached = mapped_cache[jtype]
	if cached ~= nil then return cached or nil end
	local jname = data and data.timelessJewelTypes and data.timelessJewelTypes[jtype]
	if not jname then return nil end
	-- absence is not cached: Python may fetch the table while this engine runs
	local bin_path = resolve_or_inflate_bin((jname:gsub("%s+", "")))
	if not bin_path then return nil end
	local view, err = M.map_file(bin_path)
	local entry = view and check_table({ view = view, gv_node_pos = {} }, jtype)
	if not entry then
		if view then view.close() end
		io.stderr:write("[timeless_mmap] skipped " .. bin_path .. ": " .. (err or "malformed or truncated") .. "\n")
	end
	mapped_cache[jtype] = entry or false
	return entry
end

function M.can_handle(jtype)
	return jtype ~= nil and M.ensure_mapped(jtype) ~= nil
end

function M.readLUT(seed, nodeID, jtype, probe_only)
	if probe_only then return M.can_handle(jtype) end
	local entry = M.ensure_mapped(jtype)
	if not entry then return nil end
	local ptr, rseed = entry.view.ptr, (jtype == 5) and (seed / 20) or seed
	local smin, smax = data.timelessJewelSeedMin[jtype], data.timelessJewelSeedMax[jtype]
	if not smin or not smax or rseed < smin or rseed > smax or rseed % 1 ~= 0 then return {} end
	local soff, ssize = rseed - smin, smax - smin + 1
	local nidx = data.nodeIDList[nodeID] and data.nodeIDList[nodeID].index or nil
	if not nidx then return {} end
	if jtype == 1 then
		if nidx >= data.nodeIDList.size then return {} end
		local gv_offs, gv_ss = build_gv_offsets()
		local pt = entry.gv_node_pos[nidx]
		if not pt then
			pt = {}
			local sz_base, cur = nidx * gv_ss, gv_offs[nidx]
			for s = 0, gv_ss - 1 do pt[s + 1] = cur; cur = cur + ptr[sz_base + s] end
			if cur > entry.view.size then return {} end
			entry.gv_node_pos[nidx] = pt
		end
		local dlen, base, res = ptr[nidx * gv_ss + soff], pt[soff + 1], {}
		for i = 0, dlen - 1 do res[i + 1] = ptr[base + i] end
		if dlen == 2 or dlen == 3 then res[1] = convert_gid(jtype, res[1])
		elseif dlen == 6 or dlen == 8 then for i = 1, dlen / 2 do res[i] = convert_gid(jtype, res[i]) end end
		return res
	elseif nidx < data.nodeIDList.sizeNotable then
		return { convert_gid(jtype, ptr[nidx * ssize + soff]) }
	end
	return {}
end

local function get_seed_offs(abyss, ptr, key, boff, skip_fn)
	if not boff then return nil end
	local offs = abyss.seedOffsets[key]
	if not offs then
		offs = {}; local cur = boff
		for i = 1, abyss.seedCount do offs[i] = cur; cur = skip_fn(ptr, cur, abyss.size) end
		abyss.seedOffsets[key] = offs
	end
	return offs
end

function M.readAbyssJewelLUT(seed, socketId, jtype, path, ascName, probe_only)
	if probe_only then return M.can_handle(jtype) end
	local entry = M.ensure_mapped(jtype)
	if not entry or not entry.abyss then return nil end
	local ptr, abyss = entry.view.ptr, entry.abyss
	local soff = seed - abyss.seedMin
	if soff < 0 or seed > abyss.seedMax or soff % abyss.seedInc ~= 0 then return {} end
	local sidx = soff / abyss.seedInc + 1
	if abyss.format == "ABYS" then
		local offs = get_seed_offs(abyss, ptr, socketId, abyss.blockOffsets[socketId], skip_sock)
		if not offs then return {} end
		local off = offs[sidx]; local cnt = ptr[off]; off = off + 1
		local affected = {}
		for _ = 1, cnt do
			local nid; nid, off = read_u16(ptr, off)
			affected[nid], off = read_abyss_mod(ptr, off, jtype)
		end
		return affected
	end
	if abyss.format ~= "ABYN" or not path then return {} end
	local affected = {}
	local function read_abyn(nid)
		local offs = get_seed_offs(abyss, ptr, nid, abyss.blockOffsets[nid], skip_mod)
		return offs and read_abyss_mod(ptr, offs[sidx], jtype) or {}
	end
	for nid in pairs(path) do local mod = read_abyn(nid); if #mod > 0 then affected[nid] = mod end end
	if abyss.ascendancyOffsets then
		local targets = ascName and { [ascName] = abyss.ascendancyOffsets[ascName] } or abyss.ascendancyOffsets
		for aname, boff in pairs(targets) do
			local offs = get_seed_offs(abyss, ptr, "asc:" .. aname, boff, skip_asc)
			if offs then
				local off = offs[sidx]; local scnt = ptr[off]; off = off + 1
				for _ = 1, scnt do local nid; nid, off = read_u16(ptr, off); affected[nid] = read_abyn(nid) end
			end
		end
	end
	return affected
end

function M.close_all()
	for k, e in pairs(mapped_cache) do
		if type(e) == "table" and e.view then e.view.close() end
		mapped_cache[k] = nil
	end
	gv_offsets_cache, gv_seed_size_cache, gv_end_cache = nil, nil, nil
end

local function encode_node_sig(prng, nid, seed, jt, ab)
	local rep, ns, r1, r2, r3, r4, na = prng.eval_node(nid, seed, jt)
	if not rep and na == 0 then return nil end
	local p = { tostring(nid), ":" }
	if rep then
		p[#p + 1] = "r" .. tostring(rep + ab)
		if ns >= 1 then p[#p + 1] = "," .. r1 end; if ns >= 2 then p[#p + 1] = "," .. r2 end
		if ns >= 3 then p[#p + 1] = "," .. r3 end; if ns >= 4 then p[#p + 1] = "," .. r4 end
	end
	local ids, ans_t, ar1, ar2 = prng.s_add_ids, prng.s_add_ns, prng.s_add_r1, prng.s_add_r2
	for k = 1, na do
		local ans = ans_t[k]
		p[#p + 1] = "+a" .. ids[k]
		if ans >= 1 then p[#p + 1] = "," .. ar1[k] end
		if ans >= 2 then p[#p + 1] = "," .. ar2[k] end
	end
	return t_concat(p)
end

local function encode_list(prng, ids, seed, jt, ab, extra)
	local parts = {}
	for i = 1, #ids do local e = encode_node_sig(prng, ids[i], seed, jt, ab); if e then parts[#parts + 1] = e end end
	if extra then parts[#parts + 1] = extra end
	return t_concat(parts, ";")
end

function M.group_hashes(prng, params, jt)
	local mult = (jt == 5) and 20 or 1
	local smin = tonumber(params.seedMin) or (data.timelessJewelSeedMin[jt] * mult)
	local smax = tonumber(params.seedMax) or (data.timelessJewelSeedMax[jt] * mult)
	local step = tonumber(params.seedStep) or mult
	local ab = (data and data.timelessJewelAdditions) or (prng.META and prng.META.additions_count) or 337
	local gmap, gorder, scnt = {}, {}, 0
	local function add_seed(sig, seed)
		local g = gmap[sig]
		if not g then g = { sig = sig, seeds = {} }; gmap[sig] = g; gorder[#gorder + 1] = g end
		g.seeds[#g.seeds + 1] = seed
	end
	local nodes, used_set, used_cnt = {}, {}, 0
	for _, nid in ipairs(params.nodes or {}) do nodes[#nodes + 1] = tonumber(nid) end
	t_sort(nodes)
	for _, nid in ipairs(params.usedNodes or params.nodes or {}) do
		local n = tonumber(nid); if n and not used_set[n] then used_set[n] = true; used_cnt = used_cnt + 1 end
	end
	local additive = (jt >= 2 and jt <= 6)
	if additive then
		local nmaps, nnext, hbuf, add_ids = {}, {}, {}, prng.s_add_ids
		for i = 1, #nodes do nmaps[i], nnext[i] = {}, 0 end
		for seed = smin, smax, step do
			scnt = scnt + 1
			for i = 1, #nodes do
				local rep, _, _, _, _, _, na = prng.eval_node(nodes[i], seed, jt)
				local gid = rep and (rep + ab) or (na > 0 and add_ids[1] or 0)
				local b = nmaps[i][gid]
				if b == nil then b = nnext[i]; nmaps[i][gid] = b; nnext[i] = b + 1 end
				hbuf[i] = s_fmt("%02x", b)
			end
			add_seed(t_concat(hbuf), seed)
		end
	elseif jt == 1 then
		if #nodes == 0 then for nid in pairs(used_set) do nodes[#nodes + 1] = nid end; t_sort(nodes) end
		for seed = smin, smax, step do scnt = scnt + 1; add_seed(encode_list(prng, nodes, seed, 1, ab), seed) end
	elseif jt >= 7 and jt <= 10 then
		local sid, hit, wout = tonumber(params.socketId or params.socket), {}, prng.walk_out
		for seed = smin, smax, step do
			scnt = scnt + 1
			local cnt, hits = prng.abyss_region_walk(sid, seed), {}
			for i = 1, cnt do
				local nid = wout[i]
				if used_cnt == 0 or used_set[nid] then hits[#hits + 1] = nid; hit[nid] = true end
			end
			t_sort(hits)
			add_seed(encode_list(prng, hits, seed, jt, ab), seed)
		end
		nodes = {}; for nid in pairs(hit) do nodes[#nodes + 1] = nid end; t_sort(nodes)
	else
		local sid = tonumber(params.socketId or params.socket)
		local build = main and main.modes and main.modes["BUILD"]
		local pnodes, hit = {}, {}
		if params.pathNodes and #params.pathNodes > 0 then
			for _, nid in ipairs(params.pathNodes) do pnodes[#pnodes + 1] = tonumber(nid) end
		elseif build and build.spec and sid and build.spec.GetShortestPathToClassStart then
			for nid in pairs(build.spec:GetShortestPathToClassStart(sid) or {}) do
				if used_cnt == 0 or used_set[nid] then pnodes[#pnodes + 1] = nid end
			end
		end
		t_sort(pnodes)
		for i = 1, #pnodes do hit[pnodes[i]] = true end
		local asc = params.ascendancyName or (build and build.spec and build.spec.curAscendClassName)
		for seed = smin, smax, step do
			scnt = scnt + 1
			local picked, extra = prng.abyss_ascendancy_pick(seed, asc), nil
			if picked and (used_cnt == 0 or used_set[picked]) then
				hit[picked] = true
				local e = encode_node_sig(prng, picked, seed, 11, ab)
				if e then extra = "asc:" .. e end
			end
			add_seed(encode_list(prng, pnodes, seed, 11, ab, extra), seed)
		end
		nodes = {}; for nid in pairs(hit) do nodes[#nodes + 1] = nid end; t_sort(nodes)
	end
	return { supported = true, jewelType = jt, additive = additive, nodes = nodes, seedCount = scnt, groups = gorder }
end

function M.run_finder_modal(params, resolve_jt)
	params = params or {}
	local build = main and main.modes and main.modes["BUILD"]
	assert(build and build.treeTab, "No active build loaded")
	local jtype = (resolve_jt and resolve_jt(params)) or tonumber(params.jewelType or params.jewel_type_id) or 2
	local target_socket = tonumber(params.socketId or params.socket)
	local use_filter, top_n, target_stat = (params.socketFilter ~= false), tonumber(params.top) or 15, params.stat or "CombinedDPS"
	build.timelessData = { jewelType = {}, conquerorType = {}, devotionVariant1 = 1, devotionVariant2 = 1,
		jewelSocket = {}, fallbackWeightMode = {}, searchList = "", searchListFallback = "", searchResults = {}, sharedResults = {} }
	build.treeTab:FindTimelessJewel()
	local popup = main.popups and main.popups[#main.popups]
	assert(popup and popup.controls, "FindTimelessJewel popup not created")
	local ok, res = pcall(function()
		local ctrl = popup.controls
		ctrl.jewelSelect:SetSel(jtype)
		if ctrl.jewelSelect.selFunc then ctrl.jewelSelect.selFunc(jtype, ctrl.jewelSelect.list[jtype]) end
		local chosen_idx, best_score = nil, -1
		for idx, entry in ipairs(ctrl.socketSelect.list or {}) do
			if entry.id and entry.id > 0 then
				if target_socket and entry.id == target_socket then chosen_idx = idx; break end
				local sc = (build.spec.allocNodes[entry.id] ~= nil) and 100 or 0
				if sc > best_score then best_score, chosen_idx = sc, idx end
			end
		end
		assert(chosen_idx, "No valid jewel socket found")
		ctrl.socketSelect:SetSel(chosen_idx)
		if ctrl.socketSelect.selFunc then ctrl.socketSelect.selFunc(chosen_idx, ctrl.socketSelect.list[chosen_idx]) end
		local eff_filter = use_filter and ((build.treeTab.allocatedNodesInRadiusCount or 0) > 0)
		ctrl.socketFilter.state = eff_filter
		if ctrl.socketFilter.stateFunc then ctrl.socketFilter.stateFunc(eff_filter) end
		for idx, entry in ipairs(ctrl.fallbackWeightsList.list or {}) do
			if entry.stat == target_stat then ctrl.fallbackWeightsList:SetSel(idx); break end
		end
		ctrl.fallbackWeightsButton.onClick(); ctrl.searchButton.onClick()
		local raw_res, top_seeds = build.timelessData.searchResults or {}, {}
		for i = 1, math.min(top_n, #raw_res) do
			top_seeds[i] = { seed = raw_res[i].seed, total = raw_res[i].total }
		end
		return { jewelType = jtype, socketId = ctrl.socketSelect.list[chosen_idx].id, socketFilter = eff_filter,
			matchedSeeds = #raw_res, topSeeds = top_seeds }
	end)
	if main.ClosePopup then pcall(main.ClosePopup, main) end
	if not ok then error(res) end
	return res
end

return M
