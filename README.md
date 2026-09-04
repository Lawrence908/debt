# Debt Atlas

AI corporate debt, US federal debt and North American household balance sheets on common axes.
Live at [debt.chrislawrence.ca](https://debt.chrislawrence.ca).

No framework, no build step, no package manager. Plain HTML, CSS and vanilla JS with `fetch`.
It has to still work in three years when nobody has run an install in as long.

## Layout

```
src/index.html   markup, styling and every render function
data/*.json      curated figures, human-owned, five-field contract
data/series.json machine-fetched long series, never hand-edited
api/server.py    updater and read-only status API
```

## The one rule

**The page contains no figures.** Every number renders from `data/`, prose included. A sentence
states a figure by token, `{us_gdp}`, never by literal, so it cannot drift from the value it
describes. An unknown token renders visibly rather than vanishing, so a typo surfaces on the page.

## Data contract

Every value carries five fields:

```json
{
  "value": 18.794,
  "unit": "USD_trillions",
  "as_of": "2026-Q2",
  "confidence": "reported",
  "source": "Federal Reserve Bank of New York",
  "source_url": "https://..."
}
```

`confidence` is `reported` (filed or official), `estimate` (third-party reconstruction) or
`projection` (a model). It drives colour and hatching, so mislabelling a projection as reported
is a correctness bug, not a cosmetic one. A value missing `as_of` or `source_url` renders a
visible warning rather than rendering clean.

Where a figure is contested, the disagreement is the content. A contradiction goes in a
`disputed` sibling field and surfaces on the page; nothing is silently overwritten.

## Curated versus fetched

Two different owners, two different failure modes, so they live apart.

`data/*.json` is the spec. Small, diffable, reviewed by a human. `data/series.json` holds
thirteen FRED series and roughly 2,900 observations, is rewritten wholesale on every run, and is
gitignored. Putting a 241-point quarterly array in a curated file would bury every real edit.

## The updater

```bash
docker exec debt-updater python /app/server.py --once            # dry run
docker exec debt-updater python /app/server.py --once --write    # apply
docker exec debt-updater python /app/server.py --series          # refresh long series
```

Four guardrails, each of which has already caught something real:

1. **Existing values only.** A new series is a human decision; the updater cannot create one.
2. **Only `value` and `as_of` are written.** `source`, `source_url`, `confidence`, `unit` and
   `note` survive a run untouched. A refreshed figure additionally gets `updated_by`, recording
   which endpoint produced the number now on screen, so traceability survives automation.
3. **A change beyond 20% is refused and logged.** These series do not move that fast, so a jump
   that large means an endpoint changed, not that the world did.
4. **`as_of` never moves backwards.** FRED's `GFDEGDQ188S` trails the ratio computed from
   Treasury debt over BEA's advance GDP by a quarter. Without this check the first scheduled run
   would have quietly downgraded a current figure to an older one and reported success.

`UPDATER_WRITE` is off by default, so a fresh deploy observes and logs for a cycle before it is
trusted to edit files that took real research to assemble.

## What cannot be automated

Treasury, FRED and the BIS household series refresh on a schedule. The AI capital figures come
from securities filings, ratings notes and press reporting: they need something that can read,
not something that can poll. `ai-capital.json` carries `last_reviewed`, and past 90 days the page
says so. Honest staleness beats a number that looks fresh because a cron touched the file.

StatCan stays manual. Guessing a WDS vector ID produces a plausible wrong number, which is worse
than an obvious gap.

## Known gaps

- Measure C of the AI section cannot honestly be drawn as a time series and is not. Every sourced
  value falls inside a nine-month window yet they span $662B to $3,000B, and Bank of America and
  Nikkei cover the identical five companies one month apart while differing by 39%. The variation
  is definitional, so the points are ordered by definitional breadth instead of by date.
- AI debt outstanding has no sourced value before late 2025. Nobody back-tagged the index, so the
  series starts there rather than being filled in.
- The Anthropic run rate stays a range while sources conflict. Collapsing it would make the table
  tidier and the page less true.

## Provenance

Assembled with Claude, made by Anthropic. The page reports on Anthropic among other companies.
Those figures come from the same third-party sources as everything else, but the conflict of
interest is worth stating rather than burying.
