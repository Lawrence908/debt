# Debt Atlas

Static page comparing AI-related corporate debt, US federal debt, and US/Canadian household debt on common axes. No build step, no dependencies, no framework.

```
index.html          all markup, styling and render logic
data/*.json         every number on the page
```

## Run it

```bash
python3 -m http.server 8080
```

Must be served over HTTP. Opening `index.html` directly will fail the `fetch` calls on CORS.

## The one rule

**The page contains no figures.** If you find yourself typing a number into `index.html`, it belongs in a data file instead. Prose in the render functions interprets the data; it should not restate values that the data layer already holds. Where prose currently repeats a figure, that is a known weakness — those are the lines that will silently go stale first, and moving them to interpolation is a good first task.

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

`confidence` is one of `reported` (filed or official), `estimate` (third-party reconstruction), `projection` (a model). It drives colour and hatching in the page, so it is not decorative — mislabelling a projection as reported changes what a reader sees.

Supported units: `USD_trillions`, `USD_billions`, `CAD_trillions`, `CAD_billions`, `USD`, `percent`, `percent_of_gdp`, `ratio`. Add new ones to `money()` in `index.html`.

## Update cadence

| File | Cadence | Automatable |
|---|---|---|
| `federal-debt.json` | daily level, quarterly ratio | yes — Treasury Fiscal Data + FRED |
| `gdp.json` | quarterly | yes — FRED `GDP`, StatCan WDS |
| `household-debt.json` | quarterly | partly — NY Fed publishes PDF/XLSX, StatCan has an API |
| `ai-capital.json` | quarterly + ad hoc | no |
| `cycles.json` | rarely | no |

Endpoints are in `data/meta.json` under `api_hooks`. Treasury and FRED can run unattended. The AI capital figures come from SEC filings, ratings notes and press reporting — they need something that can read, not something that can poll.

## Known gaps

- Historical debt-to-GDP points for 1980, 1990, 2000, 2012 and 2024 are eyeballed off the FRED chart. Replace with exact quarterly observations. Flagged in `federal-debt.json` as `gap_note`.
- `FX_CAD_USD` is hardcoded at the top of the script. A 5% CAD move changes the Canada bars more than a year of growth does.
- Anthropic run rate is a range because sources conflict ($47B May, $65B July). Resolve when the S-1 becomes public.
- Household debt figures for the US and Canada come from structurally different collections. The debt-to-GDP comparison is the closest to like-for-like; treat the rest as directional.

## Extending it

Good next additions, roughly in order of value:

1. Wire the FRED and Treasury pulls to a cron, writing back into the JSON files. This removes the largest source of staleness for free.
2. Add a `history` array to the AI capital measures so the three-measure gap can be charted over time rather than shown as a snapshot. The widening of that gap is the actual story.
3. Per-capita normalisation for the household section — currently aggregate only, which understates how differently the debt is distributed.
4. A diff view showing what changed between builds. Useful precisely because the AI figures move fast and the sovereign ones do not.

## Provenance

Assembled with Claude, made by Anthropic. The page reports on Anthropic among other companies; those figures come from the same third-party sources as everything else, but the conflict of interest is worth stating rather than burying.
