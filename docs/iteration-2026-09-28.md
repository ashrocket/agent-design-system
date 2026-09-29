# Assistant design-system enforcement: evening iteration

## Goal and context

Keep LLM coding assistants inside an approved design system while editing actual
application source. The previous tool enforced supplied component JSON, which
could pass without inspecting an assistant's CSS. The user requested iterative
work, thorough validation, and a panel evaluating goals, plan and results.
They also authorized committing all existing research and tonight's work.

## Constraints and done evidence

Enforcement stays deterministic, offline and dependency-free on Python 3.11+.
Check preserves inspected files; fix requires an explicit destination or `--write`.
The research harness remains separate. Preserve existing research attempts and
their budget charges. A milestone needs real source drift blocked, exact repairs
rechecked, preserved input bytes, idempotence, adversarial bypass tests, schema
validation, original conformance, focused static checks and a reviewed diff.

## Panel evaluation of goals and plan

Three independent Codex reviewers examined semantics, safety and the assistant
workflow. All agreed the primary missing capability was checking actual source.
They approved a bounded CSS milestone with these requirements:

- Explicit property-specific allowed tokens and variable bindings.
- Actual source locations and inspected coverage.
- Rejection of unknown variables, fallbacks, local token shadowing, syntax gaps,
  shorthand/unknown-property bypasses, and empty coverage.
- Exact-only, unambiguous repairs followed by fresh enforcement.
- Assistant context identifying the approved contract and exact gate.
- Explicit limits: CSS does not establish component reuse or whole-app compliance.

The safety/semantics reviews also reproduced input/output/log alias corruption,
successful exits for still-invalid repairs, lost null replacements, overlapping
repair crashes, imprecise integer comparisons, duplicate recursive matches and
ambiguous token keys. These were corrected with regression and conformance cases.
The first re-review caught a reject-decision regression and symlink edge cases;
those were corrected before the enforcement-safety commit.

## Implemented checkpoints

- `f86c259`: existing nightly attempts and conservative budget accounting committed.
  The records describe failed attempts, not completed research findings.
- A subsequent 29 September interoperability attempt also failed and charged
  its 9,000-token reservation. Its recorded raw output reports a Pidgin OAuth
  refresh `invalid_grant` inside the research agent. This is no research finding.
- `4c018a3`: enforcement/write safety corrections committed after 54 unit tests,
  9 normative cases and the example validate/check/fix/recheck flow.
- The first CSS source gate checks authored declarations against property-specific
  token references, emits assistant context and a generated token sheet, proposes
  only exact repairs, and reports source locations and inspected coverage. The
  CI workflow runs its real-source integration verifier. The latest local pass
  had 77 unit tests, 9 normative cases, Ruff checks and mypy on Python 3.11;
  the example validate/check/fix/recheck sequence also passed. No remote CI run
  is claimed.
- A panel probe found that excessive nested grouping caused an uncaught
  `RecursionError`. A depth limit now converts it to a source error, with direct
  and CLI regressions. Exported assistant examples are checked against actual
  enforcement; SARIF declares Unicode code-point columns. The source reviewer
  could inspect relayed parser code but could not run an independent checkout
  because Codex's shared daemon exhausted its 256-file-handle soft limit.

## Remaining iterations

1. Finish the independent CSS panel re-review as process headroom allows.
2. Add a fixture app, project-wide source coverage manifest and generated-token
   parity checks. A single assistant-selected CSS path remains an omission gap.
3. Detect unsupported inline/utility/runtime styling surfaces instead of allowing
   uninspected files to imply complete source compliance.
4. Add component/semantic constraints where evidence supports useful adapters.
5. Separately harden malformed research event/result handling and persist
   reservations before subprocess execution. Current failed records were preserved;
   the panel found interruption/untrusted-payload charging gaps in the runner.

Jev was unavailable in this session (no exposed tool or API credential), so the
panel used direct code evidence. The MacBook worker readiness snapshot reported
offline; these validation results were local, not worker results.

The file-handle diagnosis points to the shared Codex app-server daemon, not the
repository tests. It held 249-254 numbered descriptors near a 256 soft limit,
including many connector pipes, session files and database handles. This
establishes process-local pressure but not whether retained idle sessions or a
daemon leak is the root cause. No peer session was terminated.
