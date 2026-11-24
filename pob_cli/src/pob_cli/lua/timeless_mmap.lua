-- Cross-platform zero-copy mmap fallback for PoB timeless jewel LUTs (Win32 MapViewOfFile + POSIX mmap)
-- plus shared Timeless PRNG RPC grouping & PoB FindTimelessJewel modal drivers.

local ffi = require("ffi")
local t_sort, t_concat, s_fmt = table.sort, table.concat, string.format

local M = {}

local IS_WIN = (ffi.os == "Windows")
local kernel32 = nil

if IS_WIN then
	pcall(ffi.cdef, [[
		int MultiByteToWideChar(unsigned int cp, unsigned long flags,
			const char *mb, int cchMb, uint16_t *wc, int cchWc);
		void *CreateFileW(const uint16_t *name, unsigned long access, unsigned long share,
			void *sec, unsigned long disp, unsigned long flags, void *tmpl);
		int GetFileSizeEx(void *hFile, int64_t *lpFileSize);
		void *CreateFileMappingW(void *hFile, void *sec, unsigned long protect,
			unsigned long maxHigh, unsigned long maxLow, const uint16_t *name);
		void *MapViewOfFile(void *hMap, unsigned long access,
			unsigned long offHigh, unsigned long offLow, size_t bytes);
		int UnmapViewOfFile(const void *base);
		int CloseHandle(void *hObj);
	]])
	local ok, lib = pcall(ffi.load, "kernel32")
	if ok then kernel32 = lib end
else
	pcall(ffi.cdef, [[
		int pob_tl_open(const char *path, int oflag) asm("open");
		int64_t pob_tl_lseek(int fd, int64_t offset, int whence) asm("lseek");
		void *pob_tl_mmap(void *addr, size_t len, int prot, int flags, int fd, int64_t offset) asm("mmap");
		int pob_tl_munmap(void *addr, size_t len) asm("munmap");
		int pob_tl_close(int fd) asm("close");
	]])
end

local INVALID_HANDLE = ffi.cast("void *", -1)
local MAP_FAILED = ffi.cast("void *", -1)

local cfg = { game_dir = nil, cache_dir = nil }
local mapped_cache = {}
local gv_offsets_cache, gv_seed_size_cache = nil, nil

local function utf8_to_wide(s)
	local n = kernel32.MultiByteToWideChar(65001, 0, s, -1, nil, 0)
	if n <= 0 then return nil end
	local buf = ffi.new("uint16_t[?]", n)
	return (kernel32.MultiByteToWideChar(65001, 0, s, -1, buf, n) > 0) and buf or nil
end

function M.map_file(path)
	if type(path) ~= "string" or path == "" then return nil, "empty path" end
	if IS_WIN then
		if not kernel32 then return nil, "kernel32 unavailable" end
		local wpath = utf8_to_wide(path)
		if not wpath then return nil, "utf8 conversion failed" end
		local hfile = kernel32.CreateFileW(wpath, 0x80000000, 0x00000001, nil, 3, 0x80, nil)
		if hfile == nil or hfile == INVALID_HANDLE then return nil, "CreateFileW failed" end
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
		local closed = false
		local function cleanup()
			if closed then return end
			closed = true
			kernel32.UnmapViewOfFile(raw_ptr); kernel32.CloseHandle(hmap); kernel32.CloseHandle(hfile)
		end
		return {
			ptr = ffi.cast("const uint8_t *", raw_ptr), size = size, path = path,
			_anchor = ffi.gc(ffi.new("uint8_t[1]"), cleanup), close = cleanup,
		}
	end
	local fd = ffi.C.pob_tl_open(path, 0)
	if fd < 0 then return nil, "open failed" end
	local fsize = tonumber(ffi.C.pob_tl_lseek(fd, 0, 2))
	if not fsize or fsize <= 0 then ffi.C.pob_tl_close(fd); return nil, "empty or unreadable file" end
	local raw_ptr = ffi.C.pob_tl_mmap(nil, fsize, 1, 2, fd, 0)
	ffi.C.pob_tl_close(fd)
	if raw_ptr == nil or raw_ptr == MAP_FAILED then return nil, "mmap failed" end
	local closed = false
	local function cleanup()
		if closed then return end
		closed = true
		ffi.C.pob_tl_munmap(raw_ptr, fsize)
	end
	return {
		ptr = ffi.cast("const uint8_t *", raw_ptr), size = fsize, path = path,
		_anchor = ffi.gc(ffi.new("uint8_t[1]"), cleanup), close = cleanup,
	}
