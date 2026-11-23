#!/usr/bin/env bash
# Install or upgrade pob4agents (`pob`) on macOS or Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install.sh | bash
#
# Installs the standalone build of the latest release (no Python needed) into
# ~/.local/share/pob4agents/bin and links ~/.local/bin/pob to it. Every download is checked against
# the release's SHA256SUMS.txt, whose Ed25519 signature is checked first (with OpenSSL 3, or python3
# when OpenSSL cannot). When no build fits this machine (an older glibc, another CPU, a release
# without macOS/Linux builds), it installs the Python package with uv, pipx or pip instead.
#
# Environment:
#   POB_VERSION        release to install, 0.9.0 or v0.9.0 (default: latest)
#   POB_INSTALL_DIR    where the build goes (default: ${XDG_DATA_HOME:-~/.local/share}/pob4agents/bin)
#   POB_BIN_DIR        where the `pob` link goes (default: ~/.local/bin)
#   POB_PROXY          proxy for every download (default: HTTPS_PROXY / HTTP_PROXY, as curl reads them)
#   POB_GITHUB_MIRROR  prefix for GitHub URLs; a mirror can serve the files but cannot change them
#   POB_FROM_SOURCE=1  install the Python package instead of the standalone build
#   POB_BRANCH         install the Python package from this branch of the repository
#
#   ./install.sh --self-test   check the Ed25519 verifier against the RFC 8032 test vectors

set -euo pipefail

REPO="exilemoulei/pob4agents"
# Release signing keys: base64 raw Ed25519 public keys, the same list as RELEASE_PUBLIC_KEYS in
# pob_cli/src/pob_cli/updater/signature.py (test_release_signature.py keeps them equal).
RELEASE_PUBLIC_KEYS=()
# Releases after v0.8.0 ship SHA256SUMS.txt.sig; for them a missing or bad signature stops the install.
FIRST_SIGNED_VERSION="0.8.1"

say() { printf '[pob-install] %s\n' "$*"; }
die() { printf '[pob-install] ERROR: %s\n' "$*" >&2; exit 1; }

# --- Downloads ---------------------------------------------------------------------------------

github_url() {
  if [ -n "${POB_GITHUB_MIRROR:-}" ]; then printf '%s/%s' "${POB_GITHUB_MIRROR%/}" "$1"; else printf '%s' "$1"; fi
}

curl_args() {
  printf '%s\n' -fsSL --retry 2 --connect-timeout 15 -A pob-installer/2.0
  if [ -n "${POB_PROXY:-}" ]; then printf '%s\n' -x "$POB_PROXY"; fi
}

fetch() { # url out-file
  local args=()
  while IFS= read -r a; do args+=("$a"); done < <(curl_args)
  curl "${args[@]}" -o "$2" "$1"
}

latest_tag() {
  local args=() url location
  while IFS= read -r a; do args+=("$a"); done < <(curl_args)
  url="$(github_url "https://github.com/$REPO/releases/latest")"
  # The redirect target of /releases/latest names the tag; no GitHub API call needed. A mirror may
  # follow the redirect itself, so fall back to where the request ends up.
  location="$(curl "${args[@]/-fsSL/-fsS}" -o /dev/null -w '%{redirect_url}' "$url")" || true
  if [ -z "$location" ]; then location="$(curl "${args[@]}" -o /dev/null -w '%{url_effective}' "$url")" || true; fi
  case "$location" in
    */releases/tag/v*) location="${location##*/releases/tag/}"; printf '%s' "${location%%[?#]*}" ;;
    *) die "Could not resolve the latest release (got '${location}'). Set POB_VERSION or POB_PROXY." ;;
  esac
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

version_ge() { # a b: true when a >= b, comparing X.Y.Z and ignoring a -prerelease suffix
  local a="${1%%-*}" b="${2%%-*}" x y i
  IFS=. read -r -a x <<<"$a"
  IFS=. read -r -a y <<<"$b"
  for i in 0 1 2; do
    if [ "${x[i]:-0}" -gt "${y[i]:-0}" ]; then return 0; fi
    if [ "${x[i]:-0}" -lt "${y[i]:-0}" ]; then return 1; fi
  done
  return 0
}

# --- Ed25519 (RFC 8032) ------------------------------------------------------------------------

