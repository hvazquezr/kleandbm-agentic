#!/usr/bin/env bash
# Regenerate backend/pylock.toml from the versions installed in the kleandbm-agentic conda env.
#
# The lock pins exact versions with hashes for the direct dependencies in pyproject.toml and
# everything they pull in. It records what the env has *now*, so update and test the env first
# (see "Dependencies" in CLAUDE.md), then run this and commit pyproject.toml and pylock.toml.
#
#   conda run -n kleandbm-agentic bash scripts/lock_dependencies.sh
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Pin the resolver to what's installed, so the lock matches the env the tests ran on.
python -m pip list --format=freeze --exclude-editable | grep -v '^kleandbm' > "$tmp/constraints.txt"
# Direct dependencies (runtime and dev) from pyproject.toml; the project itself isn't locked,
# because pip can't hash a local directory.
python - > "$tmp/direct.txt" <<'PY'
import tomllib
project = tomllib.load(open("pyproject.toml", "rb"))["project"]
print("\n".join(project["dependencies"] + project["optional-dependencies"]["dev"]))
PY
python -m pip lock -q -c "$tmp/constraints.txt" -r "$tmp/direct.txt" -o pylock.toml
echo "Wrote pylock.toml ($(grep -c '^\[\[packages\]\]' pylock.toml) packages)"
