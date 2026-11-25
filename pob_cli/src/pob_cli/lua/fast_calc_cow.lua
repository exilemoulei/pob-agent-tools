-- Lua CoW snapshot/restore (C2c); direct_calc (C1/C3) lives in fast_calc_direct.lua.
local H = require("pob_host")
local M = {}
local t_insert, t_remove = table.insert, table.remove
-- Callers drop their snapshots, but a skipped drop (error path, killed client) must not pin
-- whole item/tree copies for the process lifetime: keep only the most recently used few.
local MAX_SNAPS = 16
local snapshots, snap_order, next_seq = {}, {}, 0

-- Snapshots read and write the build as it stands, and direct_calc runs its own calculation
-- pass, so neither settles a pending rebuild first.
local function get_build()
	return H.get_build({ rebuild = false })
end

local function shallow_copy(t)
	local out = {}
	if type(t) == "table" then for k, v in pairs(t) do out[k] = v end end
	return out
end

local function copy_array(a)
	local out = {}
	if type(a) == "table" then for i = 1, #a do out[i] = a[i] end end
	return out
end

local function replace_arr(dst, src)
	if type(dst) ~= "table" or type(src) ~= "table" then return end
	for i = 1, #src do dst[i] = src[i] end
	while #dst > #src do t_remove(dst) end
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

-- Plain fields only: derived sub-tables (gemData, displayEffect, ...) are rebuilt by PoB.
local function prims_eq(a, b)
	for k, v in pairs(a) do if type(v) ~= "table" and b[k] ~= v then return false end end
	for k, v in pairs(b) do if type(v) ~= "table" and a[k] ~= v then return false end end
	return true
end

local function replace_tbl(dst, src)
	if type(dst) == "table" then
		for k in pairs(dst) do if src[k] == nil then dst[k] = nil end end
		for k, v in pairs(src) do dst[k] = v end
	end
end

local function copy_sg(sg)
	local out, gl = shallow_copy(sg), {}
	if type(sg.gemList) == "table" then for i = 1, #sg.gemList do gl[i] = shallow_copy(sg.gemList[i]) end end
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

local PARTY_SPEC = {
	{ "editPartyMemberStats", "modDB", "PartyMemberStats", "output", nil, "PartyMemberStats" },
	{ "editAuras", "Aura", "Aura", nil, "simpleAuras", "Aura" },
	{ "editCurses", "Curse", "Curse", nil, "simpleCurses", "Curse" },
	{ "editWarcries", "Warcry", "Warcry", nil, "simpleWarcries", "Warcry" },
	{ "editLinks", "Link", "Link", nil, "simpleLinks", "Link" },
	{ "enemyCond", nil, "EnemyConditions", nil, nil, "EnemyCond" },
	{ "enemyMods", nil, "EnemyMods", nil, "simpleEnemyMods", "EnemyMods" },
}

local function snap_party(pt)
	if type(pt) ~= "table" then return nil end
	local c, bufs = pt.controls or {}, {}
	for _, s in ipairs(PARTY_SPEC) do
		local ctrl = c[s[1]]
		bufs[s[1]] = (ctrl and type(ctrl.buf) == "string") and ctrl.buf or ""
	end
	return { ref = pt, exp = pt.enableExportBuffs and true or false, bufs = bufs }
end

local function restore_party(b, sp)
	local pt = b.partyTab
	if not sp or type(pt) ~= "table" then return false end
	local c = pt.controls or {}
	local dirty = (pt ~= sp.ref) or ((pt.enableExportBuffs and true or false) ~= sp.exp)
	if not dirty then
		for _, s in ipairs(PARTY_SPEC) do
			local ctrl = c[s[1]]
			if ((ctrl and type(ctrl.buf) == "string") and ctrl.buf or "") ~= sp.bufs[s[1]] then
				dirty = true; break
			end
		end
	end
	if not dirty then return false end
	pt.enableExportBuffs = sp.exp
	if type(pt.lastContent) == "table" then pt.lastContent.EnableExportBuffs = sp.exp end
	if pt.enemyModList and type(wipeTable) == "function" then wipeTable(pt.enemyModList) end
	for _, s in ipairs(PARTY_SPEC) do
		local val, ctrl = sp.bufs[s[1]] or "", c[s[1]]
		if ctrl and type(ctrl.SetText) == "function" then ctrl:SetText(val) end
		if type(pt.lastContent) == "table" then pt.lastContent[s[6]] = val end
		if type(pt.ParseBuffs) == "function" then
			local lbl = (s[4] and pt.actor and pt.actor[s[4]]) or (s[5] and c[s[5]]) or nil
			if s[2] == nil and pt.enemyModList then
				pt:ParseBuffs(pt.enemyModList, val, s[3], lbl)
			elseif s[2] and pt.actor and pt.actor[s[2]] then
				if s[2] ~= "modDB" and type(wipeTable) == "function" then wipeTable(pt.actor[s[2]]) end
				pt:ParseBuffs(pt.actor[s[2]], val, s[3], lbl)
			end
		end
	end
	return true
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
	if type(itab) ~= "table" then return false, false end
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
	for sn, sl in pairs(itab.slots) do
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
	if not dirty then return false, false end

	for id in pairs(itab.items) do if snap.itemsRef[id] == nil then itab.items[id] = nil end end
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

