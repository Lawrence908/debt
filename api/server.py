#!/usr/bin/env python3
"""Debt Atlas data updater and read-only status API.

Two of the five series can update themselves; three cannot. This module is
deliberately explicit about which is which, because an automated-looking figure
that is actually hand-maintained is worse than an obviously stale one.

Writes are guarded three ways:

  * only values that already exist are touched -- a new series is a human
    decision, so the updater can never create one;
  * only `value` and `as_of` are written, so `source`, `source_url`,
    `confidence`, `unit` and `note` survive a run untouched;
  * a fetched value more than MAX_CHANGE_PCT from the stored one is logged and
    dropped. These series do not move that fast, so a large jump means an API
    change, a units change or a bad parse -- all of which want a human.

HTTP here is read-only. Runs happen on the internal schedule or explicitly via
`docker exec debt-updater python /app/server.py --once`, so there is no public
endpoint that can trigger a fetch against the upstream APIs.

Everything that touches FRED goes through econcore, vendored from econ-core.
That is not just deduplication: fred.stlouisfed.org tarpits User-Agents it does
not recognise as a known tool, so the keyless CSV endpoint has to be asked with
urllib's honest default UA. This module used to send its own UA on that path,
which meant the documented "still works if the key is revoked" fallback had in
fact never worked -- masked because FRED_API_KEY was set. See econcore._get.
"""

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import econcore

TREASURY = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny"
UA = {"User-Agent": "debt.chrislawrence.ca (debt atlas updater)"}

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
# Off by default. A fresh deploy observes and logs for a cycle before it is
# trusted to edit files that took real research to assemble.
WRITE_ENABLED = os.environ.get("UPDATER_WRITE", "0") == "1"

