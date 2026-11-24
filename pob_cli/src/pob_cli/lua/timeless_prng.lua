-- Pure LuaJIT TinyMT32 + Abyss Walk Timeless Jewel Engine with timeless_mmap fallback.
-- Hooks data.readLUT & data.readAbyssJewelLUT using compact timeless_meta_3_30.lua.gz (~3.7 KiB).

local bit = require("bit")
local band, bxor, lshift, rshift, tobit = bit.band, bit.bxor, bit.lshift, bit.rshift, bit.tobit
local m_floor, t_sort = math.floor, table.sort

local M = {}

local C0, C1, C2, C3 = tobit(0x40336050), tobit(0xCFA3723C), tobit(0x3CAC5F6F), tobit(0x3793FDFF)
local MAT1, MAT2, TMAT = tobit(0x8F7011EE), tobit(0xFC78FF1F), tobit(0x3793FDFF)
local AH_A, AL_A = rshift(tobit(0x19660D), 16), band(tobit(0x19660D), 0xFFFF)
local AH_B, AL_B = rshift(tobit(0x5D588B65), 16), band(tobit(0x5D588B65), 0xFFFF)
local AH_I, AL_I = rshift(tobit(1812433253), 16), band(tobit(1812433253), 0xFFFF)

local function mul32(v, ah, al)
	local vh, vl = rshift(v, 16), band(v, 0xFFFF)
	return tobit(lshift(vh * al + vl * ah, 16) + vl * al)
end
local function alpha(v) return mul32(bxor(v, rshift(v, 27)), AH_A, AL_A) end
local function bravo(v) return mul32(bxor(v, rshift(v, 27)), AH_B, AL_B) end
local function si(i, v) return tobit(i + mul32(bxor(v, rshift(v, 30)), AH_I, AL_I)) end

local st0, st1, st2, st3 = 0, 0, 0, 0
local memo_g, memo_s, m0, m1, m2, m3 = -1, -1, 0, 0, 0, 0

local function tinymt_next()
	local a = st3
	local b = bxor(bxor(band(st0, 0x7FFFFFFF), st1), st2)
	a = bxor(a, lshift(a, 1))
	b = bxor(b, bxor(rshift(b, 1), a))
	st0, st1, st2, st3 = st1, st2, bxor(a, lshift(b, 10)), b
	local mask = -band(b, 1)
	st1, st2 = bxor(st1, band(mask, MAT1)), bxor(st2, band(mask, MAT2))
end

local function warmup8()
	tinymt_next(); tinymt_next(); tinymt_next(); tinymt_next()
	tinymt_next(); tinymt_next(); tinymt_next(); tinymt_next()
end

local function tinymt_u32()
	tinymt_next()
	local b = tobit(st0 + rshift(st2, 8))
	local r = bxor(bxor(st3, b), band(-band(b, 1), TMAT))
	return r + rshift(r, 31) * 4294967296
end

local function init_pair_raw(g, s)
	local a, b, c, d, r = C0, C1, C2, C3, 0
	r = alpha(bxor(bxor(b, c), a)); c = tobit(c + r); r = tobit(r + g + 1); d = tobit(d + r); b = r
	r = alpha(bxor(bxor(c, d), b)); d = tobit(d + r); r = tobit(r + s + 2); a = tobit(a + r); c = r
	r = alpha(bxor(bxor(d, a), c)); a = tobit(a + r); r = tobit(r + 3); b = tobit(b + r); d = r
	r = alpha(bxor(bxor(a, b), d)); b = tobit(b + r); c = tobit(c + r); a = r
	r = alpha(bxor(bxor(b, c), a)); c = tobit(c + r); r = tobit(r + 1); d = tobit(d + r); b = r
	r = alpha(bxor(bxor(c, d), b)); d = tobit(d + r); r = tobit(r + 2); a = tobit(a + r); c = r
	r = alpha(bxor(bxor(d, a), c)); a = tobit(a + r); r = tobit(r + 3); b = tobit(b + r); d = r
	r = bravo(tobit(a + b + d)); b = bxor(b, r); c = bxor(c, r); a = r
	r = bravo(tobit(b + c + a)); c = bxor(c, r); r = tobit(r - 1); d = bxor(d, r); b = r
	r = bravo(tobit(c + d + b)); d = bxor(d, r); r = tobit(r - 2); a = bxor(a, r); c = r
	r = bravo(tobit(d + a + c)); a = bxor(a, r); r = tobit(r - 3); b = bxor(b, r); d = r
	st0, st1, st2, st3 = a, b, c, d
	warmup8()
