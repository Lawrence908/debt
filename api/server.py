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
"""

import json
import os
import sys
import csv
import io
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TREASURY = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
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
REFRESH_SECONDS = 24 * 3600  # quarterly series, checked daily
RETRY_BASE_SECONDS = 120
RETRY_MAX_SECONDS = 3600
RETRY_MAX_DOUBLINGS = 10
CHANGELOG_IN_PAYLOAD = 100

_state = {"last_run": None, "last_error": None, "failures": 0, "results": []}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read().decode("utf-8")


def fred_series(series_id):
    """Return {date: float} for one FRED series, newest last.

    Uses the keyed JSON API when FRED_API_KEY is set and falls back to the
    keyless CSV endpoint otherwise, so the updater still works if the key is
    ever revoked rather than failing the whole run.
    """
    if FRED_KEY:
        params = urllib.parse.urlencode({
            "series_id": series_id,
            "api_key": FRED_KEY,
            "file_type": "json",
        })
        body = json.loads(_get("%s?%s" % (FRED_API, params)))
        out = {}
        for row in body.get("observations", []):
            if row.get("value") in (None, "", "."):
                continue
            try:
                out[row["date"]] = float(row["value"])
            except (TypeError, ValueError):
                continue
        if out:
            return out
        # An empty keyed response is more likely a bad key than an empty
        # series, so fall through to the keyless endpoint rather than raise.

    out = {}
    reader = csv.reader(io.StringIO(_get(FRED_CSV.format(series_id))))
    next(reader)
    for row in reader:
        if len(row) < 2 or row[1].strip() in ("", "."):
            continue
        try:
            out[row[0].strip()] = float(row[1].strip())
        except ValueError:
            continue
    if not out:
        raise ValueError("no observations for %s" % series_id)
    return out


def fred_latest(series_id):
    """(value, observation_date) for the most recent observation."""
    obs = fred_series(series_id)
    date = max(obs)
    return obs[date], date


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

SERIES = [
    ("GFDEGDQ188S", "US total public debt", "percent_of_gdp",
     "Federal debt including intragovernmental holdings. The number cable news quotes."),
    ("FYGFGDQ188S", "US debt held by the public", "percent_of_gdp",
     "Excludes money the government owes itself. The measure CBO and most economists use."),
    ("GDP", "US nominal GDP", "USD_billions", "Quarterly, seasonally adjusted annual rate."),
    ("GFDEBTN", "US total public debt outstanding", "USD_millions", "Quarterly level."),
    ("HDTGPDUSQ163N", "US household debt", "percent_of_gdp",
     "BIS basis. Directly comparable with the Canadian series below, which the national collections are not."),
    ("HDTGPDCAQ163N", "Canada household debt", "percent_of_gdp",
     "BIS basis, same methodology as the US series."),
    ("TDSP", "US household debt service ratio", "percent",
     "Required debt payments as a share of disposable personal income. The US counterpart to StatCan's DSR."),
    ("CDSP", "US consumer debt service ratio", "percent", "Non-mortgage consumer debt only."),
    ("BCNSDODNS", "US nonfinancial corporate debt", "USD_millions",
     "All debt securities and loans of nonfinancial corporate business. The denominator that puts AI borrowing in proportion."),
    ("A091RC1Q027SBEA", "US federal interest payments", "USD_billions", "Annual rate, quarterly observations."),
    ("POPTHM", "US population", "thousands", "Monthly."),
    ("POPTOTCAA647NWDB", "Canada population", "persons", "World Bank, annual."),
    ("MKTGDPCAA646NWDB", "Canada nominal GDP", "USD", "World Bank, annual, market exchange rates."),
]


def build_series():
    """Fetch every long series. A failure on one is recorded, not fatal."""
    out = {}
    errors = {}
    for series_id, label, unit, note in SERIES:
        try:
            obs = fred_series(series_id)
            dates = sorted(obs)
            out[series_id] = {
                "label": label,
                "unit": unit,
                "note": note,
                "source": "FRED " + series_id,
                "source_url": "https://fred.stlouisfed.org/series/" + series_id,
                "confidence": "reported",
                "as_of": dates[-1],
                "dates": dates,
                "values": [obs[d] for d in dates],
            }
        except Exception as exc:  # noqa: BLE001 - one dead series must not sink the rest
            errors[series_id] = "%s: %s" % (type(exc).__name__, exc)
            print("series %s failed: %s" % (series_id, errors[series_id]), flush=True)
    return out, errors


def latest(series, series_id):
    entry = series.get(series_id)
    if not entry or not entry["values"]:
        return None, None
    return entry["values"][-1], entry["as_of"]


def derive(series, curated):
    """Per-capita figures, computed from sourced inputs rather than asserted.

    Returned with the inputs that produced them so a reader can check the
    arithmetic instead of trusting it.
    """
    out = {}
    us_pop, us_pop_date = latest(series, "POPTHM")          # thousands
    ca_pop, ca_pop_date = latest(series, "POPTOTCAA647NWDB")  # persons

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


def refresh_series():
    series, errors = build_series()
    if not series:
        raise ValueError("no series fetched")
    curated = {}
    for name in ("household-debt.json",):
        try:
            curated[name] = load(name)
        except Exception:  # noqa: BLE001 - derived figures are optional
            pass
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": "Machine-fetched long series. Never hand-edited; the updater overwrites this file wholesale.",
        "errors": errors,
        "derived": derive(series, curated),
        "series": series,
    }
    tmp = SERIES_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, SERIES_FILE)
    total = sum(len(s["values"]) for s in series.values())
    print("series refreshed: %d series, %d observations, %d errors"
          % (len(series), total, len(errors)), flush=True)
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


def log_change(rec):
    rec = dict(rec, observed_at=datetime.now(timezone.utc).isoformat())
    with open(CHANGELOG, "a") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    try:
        os.chmod(CHANGELOG, 0o644)
    except OSError:
        pass
    return rec


def read_changelog(limit):
    if not os.path.exists(CHANGELOG):
        return [], 0
    out = []
    with open(CHANGELOG) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return list(reversed(out[-limit:])), len(out)


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
    save_state()
    return results


def stamp_built():
    """meta.json's `built` marks the last time a figure actually changed."""
    try:
        meta = load("meta.json")
        meta["built"] = datetime.now(timezone.utc).date().isoformat()
        save("meta.json", meta)
    except Exception as exc:  # noqa: BLE001 - a failed stamp is not a failed run
        print("could not stamp built: %s" % exc, flush=True)


