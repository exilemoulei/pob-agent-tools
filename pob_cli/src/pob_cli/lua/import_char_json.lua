-- import_character_json: an official character JSON bundle through PoB's own ImportTab,
-- hardened against unknown tattoos, invalid classes, upstream Runegraft mastery drops, and Lv70+ res penalty drift.
local H = require("pob_host")

local function shallow_copy(tbl)
	local out = {}
	if type(tbl) == "table" then
		for k, v in pairs(tbl) do
			out[k] = v
		end
	end
	return out
end

local function resolve_valid_class(raw_cls, passives, tree)
	local classes = tree and tree.classes
	if type(classes) ~= "table" then return raw_cls end
	if type(raw_cls) == "string" and raw_cls ~= "" then
		for _, cls in pairs(classes) do
			if cls.name == raw_cls then return raw_cls end
			for i = 0, #(cls.classes or {}) do
				if cls.classes[i] and cls.classes[i].name == raw_cls then return raw_cls end
			end
		end
	end
	-- PassiveTree.lua migrates tree.classes to 0-indexed [0..6] (0=Scion..5=Templar..6=Shadow) with cls.classes[1..3].
	local cid = tonumber(passives and passives.character)
	local aid = tonumber(passives and passives.ascendancy) or 0
	local cls = cid and classes[cid]
	if cls then
		if aid > 0 and cls.classes and cls.classes[aid] and cls.classes[aid].name then
			return cls.classes[aid].name
		end
		if cls.name then return cls.name end
	end
	return raw_cls
end

local function filter_skill_overrides(raw_overrides, tattoo_nodes, warnings)
	if type(raw_overrides) ~= "table" or type(tattoo_nodes) ~= "table" then return raw_overrides end
	local filtered = {}
	for nid, ov in pairs(raw_overrides) do
		local nm = type(ov) == "table" and ov.name or nil
		local tn = type(nm) == "string" and tattoo_nodes[nm] or nil
		if tn ~= nil then
			filtered[nid] = ov
			if (tn.legacy == true or tn.overrideType == "UnknownTattoo") and #(tn.sd or {}) == 0 then
				warnings[#warnings + 1] = string.format(
					"[IMPORT-OVERRIDE-WARN] Node %s: legacy placeholder override '%s' has no active stats.",
					tostring(nid),
					nm
				)
			end
		elseif type(nm) == "string" and nm:find("[\228-\233]") then
			warnings[#warnings + 1] = string.format(
				"[CN-TRANSLATE-WARN] Node %s: untranslated passive override '%s' skipped.",
				tostring(nid),
				nm
			)
		else
			warnings[#warnings + 1] = string.format(
				"[IMPORT-OVERRIDE-SKIP] Node %s: unknown passive override '%s' skipped.",
				tostring(nid),
				tostring(nm or ov)
			)
		end
	end
	return filtered
end

local function has_foulborn_keystone_jump(b)
	local needle = "medium radius of allocated keystone"
	local spec = b.spec
	for _, node in pairs((spec and spec.allocNodes) or {}) do
		for _, line in ipairs(node.sd or {}) do
			if type(line) == "string" and line:lower():find(needle, 1, true) then return true end
		end
	end
	local items = b.itemsTab and b.itemsTab.items or {}
	for _, item_id in pairs((spec and spec.jewels) or {}) do
		local it = item_id ~= 0 and items[item_id] or nil
		for _, list in ipairs({ it and it.explicitModLines, it and it.mutatedModLines }) do
			for _, ml in ipairs(list or {}) do
				if type(ml) == "table" and type(ml.line) == "string" and ml.line:lower():find(needle, 1, true) then
					return true
				end
			end
		end
	end
	return false
end

local function repair_post_import_allocations(b, raw_hashes)
	local spec = b.spec
	if not spec or type(raw_hashes) ~= "table" then return false end
	local foulborn_jump = has_foulborn_keystone_jump(b)
	local repaired = false
	for _, h in ipairs(raw_hashes) do
		local id = tonumber(h)
		local node = id and spec.nodes and spec.nodes[id]
		if node and not spec.allocNodes[id] then
			local ov = spec.hashOverrides and spec.hashOverrides[id]
			local is_runegraft = ov and (ov.overrideType == "AlternateMastery" or node.type == "Mastery")
			local is_foulborn = false
			if foulborn_jump and node.type ~= "Keystone" then
				for _, alloc in pairs(spec.allocNodes or {}) do
					if alloc.type == "Keystone" and alloc.nodesInRadius and alloc.nodesInRadius[2] and alloc.nodesInRadius[2][id] then
						is_foulborn = true
						break
					end
				end
			end
			if is_runegraft or is_foulborn then
				if is_runegraft and not (spec.masterySelections and spec.masterySelections[id]) then
					spec.masterySelections = spec.masterySelections or {}
					local orig = spec.tree and spec.tree.nodes and spec.tree.nodes[id]
					local me = (node.masteryEffects and node.masteryEffects[1])
						or (orig and orig.masteryEffects and orig.masteryEffects[1])
					if me and me.effect then
						spec.masterySelections[id] = me.effect
					end
				end
				node.alloc = true
				spec.allocNodes[id] = node
				repaired = true
			end
		end
	end
	if repaired then
		spec:BuildAllDependsAndPaths()
	end
	return repaired
end