end

function M.configure(opts)
	opts = opts or {}
	if opts.game_dir then cfg.game_dir = opts.game_dir end
	if opts.cache_dir then cfg.cache_dir = opts.cache_dir end
end

local function file_exists(path)
	if not path then return false end
	local f = io.open(path, "rb")
	if f then f:close(); return true end
	return false
end

local function resolve_game_dir()
	if cfg.game_dir and cfg.game_dir ~= "" then return cfg.game_dir end
	if type(GetScriptPath) == "function" then
		local sp = GetScriptPath(); if sp and sp ~= "" then return sp end
	end
	return "."
end

local function resolve_cache_dir()
	if cfg.cache_dir and cfg.cache_dir ~= "" then return cfg.cache_dir end
	local env_dir = os.getenv("POB_TIMELESS_CACHE_DIR") or os.getenv("POB_CACHE_DIR")
	if env_dir and env_dir ~= "" then return env_dir .. "/timeless_lut" end
	local rt = os.getenv("POB_RUNTIME_DIR")
	if rt and rt ~= "" then return rt .. "/cache/timeless_lut" end
	local tmp = os.getenv("TEMP") or os.getenv("TMPDIR") or "/tmp"
	if tmp and tmp ~= "" then return tmp:gsub("\\", "/") .. "/pob4agents_timeless_lut" end
	return nil
end

local function active_tree_tag()
	local ver = (main and main.modes and main.modes["BUILD"] and main.modes["BUILD"].spec
		and main.modes["BUILD"].spec.treeVersion) or latestTreeVersion or "unknown"
	return tostring(ver):gsub("[^%w_.-]", "_")
end

