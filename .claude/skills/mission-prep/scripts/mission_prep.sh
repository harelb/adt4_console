#!/usr/bin/env bash
# Run `python -m adt4_console.mission_prep <compile|render|verify> ...` with what it needs:
# this adt4_console checkout on PYTHONPATH and a venv with requirements.txt (created on
# first use). Override: MISSION_PREP_PYTHON=/path/to/python.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
console="$(cd "$here/../../.." && pwd)"   # repo root: .claude/skills/mission-prep/../../..
py="${MISSION_PREP_PYTHON:-}"
if [[ -z "$py" ]]; then
  venv="${XDG_CACHE_HOME:-$HOME/.cache}/adt4_mission_prep_venv"
  if [[ ! -x "$venv/bin/python" ]] || ! cmp -s "$here/requirements.txt" "$venv/.requirements.txt"; then
    echo "setting up $venv ..." >&2
    python3 -m venv "$venv"
    "$venv/bin/pip" install -q -r "$here/requirements.txt"
    cp "$here/requirements.txt" "$venv/.requirements.txt"
  fi
  py="$venv/bin/python"
fi
PYTHONPATH="$console${PYTHONPATH:+:$PYTHONPATH}" exec "$py" -m adt4_console.mission_prep "$@"
