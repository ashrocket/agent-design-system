# Local nightly scheduling on macOS

The local scheduler uses the authenticated Codex CLI, so it does not require an
API key. It runs at 03:17 local time and delegates every spend decision to the
same daily, UTC calendar-week, and UTC calendar-month ledger gates used by the
rest of the harness.

Install and load the LaunchAgent:

```sh
./scripts/install-macos-launch-agent.sh
```

The installer resolves absolute Python, Codex, and repository paths; renders a
plist with the standard-library CLI; validates it with `plutil`; backs up an
existing plist; and prints the loaded launchd service. It does not run research
immediately. Existing daily budget state still applies to the next scheduled
run.

Logs are written to `.research/logs/`. Inspect the loaded job with:

```sh
launchctl print "gui/$(id -u)/dev.adsf.nightly-research"
adsys research status
```

Uninstall reversibly:

```sh
./scripts/uninstall-macos-launch-agent.sh
```

The uninstaller stops the job and moves its plist to Trash. It preserves the
research ledger, results, logs, and project checkout.

Do not set `ADSF_ENABLE_GITHUB_SCHEDULE=true` while this local scheduler is
loaded unless both runners use a shared authoritative ledger. Separate
checkouts cannot see each other's reservations and could both spend budget.
