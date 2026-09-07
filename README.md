# Debt Atlas

AI corporate debt, US federal debt and North American household balance sheets on common axes.
Live at [debt.chrislawrence.ca](https://debt.chrislawrence.ca).

No framework, no build step, no package manager. Plain HTML, CSS and vanilla JS with `fetch`.
It has to still work in three years when nobody has run an install in as long.

## Layout

```
src/index.html        markup, styling and every render function
data/*.json           curated figures, human-owned, five-field contract
data/series.json      machine-fetched long series, never hand-edited
data/recessions.json  vendored from econ-core, never edited here
api/server.py         updater and read-only status API
api/econcore.py       vendored from econ-core, never edited here
```

## Shared standards

The long series follow the [econ-core](../econ-core) contract, which exists so
that when `econ` eventually overlays diesel, debt and jobs on common axes, three
sites do not need refactoring first. Each series carries `id`, `label`, `source`,
`source_url`, `units`, `freq`, `confidence`, `as_of` and `[date, value]` pairs,
and is assembled by `econcore.make_series`, which validates and raises rather
than writing something malformed.

Series are keyed by a stable snake_case id, not by the FRED mnemonic: the
overlay keys on the id, and FRED's mnemonics are neither stable nor shared
across sources. Where a series names the same quantity as a curated figure it
deliberately reuses that figure's id *and* its unit, so one id cannot mean two
things. `us_gdp_nominal` is the same quantity in `gdp.json` and in
`series.json`; one is the latest reading, the other the whole history.

`econcore.py` and `recessions.json` are copies, not imports. Every app in this
collection has to keep working in three years with no installs run in as long,
so there is no shared runtime path to rot. Update them deliberately:

```bash
cd ../econ-core && ./vendor.sh ../debt
```

The stamp at the top of each says exactly which revision this app got. Do not
edit either in place; edit econ-core and re-vendor.

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
thirteen FRED series and roughly 2,900 observations in the econ-core contract shape, is rewritten
wholesale on every run, and is gitignored. Putting a 241-point quarterly array in a curated file
would bury every real edit.

`data/recessions.json` is a third category: machine-generated, but upstream of this repo and
tracked rather than gitignored, because it changes about once per business cycle and the stamp
should travel with the commit.

## How the page gets its data

One endpoint, not seven static files:

```
GET /api/data -> meta, gdp, federal-debt, ai-capital, household-debt, cycles,
                 recessions, series, derived, changelog, econcore,
                 series_errors, generated_at
```

The payload is composed **from disk**, cached on the newest mtime across the data directory.
That is the one deliberate difference from diesel, which serves its payload from memory. Here the
refresh runs outside the process via `docker exec`, so an in-memory payload would keep serving
superseded figures until the container restarted while the files on disk were already current:
stale numbers behind a healthy endpoint, which is exactly the failure this project exists to
avoid. A cron write is visible on the next request, no restart needed.

`data/` is still mounted into nginx so a single raw file can be inspected with `curl`, but
nothing links to it.

## The updater

```bash
docker exec debt-updater python /app/server.py --refresh          # what cron runs
docker exec debt-updater python /app/server.py --once             # dry run
docker exec debt-updater python /app/server.py --once --write     # apply curated only
docker exec debt-updater python /app/server.py --series           # long series only
```

The schedule lives in the host crontab, not in the container, matching every other scheduled job
on this box:

```cron
15 6 * * * docker exec debt-updater python /app/server.py --refresh >> /home/chris/logs/debt-updater.log 2>&1
7 4 1 * * : > /home/chris/logs/debt-updater.log
```

One scheduler, visible from `crontab -l`. Retry cadence is the cron cadence: a failed run is
logged and the next tick tries again. The second line exists because nothing rotates
`/home/chris/logs`, so a daily append would grow without bound.

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

The long series are machine-owned and rewritten wholesale, so they follow different rules: nothing
is preserved across a run because nothing there is hand-authored. What is guarded instead is that
the file never comes back worse than the one it replaces.

1. **A series that fails to fetch is carried forward** from the last good run rather than
   disappearing until the next one succeeds.
2. **A series returning under 90% of the stored observations is refused.** A truncated response is
   indistinguishable from a real series until you compare lengths.
3. **A run with nothing fetched and nothing stored refuses to write**, rather than replacing the
   file with an empty one.
4. **Every already-published observation an upstream restates is diffed and logged.** A new reading
   at the end of a series is not a revision; history changing underneath a number the page has
   already shown is, and it happens quietly.

Curated and series records share `data/changelog.jsonl` and are rendered apart on the page.

### The FRED User-Agent tarpit

Everything touching FRED goes through `econcore`, and the keyless CSV endpoint is asked with
urllib's honest default User-Agent. This is load-bearing. `fred.stlouisfed.org` tarpits
User-Agents it does not recognise as a known tool: it answers curl, wget and Python's default
immediately, and hangs anything else until timeout.

This module used to send its own `debt.chrislawrence.ca` UA on that path, which means the keyless
fallback documented above had in fact never worked. It was masked because `FRED_API_KEY` was set,
so the fallback was never taken. Measured 2026-09-07 against `fredgraph.csv?id=GDP`:

| User-Agent | Result |
|---|---|
| `debt.chrislawrence.ca (debt atlas updater)` | timed out at 15s |
| urllib default | 6,398 bytes in 0.2s |

A full keyless run of all thirteen series now completes in about eight seconds. Treasury has no
such filter and keeps this app's own UA.

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

## Checking the links

```bash
python3 scripts/check-sources.py
```

Walks the data, requests every distinct `source_url`, and reports status plus the final URL after
redirects. `recessions.json` is in scope like everything else, so USREC and the C.D. Howe
chronology are checked alongside the researched sources. Two link errors reached this repo before it existed, both found by hand: a page cited at
two different paths, and a bare host standing in for an endpoint.

Two things it gets right that a naive checker does not. It sends full browser headers, because
several publishers answer 403 to a bare scripted request while serving a reader fine. And it counts
bot-protected hosts separately from broken ones: `cbo.gov` serves a captcha and `ropesgray.com` a
Cloudflare interstitial to any automated client, and reporting those as failures every run would
train whoever reads the output to skim past a real one.

A redirect is treated as a canonicalisation candidate rather than a pass, since that is how the
duplicate source path was caught.
