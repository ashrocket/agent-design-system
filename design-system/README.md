# ADSF design system

ADSF is an open, vendor-neutral format for design rules. Its core is a decision tree with four outcomes: accept, report, coerce and reject. The identity uses that tree as its one visual idea: four verdict colours, a plus grid and plain type, because a standard for checkable rules should look precise and neutral. The landing page is the README front page, and its job is to state the purpose and send the reader to the format.

## Voice

1. State the claim in one sentence. Example: "ADSF is an open format for design systems that both agents and ordinary test tools can understand."
2. Name the outcome of each rule. Example: "The reference `adsys` CLI can report violations, propose or apply safe coercions, reject unsafe values, emit SARIF, and keep an append-only audit log."
3. Say what the tool does not do. Example: "...and keep an append-only audit log—without calling a model."
4. Use the real command names. Example: "Export the approved contract with `adsys context`."
5. Say what is not proven. Example: "Status: early alpha. The core mechanics work and are tested; ADSF 0.1 is a proposal, not yet a community standard."

## Colour

| Token | Value | Role | Contrast on `ground` |
|---|---|---|---|
| `ground` | #ffffff | Page ground | Ground for all text |
| `ink` | #1a1d21 | Body text, wordmark | 16.91:1 |
| `muted` | #59606b | Secondary text | 6.34:1 |
| `rule` | #d9dce1 | Hairlines, plus-grid pattern | Not used for text |
| `verdict-accept` | #1e6b47 | Accept word and mark | 6.46:1 |
| `verdict-report` | #1f5fad | Report word and mark | 6.37:1 |
| `verdict-coerce` | #8a5a00 | Coerce word and mark | 5.93:1 |
| `verdict-reject` | #b42318 | Reject word and mark | 6.57:1 |

Every verdict also carries its word. Colour never carries the meaning alone.

## Type

- Sans: Public Sans, weights 400 and 700. Open-source neutral face. Hosted on Google Fonts: https://fonts.google.com/specimen/Public+Sans (no font file in the system).
- Code: Source Code Pro, weight 400. Hosted on Google Fonts: https://fonts.google.com/specimen/Source+Code+Pro.
- Fallback stacks: `system-ui, sans-serif` and `ui-monospace, monospace`.

| Style | Size / line | Weight | Used for |
|---|---|---|---|
| display | 96px / 96px | 700 | Wordmark in the hero and on the cover |
| heading | 32px / 40px | 700 | Section headings, component titles |
| body | 18px / 28px | 400 | Sub-line, running text |
| small | 14px / 22px | 400 | Taglines, captions, verdict meanings |
| label | 14px / 22px | 700 | Verdict words, button labels |
| code | 14px / 22px | 400 (mono) | Command lines and code |

## Layout and spacing

- Content maximum width: 960px. Hero and components are width-fluid below that.
- Side gutter: 16px on phone widths.
- Spacing steps: 8px, 16px, 24px, 48px (`space-1` to `space-4`).
- Plus-grid pitch: 24px (`space-3`).
- Radii: 0px (`radius-none`), 2px (`radius-sm`, buttons), 4px (`radius-md`, verdict marks).

## Logo and wordmark

No logo: wordmark is plain type. The project holds no logo file, so none is copied into `assets/Logos/`.

- Text: "ADSF", capitals, Public Sans 700, tracking 0.
- Colour: `ink` on `ground`. Use `verdict-*` colours only for verdict words.
- Clear space: at least one cap height on every side.
- Minimum size: 16px cap height in print or on screen. Below that, use the word "ADSF" in `label` style.
- Do not: redraw the letters, add a gradient or shadow, set the wordmark in another face, or place it on a coloured block that is not `ground` or `ink`.
- Full name: "Agent Design System Format". Use it in running text. Use "ADSF" in the wordmark.

## Landing page anatomy

1. Hero: wordmark "ADSF" in `display`, the purpose sentence in `body`, and one action, "Read the format", styled as a `label` button on `ink` with `radius-sm`.
2. Verdict: the four verdict states in order, accept, report, coerce, reject. Each row is a verdict word in its colour, a meaning in `small`, and a `rule` hairline between rows.

## Status

new

This identity has no prior source. Each value above is a new choice from the owner-approved brief. Usage notes in `tokens.json` say "new" in place of a source citation.

## Replaces

The navy and `#2563eb` look of the two research briefs, which used Georgia headings and a card layout:

- `docs/rendered-react-research-brief-2026-09-29.html:9` (blue accent, navy ink)
- `docs/design-system-research-brief-2026-09-29.html:8` (second ink value, `#1e293b`)