CHANGELOG = os.path.join(DATA_DIR, "changelog.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "updater-state.json")
SERIES_FILE = os.path.join(DATA_DIR, "series.json")

MAX_CHANGE_PCT = 20.0        # refuse a write beyond this; see module docstring
SHRINK_TOLERANCE = 0.9       # a series that comes back this much shorter is refused
CHANGELOG_IN_PAYLOAD = 100

# Curated files, in the order the page expects them. `recessions` is vendored
# from econ-core rather than researched here, but it is read-only to this app
# in exactly the way the curated files are, so it rides the same path.
CURATED = ["meta", "gdp", "federal-debt", "ai-capital", "household-debt",
           "cycles", "recessions"]

_payload_cache = {"stamp": None, "body": None}

# `results` is the curated half, `series_results` the long-series half. They are
# separate because refresh_all runs both and a shared key would leave whichever
# ran first invisible in /api/status.
_state = {"last_run": None, "last_error": None, "failures": 0,
          "results": [], "series_results": []}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# fetching
#
# FRED goes through econcore. This local _get exists only for Treasury, which
# has no such filter and gets this app's own honest User-Agent.
# --------------------------------------------------------------------------

def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read().decode("utf-8")


def fred_obs(series_id):
    """[[iso_date, float], ...] ascending, via the shared fetcher."""
    return econcore.fred_series(series_id, FRED_KEY)


def fred_latest(series_id):
    """(value, observation_date) for the most recent observation."""
    obs = fred_obs(series_id)
    if not obs:
        raise ValueError("no observations for %s" % series_id)
    return obs[-1][1], obs[-1][0]


def quarter_label(iso_date):
    """FRED dates a quarter by its first month. '2026-04-01' -> '2026-Q2'."""
    y, m, _ = iso_date.split("-")
    return "%s-Q%d" % (y, (int(m) - 1) // 3 + 1)


def treasury_total_debt():
    """(USD trillions, record_date) for total public debt outstanding."""
    params = urllib.parse.urlencode({
        "sort": "-record_date",
        "page[size]": "1",
        "fields": "record_date,tot_pub_debt_out_amt",
    })
    body = json.loads(_get("%s?%s" % (TREASURY, params)))
    rows = body.get("data", [])
    if not rows:
        raise ValueError("treasury returned no rows")
    row = rows[0]
    return float(row["tot_pub_debt_out_amt"]) / 1e12, row["record_date"]


# --------------------------------------------------------------------------
# targets
#
# Each entry names an EXISTING value object and how to refresh it. `locate`
# returns the dict to mutate, or None when the entry has gone -- a missing
# target is reported, never recreated.
# --------------------------------------------------------------------------

def _by_id(doc, series_id):
    for item in doc.get("series", []):
        if item.get("id") == series_id:
            return item
    return None


TARGETS = [
    {
        "key": "us_total_public_debt",
        "file": "federal-debt.json",
        "label": "Total public debt outstanding",
        "locate": lambda d: _by_id(d, "us_total_public_debt"),
        "fetch": lambda: treasury_total_debt(),
        "as_of": lambda raw: quarter_label(raw),
        "upstream": "US Treasury, Debt to the Penny",
        "upstream_url": "https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/",
    },
    {
        "key": "us_debt_to_gdp_total",
        "file": "federal-debt.json",
        "label": "Total public debt as share of GDP",
        "locate": lambda d: _by_id(d, "us_debt_to_gdp_total"),
        "fetch": lambda: fred_latest("GFDEGDQ188S"),
        "as_of": lambda raw: quarter_label(raw),
        "upstream": "FRED GFDEGDQ188S",
        "upstream_url": "https://fred.stlouisfed.org/series/GFDEGDQ188S",
    },
    {
        "key": "us_gdp_nominal",
        "file": "gdp.json",
        "label": "United States nominal GDP",
        # FRED GDP is billions; the stored value is trillions.
        "fetch": lambda: (lambda v, d: (v / 1000.0, d))(*fred_latest("GDP")),
        "locate": lambda d: _by_id(d, "us_gdp_nominal"),
        "as_of": lambda raw: quarter_label(raw),
        "upstream": "FRED GDP",
        "upstream_url": "https://fred.stlouisfed.org/series/GDP",
    },
]

# Declared so the page and the log can say plainly that these are hand
# maintained, rather than leaving them silently absent from the automation.
MANUAL_TARGETS = [
    {"key": "ca_gdp_nominal_usd", "file": "gdp.json",
     "reason": "StatCan WDS vector ID not yet confirmed. Guessing a vector produces a plausible wrong number, so this stays manual until a vector is verified against a published table."},
    {"key": "canada.*", "file": "household-debt.json",
     "reason": "Same. StatCan WDS needs confirmed vector IDs for credit market debt, debt-to-disposable-income and the debt service ratio."},
    {"key": "united_states.*", "file": "household-debt.json",
     "reason": "NY Fed publishes an XLSX workbook. Parsing it is possible but the layout must be pinned to a verified release first; scraping the HTML page is explicitly not acceptable."},
    {"key": "ai-capital", "file": "ai-capital.json",
     "reason": "SEC filings, ratings notes and press reporting. Not automatable. Carries last_reviewed; the page shows staleness instead of pretending freshness."},
]


# --------------------------------------------------------------------------
# long time series
#
# These land in series.json, which is machine-owned and never hand-edited. The
# split matters: the curated files are the spec a human reviews in a diff, and
# a 241-point quarterly array dropped into one would bury every real edit.
# Writing here is therefore not gated on UPDATER_WRITE -- nothing in this file
# can overwrite a researched figure.
# --------------------------------------------------------------------------

# Keyed by the econ-core contract id, not by the upstream FRED id, because the
# overlay site keys on a stable snake_case id and FRED's mnemonics are neither
# stable nor shared. The FRED id survives in `source` and `source_url`.
#
# Two rules make these series comparable with the curated files and with the
# other trackers:
#
#   * where a series names the same quantity as a curated figure it reuses that
#     figure's id AND its unit -- `us_gdp_nominal` is the same thing in gdp.json
#     and here, so one id cannot carry two units;
#   * `scale` converts at fetch time, never at render time (CONTRACT.md). FRED
#     publishes these levels in millions and billions; the page compares them
#     against each other in trillions, and doing that arithmetic in the renderer
#     is how a chart ends up off by a thousand.

FETCHED = [
    {"fred": "GFDEGDQ188S", "id": "us_debt_to_gdp_total",
     "label": "US total public debt", "units": "percent", "freq": "quarterly",
     "note": "Federal debt including intragovernmental holdings. The number cable news quotes."},
    {"fred": "FYGFGDQ188S", "id": "us_debt_held_by_public_pct_gdp",
     "label": "US debt held by the public", "units": "percent_of_gdp", "freq": "quarterly",
     "note": "Excludes money the government owes itself. The measure CBO and most economists use."},
    {"fred": "GDP", "id": "us_gdp_nominal", "scale": 1e-3,
     "label": "US nominal GDP", "units": "USD_trillions", "freq": "quarterly",
     "note": "Quarterly, seasonally adjusted annual rate. FRED publishes billions; converted here."},
    {"fred": "GFDEBTN", "id": "us_total_public_debt", "scale": 1e-6,
     "label": "US total public debt outstanding", "units": "USD_trillions", "freq": "quarterly",
     "note": "Quarterly level. FRED publishes millions; converted here."},
    {"fred": "HDTGPDUSQ163N", "id": "us_household_debt_pct_gdp",
     "label": "US household debt", "units": "percent_of_gdp", "freq": "quarterly",
     "note": "BIS basis. Directly comparable with the Canadian series below, which the national collections are not."},
    {"fred": "HDTGPDCAQ163N", "id": "ca_household_debt_pct_gdp",
     "label": "Canada household debt", "units": "percent_of_gdp", "freq": "quarterly",
     "note": "BIS basis, same methodology as the US series."},
    {"fred": "TDSP", "id": "us_household_debt_service_ratio",
     "label": "US household debt service ratio", "units": "percent", "freq": "quarterly",
     "note": "Required debt payments as a share of disposable personal income. The US counterpart to StatCan's DSR."},
    {"fred": "CDSP", "id": "us_consumer_debt_service_ratio",
     "label": "US consumer debt service ratio", "units": "percent", "freq": "quarterly",
     "note": "Non-mortgage consumer debt only."},
    {"fred": "BCNSDODNS", "id": "us_nonfinancial_corporate_debt", "scale": 1e-6,
     "label": "US nonfinancial corporate debt", "units": "USD_trillions", "freq": "quarterly",
     "note": "All debt securities and loans of nonfinancial corporate business. The denominator that "
             "puts AI borrowing in proportion. FRED publishes millions; converted here."},
    {"fred": "A091RC1Q027SBEA", "id": "us_federal_interest_payments", "scale": 1e-3,
     "label": "US federal interest payments", "units": "USD_trillions", "freq": "quarterly",
     "note": "Annual rate, quarterly observations. FRED publishes billions; converted here."},
    {"fred": "POPTHM", "id": "us_population",
     "label": "US population", "units": "thousands_of_persons", "freq": "monthly",
     "note": "Monthly."},
    {"fred": "POPTOTCAA647NWDB", "id": "ca_population",
     "label": "Canada population", "units": "persons", "freq": "annual",
     "note": "World Bank, annual."},
    {"fred": "MKTGDPCAA646NWDB", "id": "ca_gdp_nominal_usd", "scale": 1e-12,
     "label": "Canada nominal GDP", "units": "USD_trillions", "freq": "annual",
     "note": "World Bank, annual, market exchange rates. Published in dollars; converted here."},
]


def build_series():
    """Fetch every long series. A failure on one is recorded, not fatal.

    Every entry is assembled by econcore.make_series, which validates against
    the shared contract and raises rather than returning something malformed --
    so a broken series is caught by the updater that produced it instead of by
    the page that tries to draw it.
    """
    out = {}
    errors = {}
    for spec in FETCHED:
        sid = spec["id"]
        try:
            obs = fred_obs(spec["fred"])
            scale = spec.get("scale")
            if scale:
                obs = [[d, round(v * scale, 9)] for d, v in obs]
            out[sid] = econcore.make_series(
                sid, spec["label"], "FRED " + spec["fred"],
                "https://fred.stlouisfed.org/series/" + spec["fred"],
                spec["units"], spec["freq"], obs, note=spec["note"])
        except Exception as exc:  # noqa: BLE001 - one dead series must not sink the rest
            errors[sid] = "%s: %s" % (type(exc).__name__, exc)
            print("series %s failed: %s" % (sid, errors[sid]), flush=True)
    return out, errors


def latest(series, series_id):
    entry = series.get(series_id)
    if not entry or not entry.get("obs"):
        return None, None
    return entry["obs"][-1][1], entry["as_of"]


def derive(series, curated):
    """Per-capita figures, computed from sourced inputs rather than asserted.

    Returned with the inputs that produced them so a reader can check the
    arithmetic instead of trusting it.
    """
    out = {}
    us_pop, us_pop_date = latest(series, "us_population")   # thousands of persons
    ca_pop, ca_pop_date = latest(series, "ca_population")   # persons

    us_debt = curated.get("household-debt.json", {}).get("united_states", {}).get("total_debt", {})
    if us_pop and us_debt.get("value"):
        out["us_household_debt_per_capita"] = {
            "value": round(us_debt["value"] * 1e12 / (us_pop * 1000.0)),
            "unit": "USD",
            "as_of": us_debt.get("as_of"),
            "confidence": "estimate",
            "source": "Derived: NY Fed total household debt over FRED POPTHM",
            "source_url": "https://fred.stlouisfed.org/series/POPTHM",
            "inputs": {"total_debt_usd_trillions": us_debt["value"],
                       "population": round(us_pop * 1000), "population_as_of": us_pop_date},
        }

    ca_debt = curated.get("household-debt.json", {}).get("canada", {}).get("total_credit_market_debt", {})
    if ca_pop and ca_debt.get("value"):
        out["ca_household_debt_per_capita"] = {
            "value": round(ca_debt["value"] * 1e9 / ca_pop),
            "unit": "CAD",
            "as_of": ca_debt.get("as_of"),
            "confidence": "estimate",
            "source": "Derived: StatCan credit market debt over World Bank population via FRED",
            "source_url": "https://fred.stlouisfed.org/series/POPTOTCAA647NWDB",
            "inputs": {"total_debt_cad_billions": ca_debt["value"],
                       "population": ca_pop, "population_as_of": ca_pop_date},
        }
    return out


def load_old_series():
    """The previously written series map, or {} when there is none.

    Entries that predate the contract migration are dropped: they carry
    `dates`/`values` instead of `obs`, and carrying one forward would write an
    old-shaped doc back into a contract-shaped file.
    """
    try:
        with open(SERIES_FILE) as fh:
            stored = json.load(fh).get("series", {})
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in stored.items() if isinstance(v, dict) and v.get("obs")}


def diff_revisions(series_id, before, after):
    """A record of every already-published observation whose value moved.

    New observations at the end are not revisions, they are just new. What
    matters here is upstream restating history: it happens quietly, and without
    this the wholesale rewrite would leave no trace that it did.
    """
    old = dict(before)
    changed = []
    for date, value in after:
        if date in old and old[date] != value:
            changed.append((date, old[date], value))
    if not changed:
        return None
    deltas = [abs(a - b) for _, b, a in changed]
    return {
        "series": series_id, "action": "revised", "changed": len(changed),
        "span": [changed[0][0], changed[-1][0]],
        "max_delta": round(max(deltas), 6),
        "sample": [{"date": d, "before": b, "after": a} for d, b, a in changed[:3]],
    }


def refresh_series():
    """Fetch every long series, guarded, and rewrite series.json wholesale.

    Three guardrails, all of which exist because "the file was rewritten and the
    run reported success" is not the same as "the data is good":

      * a series that fails to fetch is carried forward from the last good run
        rather than vanishing from the file until the next one succeeds;
      * a series that comes back more than SHRINK_TOLERANCE shorter than the
        stored one is refused -- a truncated response is indistinguishable from
        a real series until you compare lengths;
      * a run that has nothing fetched and nothing stored refuses to write at
        all, rather than replacing the file with an empty one.
    """
    series, errors = build_series()
    previous = load_old_series()
    results = []

    for spec in FETCHED:
        sid = spec["id"]
        prev, fresh = previous.get(sid), series.get(sid)
        rec = {"series": sid}

        if fresh is None:
            if prev:
                series[sid] = dict(prev, carried_forward=True)
                rec.update(action="carried-forward", reason=errors.get(sid, "fetch failed"))
            else:
                rec.update(action="error", reason=errors.get(sid, "fetch failed"))
        elif prev and prev.get("obs"):
            if fresh["as_of"] < prev["as_of"]:
                series[sid] = dict(prev, carried_forward=True)
                rec.update(action="stale-upstream",
                           reason="upstream is at %s, behind the stored %s; stored series kept"
                                  % (fresh["as_of"], prev["as_of"]))
            elif len(fresh["obs"]) < len(prev["obs"]) * SHRINK_TOLERANCE:
                series[sid] = dict(prev, carried_forward=True)
                rec.update(action="shrunk",
                           reason="%d observations against the stored %d; stored series kept"
                                  % (len(fresh["obs"]), len(prev["obs"])))
            else:
                revision = diff_revisions(sid, prev["obs"], fresh["obs"])
                if revision:
                    econcore.log_revision(CHANGELOG, revision)
                    rec.update(action="revised", changed=revision["changed"],
                               reason="upstream restated %d observation(s)" % revision["changed"])
                else:
                    rec.update(action="fetched", observations=len(fresh["obs"]))
        else:
            rec.update(action="fetched", observations=len(fresh["obs"]))

        results.append(rec)
        if rec["action"] not in ("fetched",):
            print("series %-34s %-16s %s" % (sid, rec["action"], rec.get("reason", "")), flush=True)

    if not series:
        raise ValueError("nothing fetched and nothing stored; refusing to write")

    curated = {}
    for name in ("household-debt.json",):
        try:
            curated[name] = load(name)
        except Exception:  # noqa: BLE001 - derived figures are optional
            pass
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": "Machine-fetched long series. Never hand-edited; the updater overwrites this file wholesale.",
        "econcore": econcore.VERSION,
        "fred_key_used": bool(FRED_KEY),
        "errors": errors,
        "derived": derive(series, curated),
        "series": series,
    }
    tmp = SERIES_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, SERIES_FILE)
    total = sum(len(s["obs"]) for s in series.values())
    print("series refreshed: %d series, %d observations, %d errors"
          % (len(series), total, len(errors)), flush=True)

    with _lock:
        _state["last_run"] = datetime.now(timezone.utc).isoformat()
        _state["series_results"] = results
    save_state(["series_results"])
    return payload


