#!/usr/bin/env bash
# Smoke-test a standalone macOS/Linux build: bundled data, frozen daemon spawn, bundled runtime.
#
# Usage: smoke_frozen.sh <path/to/pob> <expected version>

set -euo pipefail

exe="$1"
expected="$2"

fail() { echo "::error::$*" >&2; exit 1; }

ver="$("$exe" --version)" || fail "pob --version failed"
[[ "$ver" == *"$expected"* ]] || fail "pob --version printed '$ver', expected $expected"
"$exe" timeless --help >/dev/null || fail "pob timeless --help failed (missing bundled cli_timeless?)"
"$exe" diagnose --help >/dev/null || fail "pob diagnose --help failed"
"$exe" ground "Divine Blessing with Hatred" --json >/dev/null || fail "pob ground failed (missing bundled guardrails/game_data?)"
"$exe" data tick coc --cdr 14 --aps 8.28 --json >/dev/null || fail "pob data tick failed"
"$exe" bench list --slot gloves --limit 3 >/dev/null || fail "pob bench list failed (missing bundled bench_costs.json?)"
"$exe" trade convert --text $'Rarity: RARE\nDoom Hand\nSlink Gloves\nImplicits: 0\n+42% to Fire Resistance' \
  --to cold --json >/dev/null || fail "pob trade convert failed"

# The engine runtime ships inside the build; it must run PoB's Lua syntax and load lua-utf8.
runtime="$(dirname "$exe")/_internal/runtime/bin"
[ -x "$runtime/luajit" ] || fail "no bundled runtime at $runtime"
LUA_CPATH="$runtime/?.so;;" "$runtime/luajit" -e \
  'local c = 1; c += 41; assert(c == 42); assert(require("lua-utf8").len("ab") == 2)' ||
  fail "the bundled runtime failed its self-test"

# The frozen daemon spawns and fails fast with bootstrap guidance when no engine exists.
rt="$(mktemp -d)"
trap 'rm -rf "$rt"' EXIT
set +e
out="$(POB_RUNTIME_DIR="$rt" POB_LEGACY_DIR="" POB_OFFLINE=1 "$exe" status 2>&1)"
code=$?
set -e
[ "$code" -eq 1 ] || fail "pob status without an engine exited $code, expected 1: $out"
[[ "$out" == *bootstrap* ]] || fail "pob status gave no bootstrap guidance: $out"
echo "Standalone pob smoke test passed ($ver)."
