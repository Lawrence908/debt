# Debt Atlas — build plan

You are building and deploying a static research site. This document is your brief. It is written for an agent working directly on the target server.

## What you are given

```
data/*.json         ← THE SPEC. Read-only in phase 1. Do not regenerate.
index.html          ← reference implementation. Rewrite freely.
README.md           ← contributor docs. Keep current as you change things.
BUILD-PLAN.md       ← this file.
```

The six JSON files contain roughly forty researched figures on AI corporate debt, US federal debt, and US/Canadian household debt, each with a source URL, an as-of date, and a confidence level. They took real research to assemble and several encode non-obvious methodology decisions.

**Do not re-research these figures.** Do not "verify" them by searching and substituting what you find. Sources on this topic disagree by wide margins and the existing files already document those disagreements deliberately. If you believe a figure is wrong, add a sibling field `disputed: "<what you found, with URL>"` and surface it in the UI. Never silently overwrite.

## Non-negotiables

These are the constraints that make the thing maintainable. Violating any of them defeats the point of the project.

**1. No numbers in markup.** Every figure renders from `data/`. If you type a number into a template, you have created something that goes stale silently. This includes prose: a sentence reading "at $725B against $32.48T" must interpolate both values, not hardcode them.

**2. The five-field contract holds for every value.**

```json
{ "value": 18.794, "unit": "USD_trillions", "as_of": "2026-Q2",
  "confidence": "reported", "source": "...", "source_url": "https://..." }
```

A value missing `as_of` or `source_url` should render with a visible warning, not fail silently and not render clean.

**3. `confidence` is load-bearing, not decorative.** `reported` = filed or official. `estimate` = third-party reconstruction. `projection` = a model. It drives colour and hatching. A projection styled as reported is a correctness bug, not a cosmetic one.

**4. Stock-vs-flow caveat stays visible.** Corporate debt compared to GDP is a scale illustration, not a ratio. That caveat sits adjacent to the comparison, not in a footer. If you restructure the page, it moves with the chart.

**5. No framework, no build step, no npm.** Plain HTML, CSS, vanilla JS, `fetch`. It has to still work in three years when nobody has run `npm install` since. If you strongly believe a build step is warranted, say why and wait — do not add one unilaterally.

## Phase 1 — deploy as-is

Get the existing implementation running before changing anything. This gives you a known-good baseline to diff against.

- Serve the directory over HTTP. It will fail on `file://` — the `fetch` calls hit CORS.
- Put it behind whatever reverse proxy and TLS the host already uses.
- Verify all six JSON files load, all seven sections render, and no console errors.
- Check mobile at 380px width, keyboard focus visibility, and `prefers-reduced-motion`.

**Done when:** the page renders end to end from a clean checkout on the server, over HTTPS, with no console errors.

## Phase 2 — close the known gaps

In priority order. Each is independently shippable.

**2a. Replace the eyeballed historical points.** `data/federal-debt.json` → `historical.points`. Values for 1980, 1990, 2000, 2012 and 2024 were read approximately off a chart and are flagged in `gap_note`. Replace with exact quarterly observations from FRED series `GFDEGDQ188S`. Delete `gap_note` once done — and only once done.

**2b. Move hardcoded prose figures into interpolation.** The render functions in `index.html` still restate some values in sentences. Audit every `<p class="prose">` and every `caveat` block. Anything that states a number must pull it from the loaded data. This is the highest-value maintenance work in the project.

**2c. Lift `FX_CAD_USD` out of the script.** It is currently a constant at the top of the JS. Move it into `data/gdp.json` with its own `as_of` and source, and surface the rate in the UI wherever a converted figure appears. A 5% CAD move shifts the Canada bars more than a year of real growth does; a reader deserves to see which rate produced the bar.

**2d. Render the missing-field warning.** Implement the check from non-negotiable 2. Any value lacking `as_of` or `source_url` gets a visible marker.

## Phase 3 — automate what can be automated

Two of the five series can update themselves. Three cannot. Be clear-eyed about which is which.

| Series | Source | Automatable |
|---|---|---|
| Federal debt level | Treasury Fiscal Data API | yes, daily |
| Federal debt/GDP ratio | FRED `GFDEGDQ188S` | yes, quarterly |
| US GDP | FRED `GDP` | yes, quarterly |
| Canada GDP | StatCan WDS | yes, needs vector IDs |
| US household debt | NY Fed, publishes XLSX | partly — parse, don't scrape HTML |
| Canada household debt | StatCan WDS | yes, needs vector IDs |
| AI capital | SEC filings, ratings notes, press | **no** |

Endpoints are in `data/meta.json` under `api_hooks`.

Build a single updater script that fetches, validates, and writes back into the JSON files in place — preserving `source`, `source_url` and `confidence`, updating only `value` and `as_of`. Run it on a schedule. Log what changed.

**Two guardrails on the updater:**

- If a fetched value differs from the stored one by more than 20%, do not write. Log it and leave the old value. These series do not move that fast; a large jump means an API change, a units change, or a bad parse.
- Never let the updater create a value. It updates existing entries only. New series are a human decision.

**The AI capital figures are not automatable and should not be faked into looking automated.** They come from filings, ratings notes and press reporting. Give `data/ai-capital.json` a `last_reviewed` date and have the page show staleness once it exceeds ~90 days. Honest staleness beats a number that looks fresh because a cron touched the file.

## Phase 4 — the additions worth making

Roughly in order of value.

**4a. History on the three measures.** The measures currently render as a snapshot. Add a `history` array to each so the gap between reported debt, committed obligations, and off-balance-sheet obligations can be charted over time. **The widening of that gap is the actual story** and the page cannot currently show it. This is the single highest-value feature addition.

**4b. Build diff view.** Show what changed between the last two builds. Valuable precisely because the AI figures move fast and the sovereign ones barely move — a diff makes the difference in tempo legible.

**4c. Per-capita normalisation on the household section.** Currently aggregate only, which understates how unevenly the debt is distributed. The Canadian data already carries the wealth concentration figure (top 20% hold 65.7% of net worth); use it.

**4d. Resolve the Anthropic run-rate range.** Stored as `[47, 65]` because sources conflict (the $47B figure from the May Series H announcement, $65B reported for July). Collapse to a single value if the S-1 becomes public and settles it. Until then the range is the honest representation — do not pick one to make the table tidier.

## Anti-goals

Things that will make this worse. Do not do them without being asked.

- Adding a JS framework, bundler, or package manager.
- Restyling. The palette encodes confidence levels and the type is set for tabular figures. Changing it for taste breaks meaning.
- Re-researching or "correcting" the stored figures.
- Adding predictive commentary. The page reports measured values and documented projections by named institutions. It does not forecast.
- Smoothing over source disagreements. The Nikkei/WSJ scope difference, the Anthropic range, and the two definitions of federal debt are documented on purpose. They are the most useful content on the page.
- Removing the provenance disclosure. The page reports on Anthropic among other companies and was assembled with Claude. That stays stated.

## Acceptance

The project is working when:

- A quarterly data update is: run the updater, review the log, commit. No editing of HTML.
- Every figure on the rendered page can be traced to a source URL in two clicks.
- Nothing renders without a visible date attached.
- The three-measure section shows the trend, not just today.
- Someone reading it who does not know the topic comes away understanding that "AI debt" is three different numbers depending on the question — not one number they now know.