end

local function reset_pair(g, s)
	if memo_g == g and memo_s == s then st0, st1, st2, st3 = m0, m1, m2, m3; return end
	init_pair_raw(g, s)
	memo_g, memo_s, m0, m1, m2, m3 = g, s, st0, st1, st2, st3
end

local function reset_single(seed)
	local s0, s1, s2, s3 = tobit(seed), MAT1, MAT2, TMAT
	s1 = bxor(s1, si(1, s0)); s2 = bxor(s2, si(2, s1)); s3 = bxor(s3, si(3, s2))
	s0 = bxor(s0, si(4, s3)); s1 = bxor(s1, si(5, s0)); s2 = bxor(s2, si(6, s1))
	s3 = bxor(s3, si(7, s2))
	st0, st1, st2, st3 = s0, s1, s2, s3
	warmup8()
end

local function draw_mod(n) return (n <= 1) and 0 or (tinymt_u32() % n) end

local function draw_msvc_slow(n, cutoff)
	while true do
		local v = tinymt_u32()
		if v < cutoff then return v % n end
	end
end

local function draw_msvc(n)
	if n <= 1 then return 0 end
	local cutoff = 4294967296 - (4294967296 % n)
	local v = tinymt_u32()
	return (v < cutoff) and (v % n) or draw_msvc_slow(n, cutoff)
end

local function roll_range(lo, hi, df) return (hi > lo) and (lo + df(hi - lo + 1)) or lo end

local META, NODE_PTYPE, NODE_IS_ASC, REGION_G, ASC_G, MMAP_MOD = nil, nil, nil, nil, nil, nil
local PTYPE_CACHE, ACTIVE_TV = {}, nil
local SCRIPT_DIR = ((debug.getinfo(1, "S") and debug.getinfo(1, "S").source) or ""):match("^@?(.*)[/\\][^/\\]+$") or "."

local function gcd(a, b) while b ~= 0 do a, b = b, a % b end; return a end

local function build_addition_luts(meta)
	for _, by_pt in pairs(meta.additions) do
		for _, ab in pairs(by_pt) do
			local list, g = ab.list, 0
			for i = 1, #list do g = gcd(g, list[i][2]) end
			if g > 0 and (ab.total_w / g) <= 4096 then
				local lut, pos = {}, 1
				for i = 1, #list do
					local e = list[i]
					for _ = 1, e[2] / g do lut[pos] = e; pos = pos + 1 end
				end
				ab.gcd, ab.lut = g, lut
			end
		end
	end
end

local function is_small_attr(node)
	if node.type ~= "Normal" or not node.sd or #node.sd ~= 1 then return false end
	local s = node.sd[1]
	return s:match("^%+%d+ to Strength$") ~= nil or s:match("^%+%d+ to Dexterity$") ~= nil
		or s:match("^%+%d+ to Intelligence$") ~= nil
end

local function is_scaffolding(n)
	return n.isProxy == true or (n.expansionJewel ~= nil and n.expansionJewel.parent ~= nil)
end

local function raw_links(n)
	local out = {}
	if n["in"] then for i = 1, #n["in"] do out[#out + 1] = tonumber(n["in"][i]) end end
	if n.out then for i = 1, #n.out do out[#out + 1] = tonumber(n.out[i]) end end
	return out
end