def save_state():
    try:
        with _lock:
            snapshot = dict(_state)
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
    obs = fred_series("GFDEGDQ188S")
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
# --------------------------------------------------------------------------

def next_delay(failures):
    """A clean run waits the full interval; a failure backs off from 2 min."""
    if failures <= 0:
        return REFRESH_SECONDS
    doublings = min(failures - 1, RETRY_MAX_DOUBLINGS)
    return min(RETRY_BASE_SECONDS * (2 ** doublings), RETRY_MAX_SECONDS)


def refresher():
    failures = 0
    while True:
        time.sleep(next_delay(failures))
        try:
            refresh_series()
            results = run_once()
            failures = 0 if not any(r["action"] == "error" for r in results) else failures + 1
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any single run
            failures += 1
            with _lock:
                _state["last_error"] = "%s: %s" % (type(exc).__name__, exc)
            print("run failed: %s" % _state["last_error"], flush=True)


# --------------------------------------------------------------------------
# read-only HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json"):
        raw = json.dumps(body).encode() if ctype == "application/json" else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            self._send(200, {"status": "ok"})
        elif path == "/api/status":
            with _lock:
                snapshot = dict(_state)
            snapshot["write_enabled"] = WRITE_ENABLED
            snapshot["fred_key"] = bool(FRED_KEY)
            snapshot["manual_targets"] = MANUAL_TARGETS
            snapshot["max_change_percent"] = MAX_CHANGE_PCT
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
    if "--backfill-historical" in sys.argv:
        backfill_historical(write="--write" in sys.argv)
        return

    print("updater starting: write=%s fred_key=%s" % (WRITE_ENABLED, bool(FRED_KEY)), flush=True)
    # series.json is machine-owned, so it is built at startup rather than
    # waiting a full interval -- a fresh container should serve charts at once.
    try:
        refresh_series()
    except Exception as exc:  # noqa: BLE001 - the server must come up regardless
        print("initial series fetch failed: %s" % exc, flush=True)
    threading.Thread(target=refresher, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