local function sync_alloc_nodes(spec, snap_alloc)
	local an = spec.allocNodes
	for nid in pairs(an) do
		if not snap_alloc[nid] then
			an[nid] = nil
			if spec.nodes[nid] then spec.nodes[nid].alloc = false end
		end
	end
	for nid in pairs(snap_alloc) do
		local nd = spec.nodes[nid]
		if nd then an[nid] = nd; nd.alloc = true end
	end
end

local function rebuild_cluster_graphs(spec, snap)
	if type(spec.BuildClusterJewelGraphs) ~= "function" then
		replace_tbl(spec.jewels, snap.jewels); sync_alloc_nodes(spec, snap.allocNodes); return
	end
	for _ = 1, 4 do
		replace_tbl(spec.jewels, snap.jewels)
		sync_alloc_nodes(spec, snap.allocNodes)
		spec:BuildClusterJewelGraphs()
		sync_alloc_nodes(spec, snap.allocNodes)
		if keys_eq(spec.allocNodes, snap.allocNodes) and keys_eq(spec.subGraphs or {}, snap.subKeys) then return end
	end
	if not keys_eq(spec.allocNodes, snap.allocNodes) then error("Cluster jewel graph restore incomplete", 0) end
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
		if not tables_eq(spec.jewels, snap.jewels) or not keys_eq(spec.subGraphs or {}, snap.subKeys) then
			j_dirty = true
		elseif spec.tree and type(spec.tree.sockets) == "table" then
			for sid in pairs(spec.tree.sockets) do
				if (spec.allocNodes[sid] ~= nil) ~= (snap.allocNodes[sid] ~= nil) then j_dirty = true; break end
			end
		end
	end

	local dirty = spec_rep or cls_dirty or j_dirty
		or (not keys_eq(spec.allocNodes, snap.allocNodes))
		or (not tables_eq(spec.masterySelections or {}, snap.masterySel))
		or (not tables_eq(spec.hashOverrides or {}, snap.hashOvr))
	if not dirty then return false end

	if spec_rep or cls_dirty then
		spec:ImportFromNodeList(
			nil, snap.classId, snap.ascId, snap.secAscId,
			copy_array(snap.allocIds), shallow_copy(snap.hashOvr),
			shallow_copy(snap.masterySel), snap.treeVer
		)
		rebuild_cluster_graphs(spec, snap)
	else
		replace_tbl(spec.masterySelections, snap.masterySel)
		replace_tbl(spec.hashOverrides, snap.hashOvr)
		if j_dirty then rebuild_cluster_graphs(spec, snap) else sync_alloc_nodes(spec, snap.allocNodes) end
		spec.allocatedNotableCount, spec.allocatedKeystoneCount = snap.notableCnt, snap.keystoneCnt
		spec.allocatedMasteryCount, spec.allocatedMasteryTypeCount = snap.masteryCnt, snap.masteryTypeCnt
		replace_tbl(spec.allocatedMasteryTypes, snap.masteryTypes)
		replace_tbl(spec.allocatedTattooTypes, snap.tattooTypes)
	end
	replace_arr(spec.allocExtendedNodes, snap.allocExt)
	if type(spec.BuildAllDependsAndPaths) == "function" then spec:BuildAllDependsAndPaths() end
	if b.itemsTab and type(b.itemsTab.UpdateSockets) == "function" then b.itemsTab:UpdateSockets() end
	return true
end