local function read_compressed_lut(base_dir, stem)
	local f = io.open(base_dir .. "/" .. stem .. ".zip", "rb")
	if f then local raw = f:read("*a"); f:close(); if raw and #raw > 0 then return raw end end
	local parts, idx = {}, 0
	while true do
		local pf = io.open(base_dir .. "/" .. stem .. ".zip.part" .. idx, "rb")
		if not pf then break end
		parts[#parts + 1] = pf:read("*a"); pf:close(); idx = idx + 1
	end
	return (#parts > 0) and t_concat(parts) or nil
end

local function resolve_or_inflate_bin(stem)
	local data_dir = resolve_game_dir() .. "/Data/TimelessJewelData"
	local direct_bin = data_dir .. "/" .. stem .. ".bin"
	if file_exists(direct_bin) then return direct_bin end
	local cdir, tag = resolve_cache_dir(), active_tree_tag()
	if cdir then
		local cb = cdir .. "/" .. tag .. "_" .. stem .. ".bin"
		if file_exists(cb) then return cb end
		local pb = cdir .. "/" .. stem .. ".bin"
		if file_exists(pb) then return pb end
	end
	local comp = (cdir and read_compressed_lut(cdir, stem)) or read_compressed_lut(data_dir, stem)
	if not comp or type(Inflate) ~= "function" or not cdir then return nil end
	if type(MakeDir) == "function" then pcall(MakeDir, cdir) end
	local raw = Inflate(comp)
	if not raw or #raw == 0 then return nil end
	local out_bin = cdir .. "/" .. tag .. "_" .. stem .. ".bin"
	local wf = io.open(out_bin, "wb")
	if not wf then return nil end
	wf:write(raw); wf:close()
	return out_bin
end

local function read_u16(ptr, off) return ptr[off] + ptr[off + 1] * 256, off + 2 end
local function skip_mod(ptr, off)
	local cnt = ptr[off]; off = off + 1
	for _ = 1, cnt do off = off + 3 + ptr[off + 2] * 2 end
	return off
end
local function skip_sock(ptr, off)
	local cnt = ptr[off]; off = off + 1
	for _ = 1, cnt do off = skip_mod(ptr, off + 2) end
	return off
end
local function skip_asc(ptr, off) return off + 1 + ptr[off] * 2 end

local function convert_gid(jtype, lid)
	local m = data and data.nodeIDList and data.nodeIDList.localIdToGlobalId
	return (m and m[jtype] and m[jtype][lid] ~= nil) and m[jtype][lid] or lid
end

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
	local ptr = view.ptr
	if view.size < 14 then return nil end
	local fmt = ffi.string(ptr, 4)
	if fmt ~= "ABYS" and fmt ~= "ABYN" then return nil end
	local ver, stored_jt = ptr[4], ptr[5]
	local smin, off = read_u16(ptr, 6)
	local smax; smax, off = read_u16(ptr, off)
	local sinc; sinc, off = read_u16(ptr, off)
	if ver ~= 1 or stored_jt ~= jtype or sinc == 0 then return nil end
	local scnt = (smax - smin) / sinc + 1
	local idx = {
		format = fmt, seedMin = smin, seedMax = smax, seedInc = sinc, seedCount = scnt,
		blockOffsets = {}, seedOffsets = {}, ascendancyOffsets = nil,
	}
	if fmt == "ABYS" then
		local sock_cnt = ptr[off]; idx.abyssSize = ptr[off + 1]; off = off + 2
		local sids = {}
		for i = 1, sock_cnt do sids[i], off = read_u16(ptr, off) end
		for i = 1, sock_cnt do
			idx.blockOffsets[sids[i]] = off
			for _ = 1, scnt do off = skip_sock(ptr, off) end
		end
	else
		local ncnt; ncnt, off = read_u16(ptr, off)
		local nids = {}
		for i = 1, ncnt do nids[i], off = read_u16(ptr, off) end
		for i = 1, ncnt do
			idx.blockOffsets[nids[i]] = off
			for _ = 1, scnt do off = skip_mod(ptr, off) end
		end
		if off + 6 <= view.size and ffi.string(ptr + off, 4) == "ASCS" then
			off = off + 4
			local acnt; acnt, off = read_u16(ptr, off)
			idx.ascendancyOffsets = {}
			for _ = 1, acnt do
				local nlen = ptr[off]; off = off + 1
				local aname = ffi.string(ptr + off, nlen); off = off + nlen
				idx.ascendancyOffsets[aname] = off
				for _ = 1, scnt do off = skip_asc(ptr, off) end
			end
		end
	end
	return idx
end

local function build_gv_offsets()
	if gv_offsets_cache then return gv_offsets_cache, gv_seed_size_cache end
	local n_nodes = data.nodeIDList.size
	local ss = data.timelessJewelSeedMax[1] - data.timelessJewelSeedMin[1] + 1
	local by_idx = {}
	for nid, info in pairs(data.nodeIDList) do
		if type(nid) == "number" and type(info) == "table" then by_idx[info.index] = info.size end
	end
	local offs, cur = {}, n_nodes * ss
	for i = 0, n_nodes - 1 do offs[i] = cur; cur = cur + (by_idx[i] or 0) end
	gv_offsets_cache, gv_seed_size_cache = offs, ss
	return offs, ss
end

function M.ensure_mapped(jtype)
	local cached = mapped_cache[jtype]
	if cached ~= nil then return cached or nil end
	local jname = data and data.timelessJewelTypes and data.timelessJewelTypes[jtype]
	if not jname then mapped_cache[jtype] = false; return nil end
	local bin_path = resolve_or_inflate_bin(jname:gsub("%s+", ""))
	if not bin_path then mapped_cache[jtype] = false; return nil end
	local view = M.map_file(bin_path)
	if not view then mapped_cache[jtype] = false; return nil end
	local entry = { view = view, gv_node_pos = {}, abyss = parse_abyss_index(view, jtype) }
	mapped_cache[jtype] = entry
	return entry
end

function M.can_handle(jtype)
	if not jtype or not data or not data.timelessJewelTypes or not data.timelessJewelTypes[jtype] then return false end
	if mapped_cache[jtype] then return true end
	if mapped_cache[jtype] == false then return false end
	local stem = data.timelessJewelTypes[jtype]:gsub("%s+", "")
	local ddir = resolve_game_dir() .. "/Data/TimelessJewelData"
	if file_exists(ddir .. "/" .. stem .. ".bin") then return true end
	local cdir = resolve_cache_dir()
	if not cdir then return false end
	local tag = active_tree_tag()
	return file_exists(cdir .. "/" .. tag .. "_" .. stem .. ".bin")
		or file_exists(cdir .. "/" .. stem .. ".bin")
		or file_exists(cdir .. "/" .. stem .. ".zip") or file_exists(cdir .. "/" .. stem .. ".zip.part0")
		or file_exists(ddir .. "/" .. stem .. ".zip") or file_exists(ddir .. "/" .. stem .. ".zip.part0")
end

function M.readLUT(seed, nodeID, jtype, probe_only)
	if probe_only then return M.can_handle(jtype) end
	local entry = M.ensure_mapped(jtype)
	if not entry then return nil end
	local ptr, rseed = entry.view.ptr, (jtype == 5) and (seed / 20) or seed
	local smin, smax = data.timelessJewelSeedMin[jtype], data.timelessJewelSeedMax[jtype]
	if not smin or not smax or rseed < smin or rseed > smax then return {} end
	local soff, ssize = rseed - smin, smax - smin + 1
	local nidx = data.nodeIDList[nodeID] and data.nodeIDList[nodeID].index or nil
	if not nidx then return {} end
	if jtype == 1 then
		local gv_offs, gv_ss = build_gv_offsets()
		local pt = entry.gv_node_pos[nidx]
		if not pt then
			pt = {}
			local sz_base, cur = nidx * gv_ss, gv_offs[nidx]
			for s = 0, gv_ss - 1 do pt[s + 1] = cur; cur = cur + ptr[sz_base + s] end
			entry.gv_node_pos[nidx] = pt
		end
		local dlen, base, res = ptr[nidx * gv_ss + soff], pt[soff + 1], {}
		for i = 0, dlen - 1 do res[i + 1] = ptr[base + i] end
		if dlen == 2 or dlen == 3 then res[1] = convert_gid(jtype, res[1])
		elseif dlen == 6 or dlen == 8 then for i = 1, dlen / 2 do res[i] = convert_gid(jtype, res[i]) end end
		return res
	elseif nidx <= data.nodeIDList.sizeNotable then
		return { convert_gid(jtype, ptr[nidx * ssize + soff]) }
	end
	return {}
end

local function get_seed_offs(abyss, ptr, key, boff, skip_fn)
	if not boff then return nil end
	local offs = abyss.seedOffsets[key]
	if not offs then
		offs = {}
		local cur = boff
		for i = 1, abyss.seedCount do offs[i] = cur; cur = skip_fn(ptr, cur) end
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
	gv_offsets_cache, gv_seed_size_cache = nil, nil
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
	return { supported = true, jewelType = jt, additive = additive, nodes = nodes, seedCount = scnt, groupCount = #gorder, groups = gorder }
end

function M.run_finder_modal(params, resolve_jt)
	params = params or {}
	local build = main and main.modes and main.modes["BUILD"]
	assert(build and build.treeTab, "No active build loaded")
	local jtype = (resolve_jt and resolve_jt(params)) or tonumber(params.jewelType or params.jewel_type_id) or 2
	local target_socket = tonumber(params.socketId or params.socket)
	local use_filter, top_n, target_stat = (params.socketFilter ~= false), tonumber(params.top) or 15, params.stat or "CombinedDPS"
	build.timelessData = {
		jewelType = {}, conquerorType = {}, devotionVariant1 = 1, devotionVariant2 = 1,
		jewelSocket = {}, fallbackWeightMode = {}, searchList = "", searchListFallback = "",
		searchResults = {}, sharedResults = {},
	}
	local t0 = (type(GetTime) == "function") and GetTime() or 0
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
			top_seeds[i] = { seed = raw_res[i].seed, total = raw_res[i].total, label = raw_res[i].label }
		end
		local chosen = ctrl.socketSelect.list[chosen_idx]
		return {
			jewelType = jtype,
			jewelName = ctrl.jewelSelect.list[jtype] and ctrl.jewelSelect.list[jtype].label or tostring(jtype),
			socketId = chosen.id, socketLabel = chosen.label, socketFilter = eff_filter,
			allocatedNotablesInRadius = build.treeTab.allocatedNodesInRadiusCount or 0,
			matchedSeeds = #raw_res, topSeeds = top_seeds,
			totalMs = ((type(GetTime) == "function") and GetTime() or 0) - t0,
		}
	end)
	if main.ClosePopup then pcall(main.ClosePopup, main) end
	if not ok then error(res) end
	return res
end

function M.install_post_launch(opts)
	M.configure(opts)
	return true
end

return M
