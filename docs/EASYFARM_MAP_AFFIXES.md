# EasyFarm map modifiers for a PoE1 build

Use this workflow only when the user explicitly asks to modify EasyFarm (易刷 / 易泥 / E-farm) map-modifier rules or requests an EasyFarm map scheme/code. Resolve the build from the PoB code or share URL in the current request, the build the user identified earlier in the conversation, or an explicitly selected slot. Ask for a build source when none is available. A PoB code alone, a generic build or map question, or mentioning EasyFarm only in attached material does not trigger the workflow.

For example, “这是我的 PoB，帮我改一下易刷地图词缀” triggers PoB analysis, web research, map recommendations, and code generation in that order. “先看看易刷怎么标，暂时别生成” stops after the recommendations. EasyFarm handles the user's later preview and application.

## Identify how this build plays

Load the supplied build with `pob load SOURCE --target ref` in an unused slot, or inspect the slot the user selected. If both slots are occupied, confirm which slot to use before replacing a build. Run `pob summary --target SLOT`, `pob audit --target SLOT`, and `pob items --target SLOT` for the build profile. Use `pob skills` for extra skill detail when available for the selected slot. Use the active loadout to identify the main skill, damage type and delivery, trigger or resource loop, ascendancy, enabling items or keystones, and the actual recovery and defense layers. Check whether the build depends on conditions such as flask uptime, being hit, aura effect, block, or killing monsters. Record a brief build profile with PoB evidence. A skill or ascendancy alone can describe several different builds.

## Research the matching archetype

Search the web after identifying the build. Use the main skill or trigger with its ascendancy and an enabling item or mechanic, scoped to PoE1 and the build's patch. Look for a matching build guide, written build showcase, or author notes that describe strengths, weaknesses, and map modifiers. For example, the query `PoE1 3.30 Cast when Stunned Chieftain Bloodnotch map modifiers` is more specific than `Chieftain map modifiers`. For individual mechanics or modifiers, check official patch notes, the PoE Wiki, and PoEDB using the agent’s web tools.

Compare each source's patch, setup, and content to the active PoB. A guide can identify likely failure modes, while the loaded build determines whether they apply. Check the actual enabling gear, auras, and recovery before using a guide's avoid list. Cite the sources behind the relevant strengths and weaknesses. If no comparable guide is available, say so and use verified mechanics and PoB evidence for the recommendations. Keep disagreements or unverified interactions visible.

## Recommend map modifiers for the loaded build

1. Summarize what the build is good at and the conditions that make it fail. Show the relationship between each important strength or weakness and the map modifier that preserves or disrupts it.
2. Present the researched effects as **can run**, **caution**, or **skip**. Use **can run** when the current setup can handle the effect, **caution** when it amplifies a meaningful risk, and **skip** when it breaks a core damage, sustain, or defense dependency. Keep unevaluated effects separate from the can-run list. For each marked effect, give the reason for this build. Call out thresholds, map modifier effect, and relevant combinations when they matter. For example, no regeneration is high risk when regeneration is a primary source of sustain, while reflect depends on the damage actually dealt and the build's defenses. Character sheet values alone do not prove uptime.
3. For the import code, verify the exact stat identifier and resource path against the target PoE1 version using a user-provided or otherwise verified stat-description export. Check every display variant associated with the ID. For example, if the same ID covers increased and reduced aura effect, keep the reduced effect in the recommendations until a variant-aware rule can encode that reduction alone.
4. Return the build profile, sourced strengths and weaknesses, and map recommendations. Then generate the import code with verified skip and caution markers unless the user requested a preview only. The request to modify EasyFarm rules already authorizes generation. Leave can-run effects unmarked and keep unverified IDs, paths, or conditions in the written recommendations. If no eligible markers can be verified, report what is missing and do not emit an empty code.

## Generate an EasyFarm import code

Save a UTF-8 JSON request. The following is an example for a build that relies on life regeneration. The same classification need not apply to other builds.

```json
{
  "name": "Example build map warnings",
  "marks": [
    {
      "severity": "skip",
      "statId": "map_player_no_regeneration",
      "statPath": "Metadata/StatDescriptions/stat_descriptions.csd",
      "reason": "Life regeneration is the build's primary recovery source."
    }
  ]
}
```

Run from the repository root with Python 3.10+.

```text
python pob_cli/scripts/easyfarm_map_affixes.py map-marks.json
```

The output is one EasyFarm import code. Redirect stdout to a text file if requested. The code contains only affix rules for the selected paths and stat IDs. Each mark adds `[跳过]` or `[慎打]` as a plain prefix and preserves the existing color, placeholders, and skill links. Omit identifiers that also match unwanted variants of the same effect.

Import as a separate scheme in EasyFarm and preview its matches before applying. If source validation fails, reload the current game resources and regenerate the preview. This workflow has no game-writing step or hook.