local function collect_integrity(b, clean_passives)
	local alloc_ids = {}
	local dropped_hashes = {}
	local cluster_jewels = {}
	local spec = b.spec
	if spec and spec.allocNodes then
		for nid in pairs(spec.allocNodes) do
			alloc_ids[#alloc_ids + 1] = nid
		end
		table.sort(alloc_ids)
	end
	if spec and type(clean_passives) == "table" and type(clean_passives.hashes) == "table" then
		local ovs = spec.hashOverrides or {}
		local m_sel = spec.masterySelections or {}
		for _, h in ipairs(clean_passives.hashes) do
			local id = tonumber(h)
			local node = id and spec.nodes and spec.nodes[id]
			if node and not spec.allocNodes[id] and (node.type ~= "Mastery" or m_sel[id] or ovs[id]) then
				dropped_hashes[#dropped_hashes + 1] = id
			end
		end
	end
	local items = b.itemsTab and b.itemsTab.items or {}
	for nid, item_id in pairs((spec and spec.jewels) or {}) do
		local it = item_id ~= 0 and items[item_id] or nil
		if it and (it.clusterJewel or (type(it.baseName) == "string" and it.baseName:find("Cluster Jewel", 1, true))) then
			local mods = {}
			for _, list in ipairs({ it.enchantModLines, it.implicitModLines, it.explicitModLines }) do
				for _, ml in ipairs(list or {}) do
					if type(ml) == "table" and type(ml.line) == "string" then mods[#mods + 1] = ml.line end
				end
			end
			local jd = it.jewelData or {}
			cluster_jewels[#cluster_jewels + 1] = {
				nodeId = nid,
				name = it.name or it.baseName or "",
				baseName = it.baseName or "",
				valid = not not jd.clusterJewelValid,
				skill = jd.clusterJewelSkill or nil,
				rawMods = mods,
			}
		end
	end
	return alloc_ids, cluster_jewels, dropped_hashes
end

return function(params, bridge_dispatcher)
	if type(params) ~= "table" then error("import_character_json requires params object", 0) end
	local char_info = params.char
	if type(char_info) ~= "table" then error("import_character_json requires params.char object", 0) end

	local prev = H.get_build({ optional = true, rebuild = false })
	local file, name, sub = prev and prev.dbFileName, prev and prev.buildName, prev and prev.dbFileSubPath
	bridge_dispatcher("new_build", { name = name })
	local b = H.get_build({ optional = true })
	if not b or not b.importTab then error("PoB importTab is not available", 0) end
	if file then b.dbFileName, b.dbFileSubPath = file, sub end
	local tab = b.importTab
	local warnings = {}

	local clean_passives = nil
	local passive_data = params.passiveSkills
	if type(passive_data) == "table" then
		clean_passives = shallow_copy(passive_data)
		clean_passives.hashes = clean_passives.hashes or {}
		clean_passives.hashes_ex = clean_passives.hashes_ex or {}
		if b.configTab and b.configTab.input then
			clean_passives.bandit_choice = clean_passives.bandit_choice or b.configTab.input.bandit
			clean_passives.pantheon_major = clean_passives.pantheon_major or b.configTab.input.pantheonMajorGod
			clean_passives.pantheon_minor = clean_passives.pantheon_minor or b.configTab.input.pantheonMinorGod
		end
		local tattoo_nodes = b.spec and b.spec.tree and b.spec.tree.tattoo and b.spec.tree.tattoo.nodes
		if clean_passives.skill_overrides then
			clean_passives.skill_overrides = filter_skill_overrides(clean_passives.skill_overrides, tattoo_nodes, warnings)
		end
		local tree_char = shallow_copy(char_info)
		tree_char.league = tostring(tree_char.league or "")
		tree_char.class = resolve_valid_class(tree_char.class, clean_passives, b.latestTree)
		tree_char.passives = clean_passives
		tree_char.jewels = clean_passives.items
		tab:ImportPassiveTreeAndJewels(tree_char, params.deleteJewels ~= false)
		repair_post_import_allocations(b, clean_passives.hashes)
	end

	local items_data = params.items
	if type(items_data) == "table" then
		local item_char = shallow_copy(char_info)
		item_char.equipment = items_data.items
		item_char.guardian = items_data.guardian
		tab:ImportItemsAndSkills(item_char, params.clearItems ~= false, params.clearSkills ~= false, params.ignoreWeaponSwap == true)
	end

	-- Lock endgame Act 10 (-60%) resistance penalty for Lv70+ chars (avoids EstimatePlayerProgress Act 6-10 bug).
	if (tonumber(char_info.level) or 0) >= 70 and b.configTab then
		local ctl = b.configTab.varControls and b.configTab.varControls["resistancePenalty"]
		if ctl then
			local picked = false
			for i, v in ipairs(ctl.list or {}) do
				if type(v) == "table" and v.val == -60 then
					ctl:SetSel(i)
					picked = true
					break
				end
			end
			if not picked then ctl:SetSel(3) end
		end
		if b.configTab.input then
			b.configTab.input.resistancePenalty = -60
		end
		local active_set = b.configTab.configSets and b.configTab.activeConfigSetId
			and b.configTab.configSets[b.configTab.activeConfigSetId]
		if active_set and active_set.input then
			active_set.input.resistancePenalty = -60
		end
		if b.configTab.BuildModList then b.configTab:BuildModList() end
	end

	b.modFlag = true
	b.buildFlag = true
	H.frame()
	if b.calcsTab and b.calcsTab.BuildOutput then b.calcsTab:BuildOutput() end
	local alloc_ids, cluster_jewels, dropped_hashes = collect_integrity(b, clean_passives)
	return {
		imported = true,
		name = char_info.name,
		level = char_info.level,
		league = char_info.league,
		warnings = warnings,
		allocatedCount = #alloc_ids,
		allocatedNodeIds = alloc_ids,
		droppedHashes = dropped_hashes,
		clusterJewels = cluster_jewels,
	}
end