# Same algorithm as pob_cli/src/pob_cli/updater/signature.py, for machines without OpenSSL 3.
PY_ED25519='
import base64, hashlib, sys
P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
I = pow(2, (P - 1) // 4, P)
def add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % P
    b = (p[1] + p[0]) * (q[1] + q[0]) % P
    c = 2 * p[3] * q[3] * D % P
    d = 2 * p[2] * q[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)
def mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = add(q, p)
        p = add(p, p)
        s >>= 1
    return q
def recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * I % P
    if (x * x - x2) % P:
        return None
    return P - x if (x & 1) != sign else x
def point(raw):
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)
def verify(pk, msg, sig):
    if len(pk) != 32 or len(sig) != 64:
        return False
    a, r, s = point(pk), point(sig[:32]), int.from_bytes(sig[32:], "little")
    if a is None or r is None or s >= L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pk + msg).digest(), "little") % L
    gy = 4 * pow(5, P - 2, P) % P
    gx = recover_x(gy, 0)
    u, v = mul(s, (gx, gy, 1, gx * gy % P)), add(r, mul(h, a))
    return (u[0] * v[2] - v[0] * u[2]) % P == 0 and (u[1] * v[2] - v[1] * u[2]) % P == 0
try:
    pk, sig = base64.b64decode(sys.argv[1]), base64.b64decode(sys.argv[3])
    with open(sys.argv[2], "rb") as fh:
        ok = verify(pk, fh.read(), sig)
except Exception:
    ok = False
sys.exit(0 if ok else 1)
'

verify_openssl() { # key-b64 msg-file sig-b64
  local d rc=0
  d="$(mktemp -d)"
  {
    printf '%s\n' '-----BEGIN PUBLIC KEY-----'
    # SubjectPublicKeyInfo DER prefix for a raw Ed25519 key.
    { printf '\060\052\060\005\006\003\053\145\160\003\041\000'; printf '%s' "$1" | openssl base64 -d -A; } | openssl base64 -A
    printf '\n%s\n' '-----END PUBLIC KEY-----'
  } >"$d/pub.pem" 2>/dev/null || rc=1
  printf '%s' "$3" | openssl base64 -d -A >"$d/sig" 2>/dev/null || rc=1
  if [ "$rc" -eq 0 ]; then
    openssl pkeyutl -verify -pubin -inkey "$d/pub.pem" -rawin -in "$2" -sigfile "$d/sig" >/dev/null 2>&1 || rc=1
  fi
  rm -rf "$d"
  return "$rc"
}

verify_python3() { python3 -I -c "$PY_ED25519" "$1" "$2" "$3" >/dev/null 2>&1; }

# RFC 8032 section 7.1 TEST 1..3 as base64: public key, message (printf escapes), signature.
RFC_KEYS=(11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo= PUAXw+hDiVqStwqnTRt+vJyYLM8uxJaMwM1V8Sr0Zgw= /FHNjmIYoaONpH7QAjDwWAgW7RO6MwOsXeuRFUiQgCU=)
RFC_MSGS=('' '\162' '\257\202')
RFC_SIGS=(
  5VZDAMNgrHKQhuLMgG6CioSHfx645dl02HPgZSJJAVVfuIIVkKM7rMYeOXAc+bRr0lv18FlbviRlUUFDjnoQCw==
  kqAJqfDUyrhyDoILX2QlQKKye1QWUD+Ps3YiI+vbadoIWsHkPhWZbkWPNhPQ8R2MOHsurrQwKu6wDSkWErsMAA==
  YpHWV97sJAJIJ+acOr4BowzlSKKEdDpEXjaA19taw6wY/5tTjRbykK5n92CYTcZZSnwV6XFu0o3AJ77O6h7ECg==
)

backend_passes_rfc() { # backend: true when it accepts every RFC vector and rejects a changed message
  local d i ok=0
  d="$(mktemp -d)"
  for i in 0 1 2; do
    # shellcheck disable=SC2059 # the messages are printf escapes on purpose
    printf "${RFC_MSGS[i]}" >"$d/m"
    # shellcheck disable=SC2059
    printf "${RFC_MSGS[i]}x" >"$d/bad"
    "verify_$1" "${RFC_KEYS[i]}" "$d/m" "${RFC_SIGS[i]}" || ok=1
    if "verify_$1" "${RFC_KEYS[i]}" "$d/bad" "${RFC_SIGS[i]}"; then ok=1; fi
  done
  rm -rf "$d"
  return "$ok"
}

python3_usable() {
  command -v python3 >/dev/null 2>&1 || return 1
  # On macOS without Command Line Tools, /usr/bin/python3 only opens an install dialog.
  if [ "$(uname -s)" = Darwin ] && [ "$(command -v python3)" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
    return 1
  fi
  python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 6))' >/dev/null 2>&1
}