-- Every plain field counts: set_build_field writes skillPart, skillStageCount, skillMinion*, ...
-- on the gem tables and mainActiveSkill* on the group, and a fixed key list misses new ones.
local function skills_differ(b, snap)
	local st = b.skillsTab
	if b.mainSocketGroup ~= snap.mainSG then return true end
	if (b.calcsTab and b.calcsTab.input and b.calcsTab.input.skill_number) ~= snap.calcsSkillNum then return true end
	local cur = st.socketGroupList or {}
	if #cur ~= #snap.groups then return true end
	for gi = 1, #snap.groups do
		local sg, ssg = cur[gi], snap.groups[gi]
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
	if st.skillSets and st.activeSkillSetId and st.skillSets[st.activeSkillSetId] then st.skillSets[st.activeSkillSetId].socketGroupList = list end
	b.mainSocketGroup = snap.mainSG
	if b.calcsTab and b.calcsTab.input then b.calcsTab.input.skill_number = snap.calcsSkillNum end
	if type(st.UpdateSocketGroups) == "function" then st:UpdateSocketGroups()
	elseif type(st.ProcessSocketGroup) == "function" then for i = 1, #list do st:ProcessSocketGroup(list[i]) end end
	return true
end

local function cmods_differ(cur, saved)
	if cur == saved then return false end
	if type(cur) ~= "table" or type(saved) ~= "table" or #cur ~= #saved then return true end
	for i = 1, #saved do
		if cur[i].title ~= saved[i].title or cur[i].enabled ~= saved[i].enabled or cur[i].text ~= saved[i].text then return true end
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