local function build_region_graph(nodes)
	local id2d, d2id, weight, conq, links, sockets, nd = {}, {}, {}, {}, {}, {}, 0
	-- 3.30 Pitfall #1: exclude masteries, class starts, and Putrid Seed Blightcore proxyKey nodes
	local function walkable(nid)
		local n = nodes[nid]
		return n ~= nil and n.type ~= "Mastery" and not n.isMastery
			and n.classStartIndex == nil and n.proxyKey == nil
	end
	for nid, n in pairs(nodes) do
		if type(nid) == "number" and walkable(nid) then
			nd = nd + 1
			id2d[nid], d2id[nd] = nd, nid
			weight[nd] = (n.isNotable or n.type == "Notable") and 25 or (is_small_attr(n) and 1 or 5)
			local scaff = is_scaffolding(n)
			conq[nd] = (not n.isJewelSocket) and (not scaff) and (n.ascendancyName == nil)
			if n.isJewelSocket and (not scaff) and (n.ascendancyName == nil) then sockets[nid] = nd end
		end
	end
	local vis = {}
	for d = 1, nd do
		local rl, adj = raw_links(nodes[d2id[d]]), {}
		for i = 1, #rl do local od = id2d[rl[i]]; if od then adj[#adj + 1] = od end end
		links[d], vis[d] = adj, 0
	end
	return {
		dense_to_id = d2id, weight = weight, conquerable = conq,
		links = links, sockets = sockets, vis = vis, gen = 0, f_id = {}, f_w = {},
	}
end

