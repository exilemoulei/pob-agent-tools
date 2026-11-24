-- Single-pass direct calc (C1/C3) and Lua CoW snapshot/restore (C2c).
local M = {}
local t_insert, t_remove, m_huge = table.insert, table.remove, math.huge
local snapshots, next_seq = {}, 0

local function get_build()
	local m = _G.main or (__mainObject__ and (__mainObject__.modes and __mainObject__ or __mainObject__.main))
	local b = m and m.modes and m.modes["BUILD"]
	if not b or not b.calcsTab then error("No active PoB build loaded", 0) end
	return b
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

local function replace_tbl(dst, src)
	if type(dst) == "table" then
		for k in pairs(dst) do if src[k] == nil then dst[k] = nil end end
		for k, v in pairs(src) do dst[k] = v end
	end
end

local function sanitize(v)
	local vt = type(v)
	if vt == "number" then
		return (v ~= v and "nan") or (v == m_huge and "inf") or (v == -m_huge and "-inf") or v
	end
	return (vt == "boolean" or vt == "string") and v or nil
end

local function has_full_dps_skills(b)
	local list = b.skillsTab and b.skillsTab.socketGroupList
	if type(list) ~= "table" then return false end
	for i = 1, #list do
		local g = list[i]
		if g and g.enabled and g.includeInFullDPS then return true end
	end
	return false
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
	local dirty = (itab ~= snap.itemsTabRef) or (itab.activeItemSetId ~= snap.activeItemSetId)
	local tbl_dirty, j_dirty = (itab ~= snap.itemsTabRef), false

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

	if itab.activeItemSetId ~= snap.activeItemSetId and itab.itemSets and itab.itemSets[snap.activeItemSetId] then
		itab.activeItemSetId = snap.activeItemSetId
		itab.activeItemSet = itab.itemSets[snap.activeItemSetId]
	end
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
		if keys_eq(spec.allocNodes, snap.allocNodes) and keys_eq(spec.subGraphs or {}, snap.subKeys) then break end
	end
end

local function restore_spec(b, snap, j_dirty)
	local tt = b.treeTab
	if type(tt) == "table" and type(tt.specList) == "table" then
		while #tt.specList > snap.specListLen do t_remove(tt.specList) end
		if snap.activeSpec and tt.activeSpec ~= snap.activeSpec and type(tt.SetActiveSpec) == "function" then
			tt:SetActiveSpec(snap.activeSpec)
		end
	end
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

local SG_KEYS = { "label", "enabled", "includeInFullDPS", "slot", "mainActiveSkill", "mainActiveSkillCalcs", "groupCount", "source" }
local GEM_KEYS = { "nameSpec", "gemId", "variantId", "skillId", "level", "quality", "qualityId", "enabled", "count", "enableGlobal1", "enableGlobal2" }

local function skills_differ(b, snap)
	local st = b.skillsTab
	if st ~= snap.skillsTabRef or st.activeSkillSetId ~= snap.activeSkillSetId then return true end
	if b.mainSocketGroup ~= snap.mainSG then return true end
	if (b.calcsTab and b.calcsTab.input and b.calcsTab.input.skill_number) ~= snap.calcsSkillNum then return true end
	local cur = st.socketGroupList or {}
	if #cur ~= #snap.groups then return true end
	for gi = 1, #snap.groups do
		local sg, ssg = cur[gi], snap.groups[gi]
		for _, f in ipairs(SG_KEYS) do if sg[f] ~= ssg[f] then return true end end
		local cgl, sgl = sg.gemList or {}, ssg.gemList or {}
		if #cgl ~= #sgl then return true end
		for ji = 1, #sgl do
			for _, f in ipairs(GEM_KEYS) do if cgl[ji][f] ~= sgl[ji][f] then return true end end
		end
	end
	return false
end