pick_backend() { # prints openssl, python3, or nothing
  local candidates=() b
  if [ -n "${POB_VERIFY_WITH:-}" ]; then candidates=("$POB_VERIFY_WITH"); else candidates=(openssl python3); fi
  for b in "${candidates[@]}"; do
    case "$b" in
      openssl) command -v openssl >/dev/null 2>&1 || continue ;;
      python3) python3_usable || continue ;;
      *) continue ;;
    esac
    if backend_passes_rfc "$b"; then printf '%s' "$b"; return 0; fi
  done
  return 0
}

self_test() {
  local b found=0
  for b in openssl python3; do
    if [ -n "${POB_VERIFY_WITH:-}" ] && [ "$b" != "$POB_VERIFY_WITH" ]; then continue; fi
    if [ "$b" = openssl ] && ! command -v openssl >/dev/null 2>&1; then say "$b: not installed"; continue; fi
    if [ "$b" = python3 ] && ! python3_usable; then say "$b: not usable"; continue; fi
    if backend_passes_rfc "$b"; then say "$b: RFC 8032 vectors pass"; found=1; else say "$b: cannot verify Ed25519"; fi
  done
  [ "$found" -eq 1 ] || die "No Ed25519 verifier on this machine (install OpenSSL 3 or python3)."
  say "Ed25519 self-test passed."
}

verify_sums_signature() { # sums-file sig-file backend
  local sig key
  sig="$(tr -d ' \r\n' <"$2")"
  for key in ${RELEASE_PUBLIC_KEYS[@]+"${RELEASE_PUBLIC_KEYS[@]}"}; do
    if "verify_$3" "$key" "$1" "$sig"; then return 0; fi
  done
  return 1
}

# --- Python package (fallback, and POB_BRANCH / POB_FROM_SOURCE) -------------------------------

install_python_package() { # pip-style requirement: a wheel file or a git+ URL
  command -v python3 >/dev/null 2>&1 || die "python3 (>=3.11) is required for this install. On macOS: brew install python@3.13"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11+ is required (found $(python3 --version))."
  if command -v uv >/dev/null 2>&1; then
    say "Installing the Python package with uv tool ..."
    uv tool install --force "$1"
  elif command -v pipx >/dev/null 2>&1; then
    say "Installing the Python package with pipx ..."
    pipx install --force "$1"
  else
    say "Installing the Python package into user site-packages with pip ..."
    python3 -m pip install --user --upgrade "$1"
  fi
  say "Installed: $(pob --version 2>/dev/null || echo "run 'hash -r', or add ~/.local/bin to PATH")"
}

# --- Standalone build --------------------------------------------------------------------------

platform_tag() {
  local os arch
  case "$(uname -s)" in Darwin) os=macos ;; Linux) os=linux ;; *) return 1 ;; esac
  case "$(uname -m)" in arm64 | aarch64) arch=arm64 ;; x86_64 | amd64) arch=x64 ;; *) return 1 ;; esac
  printf '%s-%s' "$os" "$arch"
}

remove_old_python_installs() {
  # Earlier install.sh versions installed the Python package, whose `pob` would shadow the link.
  local name
  for name in pob-cli pob4agents; do
    if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "^$name "; then
      say "Removing the earlier uv tool install ($name) ..."
      uv tool uninstall "$name" >/dev/null 2>&1 || true
    fi
    if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q "^$name "; then
      say "Removing the earlier pipx install ($name) ..."
      pipx uninstall "$name" >/dev/null 2>&1 || true
    fi
  done
}

install_build() { # archive
  local install_dir bin_dir stage link
  install_dir="${POB_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/pob4agents/bin}"
  bin_dir="${POB_BIN_DIR:-$HOME/.local/bin}"
  stage="${install_dir}.new"
  rm -rf "$stage"
  mkdir -p "$stage" "$bin_dir"
  tar -xzf "$1" -C "$stage"
  if ! "$stage/pob" --version >/dev/null 2>&1; then
    rm -rf "$stage"
    return 1
  fi
  if [ -x "$install_dir/pob" ]; then
    # A daemon running from the old files would keep them in use; it restarts on the next command.
    "$install_dir/pob" stop >/dev/null 2>&1 || true
  fi
  rm -rf "${install_dir}.old"
  if [ -e "$install_dir" ]; then mv "$install_dir" "${install_dir}.old"; fi
  mv "$stage" "$install_dir"
  rm -rf "${install_dir}.old"

  link="$bin_dir/pob"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    remove_old_python_installs
    if [ -e "$link" ] && [ ! -L "$link" ]; then
      mv "$link" "$link.old"
      say "Moved the previous $link to $link.old."
    fi
  fi
  ln -sfn "$install_dir/pob" "$link"
  say "Installed $("$link" --version) to $install_dir"
  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) say "Add $bin_dir to PATH, e.g.: echo 'export PATH=\"$bin_dir:\$PATH\"' >> ~/.profile" ;;
  esac
  local found
  found="$(command -v pob 2>/dev/null || true)"
  if [ -n "$found" ] && [ "$found" != "$link" ]; then
    say "Note: \`pob\` currently resolves to $found, ahead of $link in PATH."
  fi
}