# --------------------------------------------------------------------------
# file helpers
# --------------------------------------------------------------------------

def load(name):
    with open(os.path.join(DATA_DIR, name)) as fh:
        return json.load(fh)


def save(name, doc):
    """Atomic, mode 644 so the read-only nginx mount can serve it."""
    path = os.path.join(DATA_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# One log, two record shapes. A curated record names one figure and carries a
# single before/after; a series record names a series and carries a sample of
# the observations upstream restated. econcore.log_revision only stamps
# observed_at, so both coexist and the page renders them apart.

def log_change(rec):
    return econcore.log_revision(CHANGELOG, rec)


def read_changelog(limit):
    return econcore.read_revisions(CHANGELOG, limit)


def pct_change(before, after):
    if before in (None, 0):
        return None
    return abs(after - before) / abs(before) * 100.0


def as_of_rank(label):
    """Sortable key for an as_of label, or None if it is not a plain period.

    Handles '2026-Q3', '2026-08' and '2026'. Anything else -- 'FY2026
    projected year-end' and friends -- returns None, which callers treat as
    "cannot compare" rather than "equal".
    """
    if not isinstance(label, str):
        return None
    text = label.strip()
    try:
        if "-Q" in text:
            year, quarter = text.split("-Q")
            return (int(year), int(quarter) * 3)
        if "-" in text:
            year, month = text.split("-")[:2]
            return (int(year), int(month))
        return (int(text), 12)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def run_once(write=None, verbose=True):
    """Fetch every automatable target. Returns a list of result records."""
    write = WRITE_ENABLED if write is None else write
    results = []
    touched = {}

    for target in TARGETS:
        rec = {"key": target["key"], "file": target["file"], "label": target["label"]}
        try:
            doc = touched.get(target["file"]) or load(target["file"])
            entry = target["locate"](doc)
            if entry is None:
                rec.update(action="missing",
                           reason="no entry with that id; the updater does not create values")
                results.append(log_change(rec))
                continue

            value, raw_date = target["fetch"]()
            as_of = target["as_of"](raw_date)
            before, before_as_of = entry.get("value"), entry.get("as_of")
            delta = pct_change(before, value)

            rec.update(before=before, after=round(value, 6),
                       as_of_before=before_as_of, as_of_after=as_of,
                       change_pct=None if delta is None else round(delta, 3))

            old_rank, new_rank = as_of_rank(before_as_of), as_of_rank(as_of)
            if delta is not None and delta > MAX_CHANGE_PCT:
                rec.update(action="blocked",
                           reason="change of %.1f%% exceeds the %.0f%% guardrail; stored value kept"
                                  % (delta, MAX_CHANGE_PCT))
            elif old_rank and new_rank and new_rank < old_rank:
                # Some upstreams lag a figure that was derived from fresher
                # inputs. FRED's GFDEGDQ188S trails the ratio computed from
                # Treasury debt over BEA's advance GDP by a quarter, so an
                # unguarded write would downgrade a current figure to an older
                # one and still look like a successful update.
                rec.update(action="stale-upstream",
                           reason="upstream is at %s, behind the stored %s; stored value kept"
                                  % (as_of, before_as_of))
            elif before == round(value, 6) and before_as_of == as_of:
                rec.update(action="unchanged")
            elif not write:
                rec.update(action="dry-run", reason="UPDATER_WRITE is not set")
            else:
                entry["value"] = round(value, 6)
                entry["as_of"] = as_of
                # `source` and `source_url` stay as the human authored them.
                # This records which upstream actually produced the number now
                # on the page, so a machine-refreshed figure is still traceable
                # to the endpoint it came from rather than to the citation it
                # was first researched from.
                entry["updated_by"] = {
                    "source": target["upstream"],
                    "source_url": target["upstream_url"],
                    "at": datetime.now(timezone.utc).date().isoformat(),
                }
                touched[target["file"]] = doc
                rec.update(action="written")
        except Exception as exc:  # noqa: BLE001 - one bad target must not sink the run
            rec.update(action="error", reason="%s: %s" % (type(exc).__name__, exc))

        results.append(log_change(rec))
        if verbose:
            print("%-24s %-10s %s" % (rec["key"], rec["action"], rec.get("reason", "")), flush=True)

    if write and touched:
        for name, doc in touched.items():
            save(name, doc)
        stamp_built()

    with _lock:
        _state["last_run"] = datetime.now(timezone.utc).isoformat()
        _state["results"] = results
    save_state(["results"])
    return results


def stamp_built():
    """meta.json's `built` marks the last time a figure actually changed."""
    try:
        meta = load("meta.json")
        meta["built"] = datetime.now(timezone.utc).date().isoformat()
        save("meta.json", meta)
    except Exception as exc:  # noqa: BLE001 - a failed stamp is not a failed run
        print("could not stamp built: %s" % exc, flush=True)


def read_state():
    """The last run's state, preferring the file over this process's memory.

    Both halves of a run write STATE_FILE, and a `docker exec ... --refresh`
    writes it from a process that shares nothing with the HTTP server. The file
    is therefore the newer of the two whenever cron has run at all; memory is
    the fallback for a fresh container that has not yet written one.
    """
    try:
        with open(STATE_FILE) as fh:
            stored = json.load(fh)
        if isinstance(stored, dict) and stored.get("last_run"):
            return stored
    except (OSError, ValueError):
        pass
    with _lock:
        return dict(_state)


def save_state(keys):
    """Merge this run's half into the stored state.

    `--series` and `--once` each fill one half and leave the other empty, and
    they run as separate processes. Writing the whole in-memory _state would
    have a series-only run report "no curated results" simply because it never
    looked, so only the keys the caller actually produced are written.
    """
    try:
        stored = read_state()
        with _lock:
            snapshot = dict(_state)
        merged = dict(stored)
        for key in keys:
            merged[key] = snapshot.get(key)
        merged["last_run"] = snapshot.get("last_run") or stored.get("last_run")
        snapshot = merged
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(snapshot, fh, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------
# one-shot: exact historical debt/GDP observations (BUILD-PLAN 2a)
# --------------------------------------------------------------------------

# Read approximately off a chart in the original delivery, and flagged as such
# in federal-debt.json's gap_note.
APPROX_YEARS = [1980, 1990, 2000, 2012, 2024]


def backfill_historical(write=False):
    """Replace the eyeballed historical points with exact FRED observations.

    Convention: the Q4 observation (FRED dates it 1 October), so each point is
    a year-end read rather than an average. Stated here because the choice is
    not recoverable from the number alone.

    Only the flagged years are touched. 1946 predates GFDEGDQ188S (which starts
    in 1966) and comes from OMB historical tables; 1974, 2007 and 2021 are
    already exact dated reads; 2026 tracks the live headline series.
    """
    obs = dict(fred_obs("GFDEGDQ188S"))
    doc = load("federal-debt.json")
    hist = doc.get("historical", {})
    points = hist.get("points", [])
    changed, misses = [], []

    for point in points:
        year = point.get("year")
        if year not in APPROX_YEARS:
            continue
        key = "%d-10-01" % year
        if key not in obs:
            misses.append(year)
            continue
        before = point.get("value")
        after = round(obs[key], 2)
        point["value"] = after
        point["as_of"] = key
        point["source_url"] = "https://fred.stlouisfed.org/series/GFDEGDQ188S"
        changed.append({"year": year, "before": before, "after": after})

    if misses:
        print("no Q4 observation for: %s -- gap_note kept" % misses, flush=True)
    elif changed:
        # Only once every flagged year is exact, per the build plan.
        hist.pop("gap_note", None)
        hist["points_note"] = (
            "Quarterly observations from FRED GFDEGDQ188S, taken at Q4 (dated 1 October) "
            "so each point is a year-end read. 1946 predates the series and comes from OMB "
            "historical tables."
        )

    for c in changed:
        print("%d: %s -> %s" % (c["year"], c["before"], c["after"]), flush=True)
        log_change({"key": "historical.points.%d" % c["year"], "file": "federal-debt.json",
                    "label": "Debt/GDP historical point", "before": c["before"],
                    "after": c["after"], "action": "written" if write else "dry-run"})

    if write and changed and not misses:
        save("federal-debt.json", doc)
        stamp_built()
        print("written; gap_note removed", flush=True)
    elif not write:
        print("dry run -- pass --write to apply", flush=True)
    return changed


# --------------------------------------------------------------------------
# scheduling
#
# The schedule lives in the host crontab, not in here. Every scheduled
# container job on this box is a crontab entry calling docker exec, and one
# scheduler that is visible from `crontab -l` beats an invisible thread. Retry
# cadence is therefore the cron cadence: a failed run is logged and the next
# tick tries again.
# --------------------------------------------------------------------------

def refresh_all():
    """Both halves, in order. The single command cron calls.

    Series first, because the curated targets are compared against GDP figures
    the series fetch may itself have moved; the other order would measure a
    change against a stale denominator.
    """
    refresh_series()
    return run_once(write=True)


# --------------------------------------------------------------------------
# read-only HTTP
# --------------------------------------------------------------------------

def data_stamp():
    """Newest mtime across everything the payload is built from."""
    newest = 0.0
    for name in CURATED:
        path = os.path.join(DATA_DIR, name + ".json")
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    for path in (SERIES_FILE, CHANGELOG):
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return newest


def build_data_payload():
    """Everything the page consumes, in one object.

    Composed from disk rather than held in memory, because the refresh runs
    outside this process via docker exec. An in-memory payload would keep
    serving superseded figures until the container restarted, while the files
    on disk were already current -- stale numbers behind a healthy endpoint,
    which is the failure mode this whole project exists to avoid. The mtime
    cache keeps the common case a dict lookup.
    """
    stamp = data_stamp()
    if _payload_cache["stamp"] == stamp and _payload_cache["body"] is not None:
        return _payload_cache["body"]

    payload = {"generated_at": datetime.now(timezone.utc).isoformat()}
    for name in CURATED:
        try:
            payload[name] = load(name + ".json")
        except Exception as exc:  # noqa: BLE001 - a missing file is reported, not fatal
            payload[name] = None
            payload.setdefault("errors", {})[name] = str(exc)
    try:
        series = json.load(open(SERIES_FILE))
        payload["series"] = series.get("series", {})
        payload["derived"] = series.get("derived", {})
        payload["series_fetched_at"] = series.get("fetched_at")
        payload["series_errors"] = series.get("errors", {})
        payload["econcore"] = series.get("econcore")
    except Exception as exc:  # noqa: BLE001 - charts degrade, the page still renders
        payload["series"] = {}
        payload["derived"] = {}
        payload.setdefault("errors", {})["series"] = str(exc)

    recent, total = read_changelog(CHANGELOG_IN_PAYLOAD)
    payload["changelog"] = {"total": total, "recent": recent}

    _payload_cache["stamp"] = stamp
    _payload_cache["body"] = payload
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json", cache="no-cache"):
        raw = json.dumps(body).encode() if ctype == "application/json" else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            self._send(200, {"status": "ok"})
        elif path == "/api/data":
            self._send(200, build_data_payload(),
                       cache="public, max-age=300, must-revalidate")
        elif path == "/api/status":
            # From disk, for the same reason /api/data is composed from disk:
            # the refresh that matters runs in another process via docker exec,
            # so this process's in-memory _state only ever reflects its own
            # startup run. Serving that would report a months-old result behind
            # a healthy endpoint while the real run's state sat on disk.
            snapshot = read_state()
            snapshot["write_enabled"] = WRITE_ENABLED
            snapshot["fred_key"] = bool(FRED_KEY)
            snapshot["econcore"] = econcore.VERSION
            snapshot["manual_targets"] = MANUAL_TARGETS
            snapshot["max_change_percent"] = MAX_CHANGE_PCT
            snapshot["shrink_tolerance"] = SHRINK_TOLERANCE
            self._send(200, snapshot)
        elif path == "/api/changelog":
            recent, total = read_changelog(CHANGELOG_IN_PAYLOAD)
            self._send(200, {"total": total, "recent": recent})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        return


def main():
    if "--once" in sys.argv:
        run_once(write="--write" in sys.argv)
        return
    if "--series" in sys.argv:
        refresh_series()
        return
    if "--refresh" in sys.argv:
        refresh_all()
        return
    if "--backfill-historical" in sys.argv:
        backfill_historical(write="--write" in sys.argv)
        return

    print("updater starting: write=%s fred_key=%s (schedule: host cron)"
          % (WRITE_ENABLED, bool(FRED_KEY)), flush=True)
    # Built at startup rather than waiting for the first cron tick, so a freshly
    # built container serves charts immediately. Threaded so a slow FRED does
    # not hold the port closed and fail the healthcheck.
    def warm():
        try:
            refresh_series()
        except Exception as exc:  # noqa: BLE001 - the server must come up regardless
            print("initial series fetch failed: %s" % exc, flush=True)
    threading.Thread(target=warm, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
