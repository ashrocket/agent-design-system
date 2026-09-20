#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_path=${PYTHON_PATH:-$(command -v python3)}
codex_path=${CODEX_PATH:-$(command -v codex)}
launch_agents_dir="$HOME/Library/LaunchAgents"
target="$launch_agents_dir/dev.adsf.nightly-research.plist"
label="dev.adsf.nightly-research"
domain="gui/$(id -u)"

mkdir -p "$launch_agents_dir" "$project_dir/.research/logs"

if [ -f "$target" ]; then
  backup="$target.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$target" "$backup"
  echo "Backed up existing LaunchAgent to $backup"
fi

PYTHONPATH="$project_dir/src" "$python_path" -m agent_design_system \
  research render-macos-scheduler \
  --repository "$project_dir" \
  --python "$python_path" \
  --codex "$codex_path" \
  --output "$target"

plutil -lint "$target"
launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
launchctl bootstrap "$domain" "$target"
launchctl enable "$domain/$label"
launchctl print "$domain/$label"
