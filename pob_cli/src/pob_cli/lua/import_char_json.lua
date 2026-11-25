-- import_character_json: an official character JSON bundle through PoB's own ImportTab, the way
-- its character import dialog applies it, without the dialog's LaunchSubScript download.
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

return function(params, bridge_dispatcher)
	if type(params) ~= "table" then
		error("import_character_json requires params object", 0)
	end
	local char_info = params.char
	if type(char_info) ~= "table" then
		error("import_character_json requires params.char object", 0)
	end
	-- A fresh build, as import_code "replace" gives the XML route: nothing of the previous
	-- build (config, other sets, skill groups) survives into the imported character, while its
	-- file and name stay, so saving still writes where the previous build came from.
	local prev = H.get_build({ optional = true, rebuild = false })
	local file, name, sub = prev and prev.dbFileName, prev and prev.buildName, prev and prev.dbFileSubPath
	bridge_dispatcher("new_build", { name = name })
	local b = H.get_build({ optional = true })
	if not b or not b.importTab then
		error("PoB importTab is not available", 0)
	end
	if file then b.dbFileName, b.dbFileSubPath = file, sub end
	local tab = b.importTab

	local passive_data = params.passiveSkills
	if type(passive_data) == "table" then
		if b.configTab and b.configTab.input then
			passive_data.bandit_choice = passive_data.bandit_choice or b.configTab.input.bandit
			passive_data.pantheon_major = passive_data.pantheon_major or b.configTab.input.pantheonMajorGod
			passive_data.pantheon_minor = passive_data.pantheon_minor or b.configTab.input.pantheonMinorGod
		end
		local tree_char = shallow_copy(char_info)
		tree_char.passives = passive_data
		tree_char.jewels = passive_data.items
		local delete_jewels = params.deleteJewels ~= false
		tab:ImportPassiveTreeAndJewels(tree_char, delete_jewels)
	end

	local items_data = params.items
	if type(items_data) == "table" then
		local item_char = shallow_copy(char_info)
		item_char.equipment = items_data.items
		item_char.guardian = items_data.guardian
		local clear_items = params.clearItems ~= false
		local clear_skills = params.clearSkills ~= false
		local ignore_swap = params.ignoreWeaponSwap == true
		tab:ImportItemsAndSkills(item_char, clear_items, clear_skills, ignore_swap)
	end

	-- One PoB frame settles the pending rebuild (output, sidebar) as after the dialog closes.
	b.modFlag = true
	b.buildFlag = true
	H.frame()
	return {
		imported = true,
		name = char_info.name,
		level = char_info.level,
		league = char_info.league,
	}
end
