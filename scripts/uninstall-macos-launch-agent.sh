#!/bin/sh
set -eu

label="dev.adsf.nightly-research"
domain="gui/$(id -u)"
target="$HOME/Library/LaunchAgents/$label.plist"

launchctl bootout "$domain/$label" >/dev/null 2>&1 || true

if [ -f "$target" ]; then
  trash_dir="$HOME/.Trash"
  mkdir -p "$trash_dir"
  destination="$trash_dir/$label.$(date -u +%Y%m%dT%H%M%SZ).plist"
  mv "$target" "$destination"
  echo "Moved LaunchAgent to $destination"
else
  echo "LaunchAgent was not installed at $target"
fi
