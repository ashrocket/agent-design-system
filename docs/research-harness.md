# Budgeted nightly research

The research loop is optional and deliberately outside the enforcement engine.
An ADSF check remains deterministic, offline, and free to run.

## Lifecycle

1. `adsys research plan` reads the append-only ledger, checks UTC daily,
   calendar-week, and calendar-month windows, enforces the per-day run count,
   observes question cooldowns, and reserves the next question's declared cost.
2. A runner gives the generated read-only prompt and result schema to an agent.
3. `adsys research record` validates identity and basic result structure, writes
   an immutable result envelope, and appends a ledger entry.
4. Every attempt is charged the greater of reserved tokens or measured usage.
   Failed, empty, and malformed runs are still charged their reservation.

Recording is idempotent by run ID. Retrying a publish job returns the existing
ledger entry rather than charging the same agent attempt twice.

The preflight reservation is the hard local gate. It prevents starting work that
would exceed a configured rolling window, but it is not an OpenAI account spend
limit. Since an individual agent run can exceed its estimate, set reservations
conservatively and configure provider-side project spend controls as a second
boundary when using API billing.

## Local runner

Preview without spending budget:

```sh
adsys research run --dry-run
```

Run once with an authenticated Codex CLI:

```sh
adsys research run
```

The runner uses `codex exec --ephemeral --sandbox read-only --json` with low
reasoning effort and low response verbosity, requests a schema-constrained final
result, and parses the `turn.completed` usage event. The official Codex
non-interactive documentation describes JSONL events and usage:
<https://developers.openai.com/codex/non-interactive-mode>.

Use cron, launchd, or another scheduler to call the command once nightly. The
command exits successfully without invoking Codex when a budget or cooldown gate
says `skip`.

## GitHub workflow

`.github/workflows/nightly-research.yml` uses `openai/codex-action` in a
read-only job, following the official GitHub Action guidance:
<https://developers.openai.com/codex/github-action>.

The key-bearing job has only `contents: read`; the final Codex step uses a
read-only sandbox and cannot publish. A later job, which never receives the API
key, records the reserved charge and pushes only the ledger/result envelope to
`automation/research-state`. That branch backs an open pull request, so research
never changes the default branch without human review.

Repository setup requires one `OPENAI_API_KEY` Actions secret and workflow
permissions allowing the GitHub token to create pull requests. A scheduled run
is not proven operational until the workflow, state-branch commit, and pull
request are observed on the remote repository.
