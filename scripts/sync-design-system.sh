#!/usr/bin/env bash
#
# sync-design-system.sh — vendor design-system assets into veto-mcp.
#
# Web dashboard (Spec A):
#   webui/vendor/veto-tokens.css  <- packages/tokens/dist/tokens.css
#   webui/vendor/veto-web.css     <- packages/web/dist/veto-web.css
#   webui/vendor/VERSIONS         <- JSON provenance manifest (VERSIONING.md)
#
# Terminal (Spec C) — ADDITIVE, never clobbers another agent's work:
#   veto_theme.py                 <- packages/terminal/python/veto_theme.py (+ _THEME_PATH patch)
#   theme.json                    <- packages/terminal/theme.json
#   DESIGN_SYSTEM_VERSIONS.json   <- JSON provenance manifest (VERSIONING.md)
#
# Structure:
#   1. Pre-flight: validate ALL inputs (existence, non-empty, JSON parses).
#   2. Per artifact: copy -> immediate byte-identity check (die on mismatch)
#      -> refresh that artifact's manifest entry. A partial failure can never
#      leave a manifest describing content that isn't on disk.
#   3. Import smoke-test of the vendored terminal module.
#   4. Post-sync checks: webui/index.html <link> references; the site's
#      file: deps resolve to the same @veto/* versions the manifests record.
#
# Usage: scripts/sync-design-system.sh [path-to-design-system-repo]
#        Defaults to ~/workspace/veto-design-system.
#
# Idempotent: safe to re-run after any monorepo rebuild. Exits non-zero if
# any byte-identity check fails. Every verification is an explicit
# `|| die` guard: `&&`-chain checks are inert under `set -e` (a failing
# command inside an && list, other than after the final &&, does NOT
# trigger exit) and are not used for verification anywhere in this script.
set -euo pipefail

DS="${1:-$HOME/workspace/veto-design-system}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$REPO/webui/vendor"
WEB_MANIFEST="$VENDOR/VERSIONS"
TERM_MANIFEST="$REPO/DESIGN_SYSTEM_VERSIONS.json"
SITE_PKG="$REPO/site/package.json"
INDEX_HTML="$REPO/webui/index.html"

die() { echo "sync-design-system: ERROR: $*" >&2; exit 1; }
warn() { echo "sync-design-system: WARNING: $*" >&2; }
info() { echo "sync-design-system: $*"; }

# ---------------------------------------------------------------- pre-flight: validate ALL inputs first
require_file() {
  # $1 = path — die unless it exists and is non-empty
  [ -f "$1" ] || die "missing input: $1"
  [ -s "$1" ] || die "empty input: $1"
}
require_json() {
  # $1 = path — die unless it exists, is non-empty, and parses as JSON
  require_file "$1"
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" 2>/dev/null \
    || die "unparseable JSON: $1"
}
pkg_version() {
  # $1 = path to package.json — echo its version, or die (no raw tracebacks)
  require_json "$1"
  local ver
  ver="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d.get("version"); sys.exit("missing version field") if not v else print(v)' "$1")" \
    || die "cannot read version from $1"
  [ -n "$ver" ] || die "empty version in $1"
  printf '%s' "$ver"
}

TOKENS_SRC="$DS/packages/tokens/dist/tokens.css"
WEB_SRC="$DS/packages/web/dist/veto-web.css"
THEME_PY_SRC="$DS/packages/terminal/python/veto_theme.py"
THEME_JSON_SRC="$DS/packages/terminal/theme.json"

require_file "$TOKENS_SRC"
require_file "$WEB_SRC"
require_file "$THEME_PY_SRC"
require_json "$THEME_JSON_SRC"

TOKENS_VER="$(pkg_version "$DS/packages/tokens/package.json")"
WEB_VER="$(pkg_version "$DS/packages/web/package.json")"
TERM_VER="$(pkg_version "$DS/packages/terminal/package.json")"
info "design-system versions: tokens=$TOKENS_VER web=$WEB_VER terminal=$TERM_VER"

# git_sha is best-effort provenance: "unknown" when git is missing or $DS is
# not a checkout, so the manifests are always written even when provenance
# is partial. (The manifest writer receives it as an argument.)
if command -v git >/dev/null 2>&1 && GIT_SHA="$(git -C "$DS" rev-parse HEAD 2>/dev/null)"; then
  info "design-system: $DS @ $GIT_SHA"
else
  GIT_SHA="unknown"
  warn "git SHA unavailable for $DS (git missing or not a checkout) — manifests will record git_sha='unknown'"
fi