-- Restore only rewrites the active spec and sets of the build it was taken from; anything
-- else (a new build, a switched or added set) makes the snapshot unusable, never mis-restored.
local function set_sig(b)
	local tt, it, st, ct = b.treeTab or {}, b.itemsTab or {}, b.skillsTab or {}, b.configTab or {}
	return table.concat({ tostring(tt.activeSpec), #(tt.specList or {}), tostring(it.activeItemSetId), #(it.itemSetOrderList or {}),
		tostring(st.activeSkillSetId), #(st.skillSetOrderList or {}), tostring(ct.activeConfigSetId), #(ct.configSetOrderList or {}) }, ",")
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
	local a_ids, sub_k, sl_s, sl_a, st_s, st_a, j_ids, i_ref, i_raw, grps = {}, {}, {}, {}, {}, {}, {}, {}, {}, {}
	for nid in pairs(sp.allocNodes) do t_insert(a_ids, nid) end
	for k in pairs(sp.subGraphs or {}) do sub_k[k] = true end
	for sn, sl in pairs(it.slots) do
		sl_s[sn], sl_a[sn] = sl.selItemId, sl.active
		if sl.nodeId ~= nil and sl.selItemId and sl.selItemId ~= 0 then j_ids[sl.selItemId] = true end
	end
	for sn, ss in pairs(it.activeItemSet or {}) do
		if type(ss) == "table" and ss.selItemId ~= nil then st_s[sn], st_a[sn] = ss.selItemId, ss.active end
	end
	for _, iid in pairs(sp.jewels or {}) do if iid and iid ~= 0 then j_ids[iid] = true end end
	for id, o in pairs(it.items) do i_ref[id], i_raw[id] = o, type(o) == "table" and o.raw or nil end
	for gi, sg in ipairs(st.socketGroupList or {}) do grps[gi] = copy_sg(sg) end
	next_seq = next_seq + 1
	local sid = "cow_" .. next_seq
	remember(sid, {
		build = b, treeTabRef = b.treeTab, specRef = sp, itemsTabRef = it, skillsTabRef = st, configTabRef = ct, sets = set_sig(b),
		charLvl = b.characterLevel, charLvlAuto = b.characterLevelAutoMode, bandit = b.bandit, pMajor = b.pantheonMajorGod, pMinor = b.pantheonMinorGod,
		mainSG = b.mainSocketGroup, calcsSkillNum = b.calcsTab and b.calcsTab.input and b.calcsTab.input.skill_number,
		classId = sp.curClassId, ascId = sp.curAscendClassId, secAscId = sp.curSecondaryAscendClassId or sp.secondaryAscendClassId or 0, treeVer = sp.treeVersion,
		allocNodes = shallow_copy(sp.allocNodes), allocIds = a_ids, allocExt = copy_array(sp.allocExtendedNodes),
		jewels = shallow_copy(sp.jewels), subKeys = sub_k, notableCnt = sp.allocatedNotableCount, keystoneCnt = sp.allocatedKeystoneCount,
		masteryCnt = sp.allocatedMasteryCount, masteryTypeCnt = sp.allocatedMasteryTypeCount,
		masteryTypes = shallow_copy(sp.allocatedMasteryTypes), tattooTypes = shallow_copy(sp.allocatedTattooTypes),
		masterySel = shallow_copy(sp.masterySelections), hashOvr = shallow_copy(sp.hashOverrides),
		slotSel = sl_s, slotAct = sl_a, setSel = st_s, setAct = st_a, weaponSwap = (it.activeItemSet or {}).useSecondWeaponSet and true or false,
		jewelIds = j_ids, itemsRef = i_ref, itemRaw = i_raw, itemOrderList = copy_array(it.itemOrderList),
		groups = grps, cfgIn = shallow_copy((cs and cs.input) or ct.input), cmods = clone_cmods(cs and cs.customModsList), party = snap_party(b.partyTab),
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
	local stale = (b ~= snap.build or b.treeTab ~= snap.treeTabRef or b.itemsTab ~= snap.itemsTabRef or b.skillsTab ~= snap.skillsTabRef
		or b.configTab ~= snap.configTabRef) and "the build was replaced" or (set_sig(b) ~= snap.sets and "the active tree, item, skill or config set changed")
	if stale then forget(sid); error("Stale cow snapshot " .. sid .. ": " .. stale .. " since it was taken", 0) end
	local sc_d = b.characterLevel ~= snap.charLvl or b.characterLevelAutoMode ~= snap.charLvlAuto or b.bandit ~= snap.bandit or b.pantheonMajorGod ~= snap.pMajor or b.pantheonMinorGod ~= snap.pMinor
	if sc_d then
		b.characterLevel, b.characterLevelAutoMode, b.bandit, b.pantheonMajorGod, b.pantheonMinorGod = snap.charLvl, snap.charLvlAuto, snap.bandit, snap.pMajor, snap.pMinor
		local cl = b.controls and b.controls.characterLevel
		if cl and type(cl.SetText) == "function" then cl:SetText(tostring(snap.charLvl)) end
	end
	local it_d, j_d, tb_d = restore_items(b, snap)
	-- The weapon swap flag lives on the item set, not in a slot, so restore_items never sees it.
	local aset = b.itemsTab and b.itemsTab.activeItemSet
	local sw_d = type(aset) == "table" and (aset.useSecondWeaponSet and true or false) ~= snap.weaponSwap
	if sw_d then aset.useSecondWeaponSet = snap.weaponSwap end
	local sp_d = restore_spec(b, snap, j_d)
	local it, sp = b.itemsTab, b.spec
	if (it_d or tb_d or j_d or sp_d) and type(it) == "table" then
		if type(it.PopulateSlots) == "function" then it:PopulateSlots() end
		local pj = sync_slots(it, snap)
		if type(sp) == "table" and (pj or not tables_eq(sp.jewels, snap.jewels) or not keys_eq(sp.subGraphs or {}, snap.subKeys) or not keys_eq(sp.allocNodes, snap.allocNodes)) then
			rebuild_cluster_graphs(sp, snap)
			if type(sp.BuildAllDependsAndPaths) == "function" then sp:BuildAllDependsAndPaths() end
			if type(it.PopulateSlots) == "function" then it:PopulateSlots() end
			sync_slots(it, snap)
		end
		if type(it.UpdateSockets) == "function" then it:UpdateSockets() end
	end
	local sk_d, cf_d, pt_d = restore_skills(b, snap), restore_config(b, snap), restore_party(b, snap.party)
	-- The config mod list bakes in the character level (set_build_field "level" rebuilds it too).
	if sc_d and not cf_d and type(b.configTab.BuildModList) == "function" then b.configTab:BuildModList() end
	if sc_d or sw_d or it_d or sp_d or sk_d or cf_d or pt_d then
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

M.rpc_handlers = {
	cow_snapshot = rpc_cow_snapshot,
	cow_restore = rpc_cow_restore,
	cow_drop = rpc_cow_drop,
}

-- Loaded from beside this file because headless_runner only loads fast_calc_cow.lua.
do
	local dir = ((debug.getinfo(1, "S") or {}).source or ""):match("^@(.*[/\\])") or ""
	local chunk = loadfile(dir .. "fast_calc_direct.lua")
	local ok, mk = false, nil
	if chunk then ok, mk = pcall(chunk) end
	if ok and type(mk) == "function" then M.rpc_handlers.direct_calc = mk(get_build) end
end

return M