local function restore_skills(b, snap)
	local st = b.skillsTab
	if type(st) ~= "table" or not skills_differ(b, snap) then return false end
	if st.activeSkillSetId ~= snap.activeSkillSetId and type(st.SetActiveSkillSet) == "function" then st:SetActiveSkillSet(snap.activeSkillSetId) end
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
	if ct == snap.configTabRef and ct.activeConfigSetId == snap.activeCfgId and tables_eq(ci, snap.cfgIn) and not cmods_differ(cs and cs.customModsList, snap.cmods) then return false end
	if ct.activeConfigSetId ~= snap.activeCfgId and type(ct.SetActiveConfigSet) == "function" then
		ct:SetActiveConfigSet(snap.activeCfgId)
		cs = ct.configSets and ct.activeConfigSetId and ct.configSets[ct.activeConfigSetId]
		ci = (cs and cs.input) or ct.input
	end
	replace_tbl(ci, snap.cfgIn)
	if ct.input and ct.input ~= ci then replace_tbl(ct.input, snap.cfgIn) end
	if cs and snap.cmods ~= nil then cs.customModsList = clone_cmods(snap.cmods) end
	if type(ct.UpdateControls) == "function" then ct:UpdateControls() end
	if type(ct.BuildModList) == "function" then ct:BuildModList() end
	return true
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
	snapshots[sid] = {
		specRef = sp, itemsTabRef = it, skillsTabRef = st, configTabRef = ct,
		charLvl = b.characterLevel, charLvlAuto = b.characterLevelAutoMode, bandit = b.bandit, pMajor = b.pantheonMajorGod, pMinor = b.pantheonMinorGod,
		mainSG = b.mainSocketGroup, calcsSkillNum = b.calcsTab and b.calcsTab.input and b.calcsTab.input.skill_number,
		activeSpec = b.treeTab and b.treeTab.activeSpec, specListLen = (b.treeTab and b.treeTab.specList) and #b.treeTab.specList or 0,
		classId = sp.curClassId, ascId = sp.curAscendClassId, secAscId = sp.curSecondaryAscendClassId or sp.secondaryAscendClassId or 0, treeVer = sp.treeVersion,
		allocNodes = shallow_copy(sp.allocNodes), allocIds = a_ids, allocExt = copy_array(sp.allocExtendedNodes),
		jewels = shallow_copy(sp.jewels), subKeys = sub_k, notableCnt = sp.allocatedNotableCount, keystoneCnt = sp.allocatedKeystoneCount,
		masteryCnt = sp.allocatedMasteryCount, masteryTypeCnt = sp.allocatedMasteryTypeCount,
		masteryTypes = shallow_copy(sp.allocatedMasteryTypes), tattooTypes = shallow_copy(sp.allocatedTattooTypes),
		masterySel = shallow_copy(sp.masterySelections), hashOvr = shallow_copy(sp.hashOverrides),
		activeItemSetId = it.activeItemSetId, slotSel = sl_s, slotAct = sl_a, setSel = st_s, setAct = st_a,
		jewelIds = j_ids, itemsRef = i_ref, itemRaw = i_raw, itemOrderList = copy_array(it.itemOrderList),
		activeSkillSetId = st.activeSkillSetId, groups = grps, activeCfgId = ct.activeConfigSetId,
		cfgIn = shallow_copy((cs and cs.input) or ct.input), cmods = clone_cmods(cs and cs.customModsList), party = snap_party(b.partyTab),
	}
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
	local sc_d = b.characterLevel ~= snap.charLvl or b.characterLevelAutoMode ~= snap.charLvlAuto or b.bandit ~= snap.bandit or b.pantheonMajorGod ~= snap.pMajor or b.pantheonMinorGod ~= snap.pMinor
	if sc_d then
		b.characterLevel, b.characterLevelAutoMode, b.bandit, b.pantheonMajorGod, b.pantheonMinorGod = snap.charLvl, snap.charLvlAuto, snap.bandit, snap.pMajor, snap.pMinor
		local cl = b.controls and b.controls.characterLevel
		if cl and type(cl.SetText) == "function" then cl:SetText(tostring(snap.charLvl)) end
	end
	local it_d, j_d, tb_d = restore_items(b, snap)
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
	if sc_d or it_d or sp_d or sk_d or cf_d or pt_d then
		if type(wipeGlobalCache) == "function" then wipeGlobalCache() end
		b.buildFlag = true
		if params.rebuild ~= false then
			if _G.launch and type(_G.launch.OnFrame) == "function" then _G.launch:OnFrame()
			else
				if b.skillsTab and type(b.skillsTab.UpdateSocketGroups) == "function" then b.skillsTab:UpdateSocketGroups() end
				b.calcsTab:BuildOutput()
				if type(b.RefreshStatList) == "function" then b:RefreshStatList() end
				b.buildFlag = false
			end
		end
	end
	if params.drop == true then snapshots[sid] = nil end
	return { ok = true }
end

local function rpc_cow_drop(params)
	snapshots[req_sid(params, "cow_drop")] = nil
	return { ok = true }
end