# ---------------------------------------------------------------- manifest helpers
# Manifests carry a `copied_at` timestamp, but rewriting them on every run
# dirties the git tree even when nothing changed. Each manifest ENTRY is
# only written when its content minus `copied_at` differs from what is on
# disk (cron re-runs stay clean).
entry_json() {
  # $1 = package name ("" to omit the field), $2 = version,
  # $3 = source rel path — emits one manifest entry JSON object.
  python3 - "$1" "$2" "$3" "$GIT_SHA" <<'PYEOF'
import datetime, json, sys
pkg, version, src, sha = sys.argv[1:5]
off = datetime.datetime.now().astimezone().utcoffset()
sign = "+" if off >= datetime.timedelta(0) else "-"
off = abs(off)
hh, mm = divmod(int(off.total_seconds() // 60), 60)
now = datetime.datetime.now().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S") + f"{sign}{hh:02d}:{mm:02d}"
entry = {}
if pkg:
    entry["package"] = pkg
entry.update({"version": version, "source_path": src,
              "copied_at": now, "git_sha": sha})
print(json.dumps(entry))
PYEOF
}

# The manifest-merge python is written once to a temp file (a heredoc inside
# $(...) confuses the parser on this bash) and invoked per entry below.
UPSERT_PY="$(mktemp)"
cat >"$UPSERT_PY" <<'PYEOF'
import json, os, sys
path, key = sys.argv[1], sys.argv[2]
entry = json.loads(sys.argv[3])
def sans(node):
    if isinstance(node, dict):
        return {k: sans(v) for k, v in node.items() if k != "copied_at"}
    if isinstance(node, list):
        return [sans(v) for v in node]
    return node
old = {}
if os.path.exists(path):
    try:
        old = json.load(open(path))
    except (json.JSONDecodeError, OSError):
        old = {}
if not isinstance(old, dict):
    old = {}
merged = dict(old)
merged[key] = entry
if sans(old.get(key)) == sans(entry):
    print(f"sync-design-system: manifest entry '{key}' unchanged (ignoring copied_at) — kept {path}")
else:
    with open(path, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    print(f"sync-design-system: wrote manifest entry '{key}' -> {path}")
PYEOF

MANIFEST_WRITES=0
manifest_upsert() {
  # $1 = manifest path, $2 = entry key, $3 = entry JSON — merge the entry,
  # rewriting only if payload-minus-copied_at changed.
  local path="$1" key="$2" entry="$3" out
  [ -n "$entry" ] || die "empty manifest entry for '$key' ($path)"
  out="$(python3 "$UPSERT_PY" "$path" "$key" "$entry")" \
    || die "failed to update manifest $path"
  echo "$out"
  case "$out" in
    *"wrote manifest entry"*) MANIFEST_WRITES=$((MANIFEST_WRITES + 1)) ;;
  esac
}

# ---------------------------------------------------------------- web dashboard (per artifact: copy -> verify -> manifest)
mkdir -p "$VENDOR"
sync_web_css() {
  # $1 = entry key (= dest filename), $2 = source rel path under $DS,
  # $3 = package name, $4 = version
  local key="$1" rel="$2" pkg="$3" ver="$4"
  local src="$DS/$rel" dest="$VENDOR/$key" entry
  cp "$src" "$dest"
  diff "$dest" "$src" || die "byte-identity check failed: $dest differs from $src"
  entry="$(entry_json "$pkg" "$ver" "$rel")"
  manifest_upsert "$WEB_MANIFEST" "$key" "$entry"
  info "$key: copied, byte-identical, manifest entry refreshed"
}
sync_web_css "veto-tokens.css" "packages/tokens/dist/tokens.css" "@veto/tokens" "$TOKENS_VER"
sync_web_css "veto-web.css" "packages/web/dist/veto-web.css" "@veto/web" "$WEB_VER"

# ---------------------------------------------------------------- terminal (per artifact: copy -> verify -> manifest)
# Never clobber another agent's work: if the files already exist, verify
# they match the expected vendored content and leave them alone.
patch_theme_path() {
  # $1 = file to patch (flat root vendoring: theme.json lives next to the module)
  python3 - "$1" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
old = '''_THEME_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "theme.json")
)'''
new = '_THEME_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.json")'
if new in src:
    print("already patched")
    sys.exit(0)
assert old in src, "_THEME_PATH block not found — module layout changed, patch manually"
open(path, "w").write(src.replace(old, new))
print("patched _THEME_PATH for flat vendoring")
PYEOF
}

tmp="$(mktemp)"
trap 'rm -f "$tmp" "$UPSERT_PY"' EXIT
cp "$THEME_PY_SRC" "$tmp"
patch_theme_path "$tmp" >/dev/null || die "could not patch _THEME_PATH in $THEME_PY_SRC copy"

if [ -e "$REPO/veto_theme.py" ]; then
  info "veto_theme.py already exists — verifying byte-identity against patched source (no clobber)"
  cmp "$tmp" "$REPO/veto_theme.py" \
    || die "veto_theme.py exists but differs from expected vendored content; refusing to overwrite. Sync with the terminal wiring owner."
  info "veto_theme.py matches expected vendored content — kept as-is"
else
  cp "$tmp" "$REPO/veto_theme.py"
  cmp "$tmp" "$REPO/veto_theme.py" \
    || die "byte-identity check failed after copying veto_theme.py"
  python3 -m py_compile "$REPO/veto_theme.py" || die "vendored veto_theme.py failed py_compile"
  info "vendored veto_theme.py (with flat-layout _THEME_PATH patch)"
fi
manifest_upsert "$TERM_MANIFEST" "veto_theme" \
  "$(entry_json "" "$TERM_VER" "packages/terminal/python/veto_theme.py")"

if [ -e "$REPO/theme.json" ]; then
  cmp "$REPO/theme.json" "$THEME_JSON_SRC" \
    || die "theme.json exists but differs from $THEME_JSON_SRC; refusing to overwrite. Sync with the terminal wiring owner."
  info "theme.json already exists and matches source — kept as-is"
else
  cp "$THEME_JSON_SRC" "$REPO/theme.json"
  cmp "$REPO/theme.json" "$THEME_JSON_SRC" \
    || die "byte-identity check failed after copying theme.json"
  info "vendored theme.json"
fi
manifest_upsert "$TERM_MANIFEST" "theme.json" \
  "$(entry_json "" "$TERM_VER" "packages/terminal/theme.json")"

# Import smoke-test of the VENDORED module (not the monorepo source): proves
# the patched _THEME_PATH resolves against the vendored theme.json. The
# monorepo pytest suite only covers the package-layout source, so without
# this the patched destination file is never import-tested.
if [ -f "$REPO/veto_theme.py" ]; then
  info "import smoke-test of vendored veto_theme.py..."
  python3 - "$REPO" <<'PYEOF'
import sys
repo = sys.argv[1]
sys.path.insert(0, repo)  # ensure the vendored copy wins over any installed one
import veto_theme
assert veto_theme._THEME_PATH.startswith(repo), \
    "vendored module resolved theme.json outside the repo: " + veto_theme._THEME_PATH
print("vendored veto_theme import OK;",
      len(veto_theme.THEME["roles"]), "roles;",
      "paint('success', 'ok') ->", repr(veto_theme.paint("success", "ok")))
PYEOF
fi

# ---------------------------------------------------------------- link check: index.html must reference the vendored CSS
[ -f "$INDEX_HTML" ] || die "missing: $INDEX_HTML"
for css in veto-tokens.css veto-web.css; do
  grep -Eq '<link[^>]*href="[^"]*vendor/'"$css"'"' "$INDEX_HTML" \
    || die "webui/index.html has no <link> reference to vendor/$css"
done
info "index.html references both vendored stylesheets"

# ---------------------------------------------------------------- cross-consistency: site file: deps vs vendored manifests
# The site consumes @veto/tokens and @veto/web via file: deps while webui/
# consumes vendored copies tracked by manifests — resolve the file: targets
# and require the versions to match what the manifests record.
if [ -f "$SITE_PKG" ]; then
  python3 - "$SITE_PKG" "$WEB_MANIFEST" <<'PYEOF' \
    || die "site file: deps resolve to different @veto/* versions than the vendored manifests"
import json, os, sys
site_pkg, manifest_path = sys.argv[1], sys.argv[2]
site = json.load(open(site_pkg))
deps = {}
deps.update(site.get("dependencies") or {})
deps.update(site.get("devDependencies") or {})
manifest = json.load(open(manifest_path))
for key, pkg in (("veto-tokens.css", "@veto/tokens"),
                 ("veto-web.css", "@veto/web")):
    entry = manifest.get(key)
    if not entry:
        sys.exit(f"manifest {manifest_path} has no entry for '{key}'")
    spec = deps.get(pkg)
    if spec is None:
        print(f"sync-design-system: WARNING: site has no dependency on {pkg}; skipping cross-check")
        continue
    if not spec.startswith("file:"):
        print(f"sync-design-system: WARNING: site {pkg} is '{spec}', not a file: dep; skipping cross-check")
        continue
    target = os.path.normpath(os.path.join(os.path.dirname(site_pkg), spec[len("file:"):]))
    try:
        resolved = json.load(open(os.path.join(target, "package.json")))
    except OSError as e:
        sys.exit(f"cannot read package.json under site {pkg} file: target {target}: {e}")
    rv, mv = resolved.get("version"), entry.get("version")
    if rv != mv:
        sys.exit(f"version mismatch for {pkg}: site file: dep resolves to {rv}, vendored manifest says {mv}")
    print(f"sync-design-system: site {pkg} -> {target} @ {rv} matches manifest")
PYEOF
else
  warn "no $SITE_PKG — skipping site file: dep cross-check"
fi

if [ "$MANIFEST_WRITES" -eq 0 ]; then
  info "manifests unchanged — nothing rewritten"
fi

info "sync complete"
