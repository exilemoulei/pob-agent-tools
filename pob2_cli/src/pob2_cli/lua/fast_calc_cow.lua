-- PoE2 Lua CoW snapshot/restore kernel:
--   * Node-level dual weapon-set allocMode (0 | 1 | 2) and spec.allocMode / spec.attributeIndex
--   * Travel attribute node (+5 Str/Dex/Int) hashOverrides + spec.nodes[nid] restoration
--   * Per-skill-group set1 / set2 / forcedBoth (#2498 weaponSetEnvs & weaponSetValidityCache)
--     plus gem-level enableGlobal1 / enableGlobal2
--   * PoE2 items, Flask 1..2, Charm 1..3, Weapon 1/2 & Swap, activeItemSet.useSecondWeaponSet
local H = require("pob_host")
local M = {}
local t_insert, t_remove = table.insert, table.remove

local MAX_SNAPS = 16
local snapshots, snap_order, next_seq = {}, {}, 0

local function get_build()
	return H.get_build({ rebuild = false })
end

local function shallow_copy(t)
	local out = {}
	if type(t) == "table" then
		for k, v in pairs(t) do out[k] = v end
	end
	return out
end

local function copy_array(a)
	local out = {}
	if type(a) == "table" then
		for i = 1, #a do out[i] = a[i] end
	end
	return out
end

local function replace_arr(dst, src)
	if type(dst) ~= "table" or type(src) ~= "table" then return end
	for i = 1, #src do dst[i] = src[i] end
	while #dst > #src do t_remove(dst) end
end

local function replace_tbl(dst, src)
	if type(dst) ~= "table" then return end
	for k in pairs(dst) do
		if src[k] == nil then dst[k] = nil end
	end
	for k, v in pairs(src) do dst[k] = v end
end

local function tables_eq(a, b)
	if a == b then return true end
	if type(a) ~= "table" or type(b) ~= "table" then return false end
	for k, v in pairs(a) do if b[k] ~= v then return false end end
	for k in pairs(b) do if a[k] == nil then return false end end
	return true
end

local function keys_eq(a, b)
	if a == b then return true end
	if type(a) ~= "table" or type(b) ~= "table" then return false end
	for k in pairs(a) do if b[k] == nil then return false end end
	for k in pairs(b) do if a[k] == nil then return false end end
	return true
end

local function prims_eq(a, b)
	for k, v in pairs(a) do if type(v) ~= "table" and b[k] ~= v then return false end end
	for k, v in pairs(b) do if type(v) ~= "table" and a[k] ~= v then return false end end
	return true
end

local function copy_sg(sg)
	local out, gl = shallow_copy(sg), {}
	if type(sg.gemList) == "table" then
		for i = 1, #sg.gemList do gl[i] = shallow_copy(sg.gemList[i]) end
	end
	out.gemList = gl
	return out
end

local function clone_cmods(list)
	if type(list) ~= "table" then return nil end
	local out = {}
	for i = 1, #list do
		out[i] = { title = list[i].title, enabled = list[i].enabled, text = list[i].text }
	end
	return out
end

local function override_sig(nd)
	if type(nd) ~= "table" then return "" end
	local sd = type(nd.sd) == "table" and table.concat(nd.sd, "\n") or ""
	return tostring(nd.dn or "") .. "|" .. sd
end

local function sync_slots(itab, snap)
	local aset, j_chg = itab.activeItemSet, false
	for sn, sl in pairs(itab.slots or {}) do
		local w = snap.slotSel[sn]
		if w ~= nil and sl.selItemId ~= w then
			sl.selItemId = w
			if sl.nodeId ~= nil then j_chg = true end
		elseif w == nil then
			sl.selItemId = 0
		end
		sl.active = snap.slotAct[sn]
	end
	if type(aset) == "table" then
		for sn, ss in pairs(aset) do
			if type(ss) == "table" and ss.selItemId ~= nil and snap.setSel[sn] == nil then aset[sn] = nil end
		end
		for sn, sel in pairs(snap.setSel) do
			local ss = aset[sn]
			if type(ss) ~= "table" then ss = {}; aset[sn] = ss end
			ss.selItemId, ss.active = sel, snap.setAct[sn]
		end
	end
	return j_chg
end

local function restore_items(b, snap)
	local itab = b.itemsTab
	if type(itab) ~= "table" then return false, false, false end
	local aset = itab.activeItemSet or {}
	local dirty, tbl_dirty, j_dirty = false, false, false

	for id in pairs(itab.items) do
		if snap.itemsRef[id] == nil then dirty, tbl_dirty = true, true; break end
	end
	if not tbl_dirty then
		for id, orig in pairs(snap.itemsRef) do
			if itab.items[id] ~= orig or (type(orig) == "table" and orig.raw ~= snap.itemRaw[id]) then
				dirty, tbl_dirty = true, true
				if snap.jewelIds[id] then j_dirty = true end
				break
			end
		end
	end
	if not dirty and not tables_eq(itab.itemOrderList, snap.itemOrderList) then
		dirty, tbl_dirty = true, true
	end
	for sn, sl in pairs(itab.slots or {}) do
		local ss = aset[sn]
		if sl.selItemId ~= snap.slotSel[sn] or sl.active ~= snap.slotAct[sn]
			or (ss and (ss.selItemId ~= snap.setSel[sn] or ss.active ~= snap.setAct[sn])) then
			dirty = true
			if sl.nodeId ~= nil then j_dirty = true end
		end
	end
	for sn in pairs(snap.slotSel) do
		if itab.slots[sn] == nil then dirty, j_dirty = true, true; break end
	end
	if not dirty then
		for sn, sel in pairs(snap.setSel) do
			local ss = aset[sn]
			if type(ss) ~= "table" or ss.selItemId ~= sel or ss.active ~= snap.setAct[sn] then dirty = true; break end
		end
	end
	if not dirty then return false, false, false end

	for id in pairs(itab.items) do
		if snap.itemsRef[id] == nil then itab.items[id] = nil end
	end
	for id, orig in pairs(snap.itemsRef) do
		if type(orig) == "table" and orig.raw ~= snap.itemRaw[id] and type(snap.itemRaw[id]) == "string" then
			local rb = new("Item"):Item(snap.itemRaw[id])
			rb.id = id; rb:BuildModList()
			itab.items[id], snap.itemsRef[id] = rb, rb
			if snap.jewelIds[id] then j_dirty = true end
		elseif itab.items[id] ~= orig then
			itab.items[id] = orig
		end
	end
	replace_arr(itab.itemOrderList, snap.itemOrderList)
	sync_slots(itab, snap)
	return true, j_dirty, tbl_dirty
end

local function node_modes_differ(spec, snap)
	if (spec.allocMode or 0) ~= snap.allocMode then return true end
	if (spec.attributeIndex or 1) ~= snap.attrIndex then return true end
	for nid, nd in pairs(spec.allocNodes or {}) do
		if (nd.allocMode or 0) ~= (snap.nodeModes[nid] or 0) then return true end
	end
	return false
end

local function overrides_differ(spec, snap)
	local cur_ov = spec.hashOverrides or {}
	if not keys_eq(cur_ov, snap.hashOvr) then return true end
	for nid, saved_nd in pairs(snap.hashOvr) do
		local cur_nd = cur_ov[nid]
		if cur_nd ~= saved_nd or override_sig(cur_nd) ~= snap.hashOvrSig[nid] then return true end
		local live = spec.nodes and spec.nodes[nid]
		if live and override_sig(live) ~= snap.hashOvrSig[nid] then return true end
	end
	for nid, base_sig in pairs(snap.allocAttrSig) do
		local live = spec.nodes and spec.nodes[nid]
		if live and override_sig(live) ~= base_sig then return true end
	end
	return false
end

local function restore_hash_overrides(spec, snap)
	local touched = {}
	for nid in pairs(spec.hashOverrides or {}) do touched[nid] = true end
	for nid in pairs(snap.hashOvr) do touched[nid] = true end
	for nid in pairs(snap.allocAttrSig) do touched[nid] = true end

	if type(spec.hashOverrides) ~= "table" then spec.hashOverrides = {} end
	replace_tbl(spec.hashOverrides, snap.hashOvr)

	local tree = spec.tree
	for nid in pairs(touched) do
		local live = spec.nodes and spec.nodes[nid]
		if live then
			local target = snap.hashOvr[nid] or (tree and tree.nodes and tree.nodes[nid])
			if target and type(spec.ReplaceNode) == "function" then
				spec:ReplaceNode(live, target)
				if tree and type(tree.ProcessStats) == "function" then
					pcall(tree.ProcessStats, tree, live)
				end
				live.finalModList = live.modList
			end
		end
	end
end

local function sync_alloc_nodes_poe2(spec, snap)
	local an = spec.allocNodes
	for nid in pairs(an) do
		if not snap.allocNodes[nid] then
			an[nid] = nil
			local nd = spec.nodes[nid]
			if nd then nd.alloc = false; nd.allocMode = 0 end
		end
	end
	for nid in pairs(snap.allocNodes) do
		local nd = spec.nodes[nid]
		if nd then
			an[nid] = nd
			nd.alloc = true
			nd.allocMode = snap.nodeModes[nid] or 0
		end
	end
	spec.allocMode = snap.allocMode
	spec.attributeIndex = snap.attrIndex
end

local function restore_spec(b, snap, j_dirty)
	local spec = b.spec
	if type(spec) ~= "table" then return false end

	local cur_sec = spec.curSecondaryAscendClassId or spec.secondaryAscendClassId or 0
	local spec_rep = (spec ~= snap.specRef)
	local cls_dirty = (spec.curClassId ~= snap.classId)
		or (spec.curAscendClassId ~= snap.ascId)
		or (cur_sec ~= snap.secAscId)
		or (spec.treeVersion ~= snap.treeVer)

	if not j_dirty then
		if not tables_eq(spec.jewels or {}, snap.jewels) then
			j_dirty = true
		elseif spec.tree and type(spec.tree.sockets) == "table" then
			for sid in pairs(spec.tree.sockets) do
				if (spec.allocNodes[sid] ~= nil) ~= (snap.allocNodes[sid] ~= nil) then j_dirty = true; break end
			end
		end
	end

	local mode_dirty = node_modes_differ(spec, snap)
	local ovr_dirty = overrides_differ(spec, snap)
	local alloc_dirty = not keys_eq(spec.allocNodes, snap.allocNodes)
	local dirty = spec_rep or cls_dirty or j_dirty or mode_dirty or ovr_dirty or alloc_dirty
		or (not tables_eq(spec.masterySelections or {}, snap.masterySel))
	if not dirty then return false end

	if spec_rep or cls_dirty then
		local ws_map = {}
		for nid, mode in pairs(snap.nodeModes) do
			if mode and mode ~= 0 then ws_map[nid] = mode end
		end
		-- PoE2 PassiveSpec:ImportFromNodeList takes weaponSets as 6th arg before hashOverrides!
		spec:ImportFromNodeList(
			nil, snap.classId, snap.ascId, snap.secAscId,
			copy_array(snap.allocIds), ws_map, shallow_copy(snap.hashOvr),
			shallow_copy(snap.masterySel), snap.treeVer
		)
		spec.allocMode = snap.allocMode
		spec.attributeIndex = snap.attrIndex
		replace_tbl(spec.jewels, snap.jewels)
	else
		if type(spec.masterySelections) == "table" then replace_tbl(spec.masterySelections, snap.masterySel) end
		if type(spec.jewels) == "table" then replace_tbl(spec.jewels, snap.jewels) end
		if ovr_dirty then restore_hash_overrides(spec, snap) end
		sync_alloc_nodes_poe2(spec, snap)
		spec.allocatedNotableCount = snap.notableCnt
		spec.allocatedKeystoneCount = snap.keystoneCnt
	end
	if type(spec.allocExtendedNodes) == "table" then replace_arr(spec.allocExtendedNodes, snap.allocExt) end
	if type(spec.BuildAllDependsAndPaths) == "function" then spec:BuildAllDependsAndPaths() end
	if b.itemsTab and type(b.itemsTab.UpdateSockets) == "function" then b.itemsTab:UpdateSockets() end
	return true
end

local function skills_differ(b, snap)
	local st = b.skillsTab
	if b.mainSocketGroup ~= snap.mainSG then return true end
	if (b.calcsTab and b.calcsTab.input and b.calcsTab.input.skill_number) ~= snap.calcsSkillNum then return true end
	local cur = st.socketGroupList or {}
	if #cur ~= #snap.groups then return true end
	for gi = 1, #snap.groups do
		local sg, ssg = cur[gi], snap.groups[gi]
		if sg.set1 ~= ssg.set1 or sg.set2 ~= ssg.set2 or sg.forcedBoth ~= ssg.forcedBoth then return true end
		if not prims_eq(sg, ssg) then return true end
		local cgl, sgl = sg.gemList or {}, ssg.gemList or {}
		if #cgl ~= #sgl then return true end
		for ji = 1, #sgl do
			if not prims_eq(cgl[ji], sgl[ji]) then return true end
		end
	end
	return false
end

local function restore_skills(b, snap)
	local st = b.skillsTab
	if type(st) ~= "table" or not skills_differ(b, snap) then return false end
	local list = st.socketGroupList
	for i = 1, #snap.groups do list[i] = copy_sg(snap.groups[i]) end
	while #list > #snap.groups do t_remove(list) end
	if st.skillSets and st.activeSkillSetId and st.skillSets[st.activeSkillSetId] then
		st.skillSets[st.activeSkillSetId].socketGroupList = list
	end
	-- Invalidate PoE2 0.5.5+ #2498 per-skill weapon-set validity cache & weaponSetEnvs
	if type(st.weaponSetValidityCache) == "table" then st.weaponSetValidityCache = {} end
	b.mainSocketGroup = snap.mainSG
	if b.calcsTab and b.calcsTab.input then b.calcsTab.input.skill_number = snap.calcsSkillNum end
	if b.calcsTab and type(b.calcsTab.mainEnv) == "table" then
		b.calcsTab.mainEnv.weaponSetEnvs = nil
	end
	if type(st.ReconcileSocketGroupWeaponSets) == "function" then
		pcall(st.ReconcileSocketGroupWeaponSets, st)
	end
	if type(st.UpdateSocketGroups) == "function" then
		st:UpdateSocketGroups()
	elseif type(st.ProcessSocketGroup) == "function" then
		for i = 1, #list do st:ProcessSocketGroup(list[i]) end
	end
	return true
end

local function cmods_differ(cur, saved)
	if cur == saved then return false end
	if type(cur) ~= "table" or type(saved) ~= "table" or #cur ~= #saved then return true end
	for i = 1, #saved do
		if cur[i].title ~= saved[i].title or cur[i].enabled ~= saved[i].enabled or cur[i].text ~= saved[i].text then
			return true
		end
	end
	return false
end

local function restore_config(b, snap)
	local ct = b.configTab
	if type(ct) ~= "table" then return false end
	local cs = ct.configSets and ct.activeConfigSetId and ct.configSets[ct.activeConfigSetId]
	local ci = (cs and cs.input) or ct.input
	if tables_eq(ci, snap.cfgIn) and not cmods_differ(cs and cs.customModsList, snap.cmods) then return false end
	replace_tbl(ci, snap.cfgIn)
	if ct.input and ct.input ~= ci then replace_tbl(ct.input, snap.cfgIn) end
	if cs and snap.cmods ~= nil then cs.customModsList = clone_cmods(snap.cmods) end
	if type(ct.UpdateControls) == "function" then ct:UpdateControls() end
	if type(ct.BuildModList) == "function" then ct:BuildModList() end
	return true
end

local function set_sig(b)
	local tt, it, st, ct = b.treeTab or {}, b.itemsTab or {}, b.skillsTab or {}, b.configTab or {}
	return table.concat({
		tostring(tt.activeSpec), #(tt.specList or {}),
		tostring(it.activeItemSetId), #(it.itemSetOrderList or {}),
		tostring(st.activeSkillSetId), #(st.skillSetOrderList or {}),
		tostring(ct.activeConfigSetId), #(ct.configSetOrderList or {}),
	}, ",")
end

local function forget(sid)
	snapshots[sid] = nil
	for i = #snap_order, 1, -1 do if snap_order[i] == sid then t_remove(snap_order, i) end end
end

local function remember(sid, snap)
	forget(sid)
	snapshots[sid] = snap
	t_insert(snap_order, sid)
	while #snap_order > MAX_SNAPS do snapshots[t_remove(snap_order, 1)] = nil end
end

local function rpc_cow_snapshot(_)
	local b = get_build()
	local sp, it, st, ct = b.spec, b.itemsTab, b.skillsTab, b.configTab
	local cs = ct.configSets and ct.activeConfigSetId and ct.configSets[ct.activeConfigSetId]
	local a_ids, n_modes, attr_sigs = {}, {}, {}
	for nid, nd in pairs(sp.allocNodes or {}) do
		t_insert(a_ids, nid)
		n_modes[nid] = (type(nd) == "table" and nd.allocMode) or 0
		if type(nd) == "table" and nd.isAttribute then attr_sigs[nid] = override_sig(nd) end
	end
	local h_ovr, h_sig = {}, {}
	for nid, ov in pairs(sp.hashOverrides or {}) do
		h_ovr[nid] = ov
		h_sig[nid] = override_sig(ov)
	end
	local sl_s, sl_a, st_s, st_a, j_ids, i_ref, i_raw, grps = {}, {}, {}, {}, {}, {}, {}, {}
	for sn, sl in pairs(it.slots or {}) do
		sl_s[sn], sl_a[sn] = sl.selItemId, sl.active
		if sl.nodeId ~= nil and sl.selItemId and sl.selItemId ~= 0 then j_ids[sl.selItemId] = true end
	end
	for sn, ss in pairs(it.activeItemSet or {}) do
		if type(ss) == "table" and ss.selItemId ~= nil then st_s[sn], st_a[sn] = ss.selItemId, ss.active end
	end
	for _, iid in pairs(sp.jewels or {}) do if iid and iid ~= 0 then j_ids[iid] = true end end
	for id, o in pairs(it.items or {}) do i_ref[id], i_raw[id] = o, type(o) == "table" and o.raw or nil end
	for gi, sg in ipairs(st.socketGroupList or {}) do grps[gi] = copy_sg(sg) end
	next_seq = next_seq + 1
	local sid = "cow_" .. next_seq
	remember(sid, {
		build = b, treeTabRef = b.treeTab, specRef = sp, itemsTabRef = it, skillsTabRef = st, configTabRef = ct,
		sets = set_sig(b), charLvl = b.characterLevel, charLvlAuto = b.characterLevelAutoMode,
		mainSG = b.mainSocketGroup, calcsSkillNum = b.calcsTab and b.calcsTab.input and b.calcsTab.input.skill_number,
		classId = sp.curClassId, ascId = sp.curAscendClassId,
		secAscId = sp.curSecondaryAscendClassId or sp.secondaryAscendClassId or 0, treeVer = sp.treeVersion,
		allocMode = sp.allocMode or 0, attrIndex = sp.attributeIndex or 1,
		allocNodes = shallow_copy(sp.allocNodes), allocIds = a_ids, nodeModes = n_modes,
		allocExt = copy_array(sp.allocExtendedNodes), jewels = shallow_copy(sp.jewels),
		notableCnt = sp.allocatedNotableCount, keystoneCnt = sp.allocatedKeystoneCount,
		masterySel = shallow_copy(sp.masterySelections), hashOvr = h_ovr, hashOvrSig = h_sig, allocAttrSig = attr_sigs,
		slotSel = sl_s, slotAct = sl_a, setSel = st_s, setAct = st_a,
		weaponSwap = (it.activeItemSet or {}).useSecondWeaponSet and true or false,
		jewelIds = j_ids, itemsRef = i_ref, itemRaw = i_raw, itemOrderList = copy_array(it.itemOrderList),
		groups = grps, cfgIn = shallow_copy((cs and cs.input) or ct.input),
		cmods = clone_cmods(cs and cs.customModsList),
	})
	return { snapId = sid }
end

local function req_sid(p, fn)
	local sid = type(p) == "table" and p.snapId or nil
	if type(sid) ~= "string" or sid == "" then error(fn .. " requires non-empty snapId", 0) end
	return sid
end

local function rpc_cow_restore(params)
	local sid = req_sid(params, "cow_restore")
	local snap = snapshots[sid] or error("Unknown cow snapshot: " .. sid, 0)
	local b = get_build()
	local stale = (b ~= snap.build or b.treeTab ~= snap.treeTabRef or b.itemsTab ~= snap.itemsTabRef
		or b.skillsTab ~= snap.skillsTabRef or b.configTab ~= snap.configTabRef) and "the build was replaced"
		or (set_sig(b) ~= snap.sets and "the active tree, item, skill or config set changed")
	if stale then forget(sid); error("Stale cow snapshot " .. sid .. ": " .. stale .. " since it was taken", 0) end

	local sc_d = b.characterLevel ~= snap.charLvl or b.characterLevelAutoMode ~= snap.charLvlAuto
	if sc_d then
		b.characterLevel, b.characterLevelAutoMode = snap.charLvl, snap.charLvlAuto
		local cl = b.controls and b.controls.characterLevel
		if cl and type(cl.SetText) == "function" then cl:SetText(tostring(snap.charLvl)) end
	end
	local it_d, j_d, tb_d = restore_items(b, snap)
	local aset = b.itemsTab and b.itemsTab.activeItemSet
	local sw_d = type(aset) == "table" and (aset.useSecondWeaponSet and true or false) ~= snap.weaponSwap
	if sw_d then aset.useSecondWeaponSet = snap.weaponSwap end
	local sp_d = restore_spec(b, snap, j_d)
	local it = b.itemsTab
	if (it_d or tb_d or j_d or sp_d) and type(it) == "table" then
		if type(it.PopulateSlots) == "function" then it:PopulateSlots() end
		sync_slots(it, snap)
		if type(it.UpdateSockets) == "function" then it:UpdateSockets() end
	end
	local sk_d, cf_d = restore_skills(b, snap), restore_config(b, snap)
	if sc_d and not cf_d and b.configTab and type(b.configTab.BuildModList) == "function" then
		b.configTab:BuildModList()
	end
	if sc_d or sw_d or it_d or sp_d or sk_d or cf_d then
		if b.calcsTab and type(b.calcsTab.mainEnv) == "table" then b.calcsTab.mainEnv.weaponSetEnvs = nil end
		if type(wipeGlobalCache) == "function" then wipeGlobalCache() end
		b.buildFlag = true
		if params.rebuild ~= false then H.frame() end
	end
	if params.drop == true then forget(sid) else remember(sid, snap) end
	return { ok = true }
end

local function rpc_cow_drop(params)
	forget(req_sid(params, "cow_drop"))
	return { ok = true }
end

-- PoE2 0.5.5+ (#2498) per-skill weapon-set binding mutator & state reader (works on both 0.23.1 & 0.5.5+ dev).
local function rpc_set_skill_weapon_set(params)
	params = type(params) == "table" and params or {}
	local b = H.get_build({ rebuild = false })
	local st = b.skillsTab
	local idx = tonumber(params.index or b.mainSocketGroup or 1)
	local sg = st and st.socketGroupList and st.socketGroupList[idx]
	if not sg then error("no socket group at index " .. tostring(idx), 0) end
	if params.set1 ~= nil then sg.set1 = params.set1 and true or false end
	if params.set2 ~= nil then sg.set2 = params.set2 and true or false end
	if params.forcedBoth ~= nil then sg.forcedBoth = params.forcedBoth and true or false end
	if (params.enableGlobal1 ~= nil or params.enableGlobal2 ~= nil) and type(sg.gemList) == "table" then
		local gi = tonumber(params.gemIndex or 1)
		local gem = sg.gemList[gi]
		if gem then
			if params.enableGlobal1 ~= nil then gem.enableGlobal1 = params.enableGlobal1 and true or false end
			if params.enableGlobal2 ~= nil then gem.enableGlobal2 = params.enableGlobal2 and true or false end
		end
	end
	if type(st.weaponSetValidityCache) == "table" then st.weaponSetValidityCache = {} end
	if type(st.ReconcileSocketGroupWeaponSets) == "function" then pcall(st.ReconcileSocketGroupWeaponSets, st) end
	if type(st.ProcessSocketGroup) == "function" then st:ProcessSocketGroup(sg) end
	if b.calcsTab and type(b.calcsTab.mainEnv) == "table" then b.calcsTab.mainEnv.weaponSetEnvs = nil end
	b.buildFlag = true
	H.frame()
	return { ok = true, index = idx, set1 = sg.set1, set2 = sg.set2, forcedBoth = sg.forcedBoth }
end

local function rpc_get_weapon_set_state(_)
	local b = H.get_build({ rebuild = true })
	local sp, it, st = b.spec, b.itemsTab, b.skillsTab
	local node_modes, attr_overrides = H.json_obj({}), H.json_obj({})
	local ws1_cnt, ws2_cnt = 0, 0
	for nid, nd in pairs((sp and sp.allocNodes) or {}) do
		local m = (type(nd) == "table" and nd.allocMode) or 0
		node_modes[tostring(nid)] = m
		if m == 1 then ws1_cnt = ws1_cnt + 1 elseif m == 2 then ws2_cnt = ws2_cnt + 1 end
	end
	for nid, ov in pairs((sp and sp.hashOverrides) or {}) do
		attr_overrides[tostring(nid)] = {
			name = type(ov) == "table" and ov.dn or nil,
			sd = type(ov) == "table" and ov.sd or nil,
		}
	end
	local skill_sets = {}
	for i, sg in ipairs((st and st.socketGroupList) or {}) do
		local g1 = sg.gemList and sg.gemList[1]
		skill_sets[i] = {
			index = i, label = sg.label or "", enabled = sg.enabled and true or false,
			set1 = sg.set1, set2 = sg.set2, forcedBoth = sg.forcedBoth,
			gem1EnableGlobal1 = g1 and g1.enableGlobal1, gem1EnableGlobal2 = g1 and g1.enableGlobal2,
		}
	end
	local aset = it and it.activeItemSet
	return {
		ok = true, game = "poe2",
		allocMode = sp and (sp.allocMode or 0) or 0,
		attributeIndex = sp and (sp.attributeIndex or 1) or 1,
		useSecondWeaponSet = type(aset) == "table" and (aset.useSecondWeaponSet and true or false) or false,
		weaponSet1Used = ws1_cnt, weaponSet2Used = ws2_cnt,
		nodeModes = node_modes, hashOverrides = attr_overrides, skillWeaponSets = skill_sets,
	}
end

M.rpc_handlers = {
	cow_snapshot = rpc_cow_snapshot,
	cow_restore = rpc_cow_restore,
	cow_drop = rpc_cow_drop,
	set_skill_weapon_set = rpc_set_skill_weapon_set,
	get_weapon_set_state = rpc_get_weapon_set_state,
}

return M