local function rpc_direct_calc(params)
	local b = get_build()
	local skip_ehp = type(params) == "table" and params.skipEHP == true
	local calcs = b.calcsTab.calcs
	if b.buildFlag and b.skillsTab and type(b.skillsTab.UpdateSocketGroups) == "function" then b.skillsTab:UpdateSocketGroups() end
	if b.spec and type(b.spec.nodes) == "table" then for _, nd in pairs(b.spec.nodes) do nd.finalModList = nd.modList end end
	if type(wipeGlobalCache) == "function" then wipeGlobalCache() end
	local env, cP, cE, cM = calcs.initEnv(b, "CALCULATOR")
	env.triggerSourceCache = {}
	calcs.perform(env, skip_ehp and true or nil)
	local out = env.player.output
	if has_full_dps_skills(b) then
		if env.player.mainSkill and type(cacheSkillUUID) == "function" then
			local uid = cacheSkillUUID(env.player.mainSkill, env)
			local cd = GlobalCache and GlobalCache.cachedData and GlobalCache.cachedData["CALCULATOR"]
			local me = cd and cd[uid]
			if me and me.Env == env then env.triggerSourceCache[uid] = me end
		end
		local fd = calcs.calcFullDPS(b, "CALCULATOR", {}, { cachedPlayerDB = cP, cachedEnemyDB = cE, cachedMinionDB = cM, env = nil, triggerSourceCache = env.triggerSourceCache })
		out.SkillDPS, out.FullDPS, out.FullDotDPS = fd.skills, fd.combinedDPS, fd.TotalDotDPS
	else out.FullDPS, out.FullDotDPS = 0, 0 end
	local mdb = env.modDB
	if mdb and type(mdb.Sum) == "function" then
		out.ExtraPoints = mdb:Sum("BASE", nil, "ExtraPoints")
		local sc = { source = "Tree" }
		out["Spec:LifeInc"], out["Spec:ManaInc"], out["Spec:EnergyShieldInc"] = mdb:Sum("INC", sc, "Life"), mdb:Sum("INC", sc, "Mana"), mdb:Sum("INC", sc, "EnergyShield")
		out["Spec:ArmourInc"], out["Spec:EvasionInc"] = mdb:Sum("INC", sc, "Armour", "ArmourAndEvasion"), mdb:Sum("INC", sc, "Evasion", "ArmourAndEvasion")
	end
	local stats, sflags, warns = {}, {}, {}
	for k, v in pairs(out) do if type(k) == "string" then local sv = sanitize(v); if sv ~= nil then stats[k] = sv end end end
	local mo = (type(out.Minion) == "table" and out.Minion) or (env.minion and type(env.minion.output) == "table" and env.minion.output) or nil
	if mo then
		for mk, mv in pairs(mo) do
			if type(mk) == "string" and stats["Minion" .. mk] == nil then local sv = sanitize(mv); if sv ~= nil then stats["Minion" .. mk] = sv end end
		end
	end
	local rf = env.player.mainSkill and env.player.mainSkill.skillFlags
	if type(rf) == "table" then for fk, fv in pairs(rf) do if type(fk) == "string" and type(fv) == "boolean" then sflags[fk] = fv end end end
	if not skip_ehp and type(b.RefreshStatList) == "function" and b.controls and b.controls.warnings then
		local pe, po = b.calcsTab.mainEnv, b.calcsTab.mainOutput
		b.calcsTab.mainEnv, b.calcsTab.mainOutput = env, out
		pcall(b.RefreshStatList, b)
		b.calcsTab.mainEnv, b.calcsTab.mainOutput = pe, po
		local tr = (type(PobToolsGetTranslate) == "function" and PobToolsGetTranslate() and type(PobToolsTranslateDisplay) == "function") and PobToolsTranslateDisplay or nil
		for _, wl in ipairs(b.controls.warnings.lines or {}) do
			if type(wl) == "string" and wl ~= "" then warns[#warns + 1] = tr and tr(wl) or wl end
		end
	end
	local pts = nil
	if b.spec and type(b.spec.CountAllocNodes) == "function" then
		local u, a = b.spec:CountAllocNodes()
		pts = { used = u, usedMax = 122 + (tonumber(out.ExtraPoints) or 0), ascUsed = a, ascMax = 8 }
	end
	return { stats = stats, skillFlags = sflags, points = pts, warnings = warns }
end

M.rpc_handlers = {
	cow_snapshot = rpc_cow_snapshot,
	cow_restore = rpc_cow_restore,
	cow_drop = rpc_cow_drop,
	direct_calc = rpc_direct_calc,
}

return M