local function build_ascendancy_graph(tree, nodes)
	local ord_starts = {}
	for nid, n in pairs(nodes) do
		if type(nid) == "number" and n.isAscendancyStart and n["in"] and #n["in"] > 0 then
			ord_starts[#ord_starts + 1] = nid
		end
	end
	t_sort(ord_starts)
	local root_edges, class_start, keep, cost = {}, {}, {}, {}
	for i = 1, #META.root_out do root_edges[#root_edges + 1] = META.root_out[i] end
	for i = 1, #ord_starts do
		local sid = ord_starts[i]
		root_edges[#root_edges + 1] = sid
		local n, bridge = nodes[sid], tonumber(nodes[sid]["in"][1])
		class_start[n.ascendancyName], keep[bridge], cost[sid] = bridge, true, 0
		local q, head = { sid }, 1
		while head <= #q do
			local cur = q[head]; head = head + 1
			for _, o in ipairs(raw_links(nodes[cur])) do
				local t = nodes[o]
				if cost[o] == nil and t and t.ascendancyName == n.ascendancyName then
					cost[o] = cost[cur] + 1; q[#q + 1] = o
				end
			end
		end
	end
	for nid, n in pairs(nodes) do if type(nid) == "number" and n.ascendancyName then keep[nid] = true end end
	local asc_nodes, disp2int, all_asc = {}, {}, {}
	for nid in pairs(keep) do
		local n, c = nodes[nid], cost[nid]
		asc_nodes[nid] = {
			asc = n.ascendancyName or "", links = raw_links(n),
			collectable = (n.isNotable or n.type == "Notable") and (not n.isMultipleChoice)
				and (not n.isMultipleChoiceOption) and (not n.isAscendancyStart) and (c ~= nil and c < 4),
		}
	end
	for _, cls in pairs(tree.classes or {}) do
		for _, asc in pairs(cls.classes or {}) do
			if asc.id and asc.name then disp2int[asc.name], disp2int[asc.id] = asc.id, asc.id end
		end
	end
	for k in pairs(class_start) do all_asc[#all_asc + 1] = k end
	t_sort(all_asc)
	return {
		root_edges = root_edges, class_start = class_start, all_asc = all_asc,
		nodes = asc_nodes, disp_to_internal = disp2int,
	}
end

local function load_meta_file(meta_path)
	local path = meta_path or (SCRIPT_DIR .. "/../data/timeless_meta_3_30.lua.gz")
	local fh = assert(io.open(path, "rb"), "timeless_prng: cannot open " .. tostring(path))
	local raw = fh:read("*a"); fh:close()
	local src = raw
	if #raw >= 2 and raw:byte(1) == 0x1F and raw:byte(2) == 0x8B then
		assert(type(Inflate) == "function", "timeless_prng: Inflate() required for .lua.gz")
		src = assert(Inflate(raw), "timeless_prng: failed to inflate " .. tostring(path))
	end
	return assert(loadstring(src, "@" .. tostring(path)))()
end

local function active_tree_ver()
	return (main and main.modes and main.modes["BUILD"] and main.modes["BUILD"].spec
		and main.modes["BUILD"].spec.treeVersion) or latestTreeVersion
end

local function ensure_tree_graphs(need_abyss)
	if not (main and main.tree) then return false end
	local tv = active_tree_ver() or (META and META.tree_version) or latestTreeVersion
	local tree = (tv and main.tree[tv]) or (META and META.tree_version and main.tree[META.tree_version])
		or (latestTreeVersion and main.tree[latestTreeVersion])
	if not tree then for k, t in pairs(main.tree) do tv, tree = k, t; break end end
	if not (tree and tree.nodes) then return false end
	if ACTIVE_TV ~= tv or NODE_PTYPE == nil then
		local cached = PTYPE_CACHE[tv]
		if not cached then
			local pt, is_asc = {}, {}
			for nid, node in pairs(tree.nodes) do
				if type(nid) == "number" and type(node) == "table" then
					if node.type == "Socket" then pt[nid] = 5
					elseif node.type == "Keystone" then pt[nid] = 4
					elseif node.type == "Notable" then pt[nid] = 3
					elseif is_small_attr(node) then pt[nid] = 1
					elseif node.type == "Normal" then pt[nid] = 2 end
					if node.ascendancyName ~= nil then is_asc[nid] = true end
				end
			end
			cached = { ptype = pt, is_asc = is_asc }
			PTYPE_CACHE[tv] = cached
		end
		NODE_PTYPE, NODE_IS_ASC, ACTIVE_TV = cached.ptype, cached.is_asc, tv
	end
	if need_abyss and (REGION_G == nil or ASC_G == nil) then
		local atree = (META and META.tree_version and main.tree[META.tree_version]) or tree
		if atree and atree.nodes then
			REGION_G = build_region_graph(atree.nodes)
			ASC_G = build_ascendancy_graph(atree, atree.nodes)
		end
	end
	return true
end

function M.ensure_init(meta_path)
	if META == nil then META = load_meta_file(meta_path); build_addition_luts(META) end
	ensure_tree_graphs(true)
	M.META = META
	return META
end

local s_add_ids, s_add_r1, s_add_r2, s_add_ns = {}, {}, {}, {}
M.s_add_ids, M.s_add_r1, M.s_add_r2, M.s_add_ns = s_add_ids, s_add_r1, s_add_r2, s_add_ns

local function roll_additions(jtype, ptype, min_a, max_a, df)
	local cnt = roll_range(min_a, max_a, df)
	if cnt == 0 then return 0 end
	local ab = META.additions[jtype] and META.additions[jtype][ptype]
	if not ab then return 0 end
	local lut, g, tw = ab.lut, ab.gcd, ab.total_w
	for k = 1, cnt do
		local roll, p = df(tw), nil
		if lut then p = lut[m_floor(roll / g) + 1]
		else
			for i = 1, #ab.list do
				local a = ab.list[i]; if a[2] > roll then p = a; break end; roll = roll - a[2]
			end
		end
		if p then
			local ns = p[3]
			local r1 = (ns >= 1) and roll_range(p[4], p[5], df) or p[4]
			local r2 = (ns >= 2) and roll_range(p[6], p[7], df) or p[6]
			s_add_ids[k], s_add_ns[k], s_add_r1[k], s_add_r2[k] = p[1], ns, r1, r2
		end
	end
	return cnt
end

function M.eval_node(nid, seed, jtype)
	local pt = NODE_PTYPE[nid]
	if not pt or pt == 5 then return nil, 0, 0, 0, 0, 0, 0 end
	if pt == 4 then
		-- 3.30 Pitfall #2: Abyss socket-keyed jewels (7..10) carry fixed PassiveType==4 keystone replacement
		local ks = (jtype >= 7) and META.skills[jtype] and META.skills[jtype][4]
		if ks and ks[1] then local c = ks[1]; return c[1], c[3], c[4], c[6], c[8], c[10], 0 end
		return nil, 0, 0, 0, 0, 0, 0
	end
	local ver, rseed = META.versions[jtype], (jtype == 5) and (seed / 20) or seed
	local df = ver.msvc and draw_msvc or draw_mod
	local asc_c = (pt == 3 and NODE_IS_ASC[nid]) and (META.skills[jtype] and META.skills[jtype][5]) or nil
	local rep
	if asc_c then rep = true
	elseif pt == 3 then
		if ver.not_w >= 100 then rep = true
		elseif ver.not_w == 0 then rep = false
		else reset_pair(nid, rseed); rep = df(101) < ver.not_w end
	elseif pt == 1 then rep = ver.sa_rep
	else rep = ver.sn_rep end

	local bmin, bmax = ver.min_sn, ver.max_sn
	if pt == 1 then bmin, bmax = ver.min_sa, ver.max_sa
	elseif pt == 3 then bmin, bmax = ver.min_no, ver.max_no end

	if rep then
		local cands = asc_c or (META.skills[jtype] and META.skills[jtype][pt])
		if not cands or #cands == 0 then return nil, 0, 0, 0, 0, 0, 0 end
		reset_pair(nid, rseed)
		if pt == 3 and not asc_c then df(101) end
		local cw, ch = 0, nil
		for i = 1, #cands do
			local c = cands[i]; cw = cw + c[2]
			if df(cw) < c[2] then ch = c end
		end
		local ns = ch[3]
		local r1 = (ns >= 1) and roll_range(ch[4], ch[5], df) or ch[4]
		local r2 = (ns >= 2) and roll_range(ch[6], ch[7], df) or ch[6]
		local r3 = (ns >= 3) and roll_range(ch[8], ch[9], df) or ch[8]
		local r4 = (ns >= 4) and roll_range(ch[10], ch[11], df) or ch[10]
		if ch[12] == 0 and ch[13] == 0 then return ch[1], ns, r1, r2, r3, r4, 0 end
		return ch[1], ns, r1, r2, r3, r4, roll_additions(jtype, pt, bmin + ch[12], bmax + ch[13], df)
	end
	reset_pair(nid, rseed)
	if pt == 3 then df(101) end
	return nil, 0, 0, 0, 0, 0, roll_additions(jtype, pt, bmin, bmax, df)
end

local walk_out = {}
M.walk_out = walk_out

function M.abyss_region_walk(socket_id, seed)
	local sd = REGION_G.sockets[socket_id]
	if not sd then return 0 end
	init_pair_raw(socket_id, seed)
	local gen = REGION_G.gen + 1; REGION_G.gen = gen
	local vis, links, wt, conq, d2id = REGION_G.vis, REGION_G.links, REGION_G.weight, REGION_G.conquerable, REGION_G.dense_to_id
	local fid, fw = REGION_G.f_id, REGION_G.f_w
	fid[1], fw[1] = sd, 1
	local flen, total, out_n = 1, 1, 0
	for _ = 1, 60 do
		if flen == 0 then break end
		local roll, pi = draw_msvc(total), 1
		while pi < flen do local w = fw[pi]; if w > roll then break end; roll = roll - w; pi = pi + 1 end
		local picked = fid[pi]
		total = total - fw[pi]
		for j = pi, flen - 1 do fid[j], fw[j] = fid[j + 1], fw[j + 1] end
		flen = flen - 1
		for _, o in ipairs(links[picked]) do
			if vis[o] ~= gen then
				vis[o] = gen; local ow = wt[o]
				total = total + ow; flen = flen + 1; fid[flen], fw[flen] = o, ow
			end
		end
		if conq[picked] then out_n = out_n + 1; walk_out[out_n] = d2id[picked] end
	end
	return out_n
end

function M.abyss_ascendancy_pick(seed, asc_name)
	if not asc_name or asc_name == "" or asc_name == "None" then return nil end
	local internal = ASC_G.disp_to_internal[asc_name] or asc_name
	local bridge, an = ASC_G.class_start[internal], ASC_G.nodes
	reset_single(seed)
	local visited, frontier, flen = {}, { -1 }, 1
	for _ = 1, 500 do
		if flen == 0 then break end
		local pi = draw_msvc(flen) + 1
		local g = frontier[pi]
		for j = pi, flen - 1 do frontier[j] = frontier[j + 1] end
		flen = flen - 1
		local edges = (g == -1) and ASC_G.root_edges or an[g].links
		for j = 1, #edges do
			local t = edges[j]
			if not visited[t] then
				visited[t] = true
				local node = an[t]
				if node then
					if bridge and t == bridge then flen = flen + 1; frontier[flen] = t
					elseif node.asc == internal then
						if node.collectable then return t end
						flen = flen + 1; frontier[flen] = t
					end
				end
			end
		end
	end
	return nil
end

local function valid_seed(seed, jt)
	local smin = data and data.timelessJewelSeedMin and data.timelessJewelSeedMin[jt]
	local smax = data and data.timelessJewelSeedMax and data.timelessJewelSeedMax[jt]
	if not seed or not smin or not smax then return false end
	if jt == 5 then return (seed % 20 == 0) and (seed / 20 >= smin) and (seed / 20 <= smax) end
	return seed >= smin and seed <= smax and seed % 1 == 0
end

function M.readLUT(seed, nodeID, jt)
	if not valid_seed(seed, jt) or not ensure_tree_graphs() then return {} end
	local rep, ns, r1, r2, r3, r4, na = M.eval_node(nodeID, seed, jt)
	local ab = (data and data.timelessJewelAdditions) or META.additions_count
	if jt == 1 then
		if na > 0 then
			local out = {}
			for k = 1, na do out[k] = s_add_ids[k]; out[na + k] = s_add_r1[k] end
			return out
		elseif rep then
			if ns == 1 then return { rep + ab, r1 }
			elseif ns == 2 then return { rep + ab, r1, r2 }
			elseif ns == 3 then return { rep + ab, r1, r2, r3 }
			else return { rep + ab, r1, r2, r3, r4 } end
		end
		return {}
	end
	return rep and { rep + ab } or (na > 0 and { s_add_ids[1] } or {})
end

local function build_abyss_mod(nid, seed, jt)
	local rep, ns, r1, r2, r3, r4, na = M.eval_node(nid, seed, jt)
	local ab, mods = (data and data.timelessJewelAdditions) or META.additions_count, {}
	if rep then
		local r = {}
		if ns >= 1 then r[1] = r1 end; if ns >= 2 then r[2] = r2 end
		if ns >= 3 then r[3] = r3 end; if ns >= 4 then r[4] = r4 end
		mods[#mods + 1] = { type = 1, id = rep + ab, rolls = r }
	end
	for k = 1, na do
		local r, ans = {}, s_add_ns[k]
		if ans >= 1 then r[1] = s_add_r1[k] end; if ans >= 2 then r[2] = s_add_r2[k] end
		mods[#mods + 1] = { type = 2, id = s_add_ids[k], rolls = r }
	end
	return mods
end

function M.readAbyssJewelLUT(seed, socketId, jt, path, ascName)
	local affected = {}
	if not valid_seed(seed, jt) or not ensure_tree_graphs(true) then return affected end
	if jt >= 7 and jt <= 10 then
		local cnt = M.abyss_region_walk(socketId, seed)
		for i = 1, cnt do local nid = walk_out[i]; affected[nid] = build_abyss_mod(nid, seed, jt) end
		return affected
	elseif jt ~= 11 or not path then return affected end
	for nid in pairs(path) do
		local mods = build_abyss_mod(nid, seed, 11)
		if #mods > 0 then affected[nid] = mods end
	end
	-- 3.30 Pitfall #3: PassiveSpec.lua:1142 passes ascendancyName == nil expecting all ascendancies
	if ascName == nil then
		for i = 1, #ASC_G.all_asc do
			local p = M.abyss_ascendancy_pick(seed, ASC_G.all_asc[i])
			if p then affected[p] = build_abyss_mod(p, seed, 11) end
		end
	elseif ascName ~= "None" and ascName ~= "" then
		local p = M.abyss_ascendancy_pick(seed, ascName)
		if p then affected[p] = build_abyss_mod(p, seed, 11) end
	end
	return affected
end

local NAME_TO_TYPE = {
	["glorious vanity"] = 1, ["lethal pride"] = 2, ["brutal restraint"] = 3,
	["militant faith"] = 4, ["elegant hubris"] = 5, ["heroic tragedy"] = 6,
	["abyss tecrod"] = 7, ["festering vengeance"] = 7, ["abyss ulaman"] = 8, ["extinguishing grasp"] = 8,
	["abyss kurgal"] = 9, ["baleful dominion"] = 9, ["abyss amanamu"] = 10, ["destructive aspiration"] = 10,
	["abyss zorath"] = 11, ["reclaimed malevolence"] = 11,
}

local function resolve_jt(params)
	local jt = tonumber(params.jewelType or params.jewel_type_id)
	if jt then return m_floor(jt) end
	local name = params.jewel or params.jewelName
	if type(name) == "string" then
		local low = name:lower()
		if NAME_TO_TYPE[low] then return NAME_TO_TYPE[low] end
		for k, v in pairs((data and data.timelessJewelTypes) or {}) do
			if type(v) == "string" and v:lower() == low then return k end
		end
	end
	return nil
end

local function can_use_prng(jt)
	if not META or not jt or jt < 1 or jt > 11 or not META.versions[jt] then return false end
	if jt <= 6 then return true end
	return active_tree_ver() == (META.tree_version or "3_30")
end

local function load_sibling_mmap()
	if MMAP_MOD then return MMAP_MOD end
	local f = loadfile(SCRIPT_DIR .. "/timeless_mmap.lua")
	if f then local ok, mod = pcall(f); if ok and type(mod) == "table" then MMAP_MOD = mod; return mod end end
	local ok, mod = pcall(require, "timeless_mmap")
	if ok and type(mod) == "table" then MMAP_MOD = mod; return mod end
	return nil
end

M.rpc_handlers = {
	timeless_prng_group_hashes = function(params)
		params = params or {}
		local jt = resolve_jt(params)
		if not can_use_prng(jt) or not ensure_tree_graphs(jt >= 7) then
			return {
				supported = false, jewelType = jt,
				activeTreeVersion = active_tree_ver(), metaTreeVersion = META and META.tree_version,
			}
		end
		local mmap = assert(load_sibling_mmap(), "timeless_mmap unavailable")
		return mmap.group_hashes(M, params, jt)
	end,
	timeless_prng_search = function(params)
		local mmap = assert(load_sibling_mmap(), "timeless_mmap unavailable")
		return mmap.run_finder_modal(params, resolve_jt)
	end,
}

function M.install_post_launch(meta_gz_path, opts)
	opts = opts or {}
	M.ensure_init(meta_gz_path)
	local mmap = opts.mmap or load_sibling_mmap()
	MMAP_MOD = mmap
	if mmap and mmap.configure then mmap.configure(opts) end
	local fast_lut = (opts.fast_lut == true) or (opts.force_mmap == true) or (os.getenv("POB_FAST_LUT") == "1")
	local orig_l = opts.fallback_readLUT or (data and data.readLUT)
	local orig_a = opts.fallback_readAbyss or (data and data.readAbyssJewelLUT)
	if data then
		data.readLUT = function(seed, nodeID, jt)
			if fast_lut and mmap and mmap.readLUT(seed, nodeID, jt, true) then
				local r = mmap.readLUT(seed, nodeID, jt, false); if r ~= nil then return r end
			end
			if can_use_prng(jt) then return M.readLUT(seed, nodeID, jt) end
			if mmap and mmap.readLUT(seed, nodeID, jt, true) then
				local r = mmap.readLUT(seed, nodeID, jt, false); if r ~= nil then return r end
			end
			return orig_l and orig_l(seed, nodeID, jt) or {}
		end
		data.readAbyssJewelLUT = function(seed, sid, jt, path, asc)
			if fast_lut and mmap and mmap.readAbyssJewelLUT(seed, sid, jt, path, asc, true) then
				local r = mmap.readAbyssJewelLUT(seed, sid, jt, path, asc, false); if r ~= nil then return r end
			end
			if can_use_prng(jt) then return M.readAbyssJewelLUT(seed, sid, jt, path, asc) end
			if mmap and mmap.readAbyssJewelLUT(seed, sid, jt, path, asc, true) then
				local r = mmap.readAbyssJewelLUT(seed, sid, jt, path, asc, false); if r ~= nil then return r end
			end
			return orig_a and orig_a(seed, sid, jt, path, asc) or {}
		end
	end
	local function restore()
		if data then data.readLUT, data.readAbyssJewelLUT = orig_l, orig_a end
		M.installed = false
	end
	M.installed, M.restore = true, restore
	return true, restore
end

return M