main() {
  if [ "${1:-}" = "--self-test" ]; then self_test; return; fi
  command -v curl >/dev/null 2>&1 || die "curl is required."

  if [ -n "${POB_BRANCH:-}" ]; then
    say "Installing $REPO@$POB_BRANCH from source."
    install_python_package "git+https://github.com/$REPO.git@$POB_BRANCH#subdirectory=pob_cli"
    return
  fi

  local tag version base plat name backend wheel
  if [ -n "${POB_VERSION:-}" ]; then tag="v${POB_VERSION#v}"; else tag="$(latest_tag)"; fi
  version="${tag#v}"
  say "Release: $tag"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  base="https://github.com/$REPO/releases/download/$tag"

  fetch "$(github_url "$base/SHA256SUMS.txt")" "$tmp/SHA256SUMS.txt" || die "Could not download SHA256SUMS.txt for $tag."
  if fetch "$(github_url "$base/SHA256SUMS.txt.sig")" "$tmp/SHA256SUMS.txt.sig" 2>/dev/null; then
    backend="$(pick_backend)"
    if [ -n "$backend" ]; then
      verify_sums_signature "$tmp/SHA256SUMS.txt" "$tmp/SHA256SUMS.txt.sig" "$backend" ||
        die "SHA256SUMS.txt of $tag is not signed by a pob4agents release key; refusing to install."
      say "Release signature verified (Ed25519, $backend)."
    elif [ -n "${POB_GITHUB_MIRROR:-}" ]; then
      die "Cannot check the release signature (needs OpenSSL 3 or python3), and a mirror is set; refusing to install."
    else
      say "Warning: no OpenSSL 3 or python3 to check the release signature; relying on HTTPS from GitHub and SHA-256."
    fi
  elif version_ge "$version" "$FIRST_SIGNED_VERSION"; then
    die "Could not download SHA256SUMS.txt.sig for $tag; refusing to install an unsigned release."
  else
    say "$tag predates signed releases; checking SHA-256 only."
  fi

  sum_of() { awk -v n="$1" '$2 == n || $2 == "*" n { print $1; exit }' "$tmp/SHA256SUMS.txt"; }
  download_checked() { # name: downloads into $tmp and checks it against SHA256SUMS.txt
    local want
    want="$(sum_of "$1")"
    [ -n "$want" ] || return 1
    fetch "$(github_url "$base/$1")" "$tmp/$1" || die "Could not download $1."
    [ "$(sha256_of "$tmp/$1")" = "$want" ] || die "$1 does not match SHA256SUMS.txt; refusing to install."
  }

  if [ -z "${POB_FROM_SOURCE:-}" ] && plat="$(platform_tag)"; then
    name="pob4agents-$version-$plat.tar.gz"
    if download_checked "$name"; then
      say "Downloaded $name (SHA-256 verified)."
      if install_build "$tmp/$name"; then
        say ""
        say "Next step, once: pob bootstrap   (behind a proxy: pob bootstrap --proxy http://127.0.0.1:7890)"
        return
      fi
      say "The standalone build does not run on this machine (glibc too old?); installing the Python package."
    else
      say "$tag has no standalone build for $plat; installing the Python package."
    fi
  fi

  wheel="$(awk '$2 ~ /-py3-none-any\.whl$/ { sub(/^\*/, "", $2); print $2; exit }' "$tmp/SHA256SUMS.txt")"
  if [ -n "$wheel" ]; then
    download_checked "$wheel"
    install_python_package "$tmp/$wheel"
  else
    install_python_package "git+https://github.com/$REPO.git@$tag#subdirectory=pob_cli"
  fi
  say ""
  say "Next step, once: pob bootstrap   (behind a proxy: pob bootstrap --proxy http://127.0.0.1:7890)"
}

main "$@"
