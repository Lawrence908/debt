#!/usr/bin/env python3
"""Request every source_url in the data layer and report what came back.

Two link problems have already reached this repo and neither would have been
caught by reading the files: a page cited at two different paths, and a bare
host standing in for an endpoint. Both were found by hand. This makes the check
repeatable.

Two details that matter more than they look:

  * Send a browser User-Agent. data.sec.gov answers 403 without one and 200
    with it, so a naive checker reports a false failure on a link that works
    perfectly for a reader clicking it.
  * Follow redirects and report where they land. A 301 is how the duplicate
    Chicago Booth path was found, so anything that redirects is a
    canonicalisation candidate rather than a pass.

Read-only, and it hits third-party servers, so it is run by hand rather than
from the cron job.

    python3 scripts/check-sources.py [--timeout 25]
"""

import concurrent.futures
import glob
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

# A User-Agent alone is not enough. Several publishers sit behind bot
# protection that also wants the Accept headers a real browser sends, and
# answers 403 without them. Those pages open fine for a reader, so reporting
# them as broken would be the checker's error, not the data's.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/152.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Connection": "close",
}
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
TIMEOUT = 45

# Hosts behind bot protection that refuse any scripted client. Verified by hand
# with a real browser: thehub.ca returns a full article, cbo.gov serves a
# DataDome captcha and ropesgray.com a Cloudflare interstitial, and all three
# open normally for a person. Reporting them as broken every run would train
# whoever reads this output to skim past real failures, so they are counted
# separately. Re-verify by opening them rather than by trusting this list.
BOT_PROTECTED = {"thehub.ca", "www.cbo.gov", "www.ropesgray.com"}


def host_of(url):
    return url.split("//", 1)[-1].split("/")[0]


def collect(node, out):
    """Every source_url anywhere in the tree, including updated_by provenance."""
    if isinstance(node, list):
        for item in node:
            collect(item, out)
    elif isinstance(node, dict):
        for key in ("source_url",):
            val = node.get(key)
            if isinstance(val, str) and val.startswith("http"):
                out.add(val)
        updated = node.get("updated_by")
        if isinstance(updated, dict) and isinstance(updated.get("source_url"), str):
            if updated["source_url"].startswith("http"):
                out.add(updated["source_url"])
        for k, v in node.items():
            if k != "updated_by":
                collect(v, out)
    return out


def check(url, attempts=2):
    """Fetch once, retry a timeout once. Concurrency is kept low deliberately:
    hitting one publisher with a dozen parallel requests gets the checker
    throttled, which then reports the publisher as down."""
    ctx = ssl.create_default_context()
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
                return url, resp.status, resp.geturl(), None
        except urllib.error.HTTPError as exc:
            return url, exc.code, url, None
        except Exception as exc:  # noqa: BLE001 - a dead link is a result, not a crash
            last = "%s: %s" % (type(exc).__name__, exc)
    return url, None, url, last


def main():
    global TIMEOUT
    if "--timeout" in sys.argv:
        TIMEOUT = int(sys.argv[sys.argv.index("--timeout") + 1])

    urls = set()
    for path in sorted(glob.glob(os.path.join(DATA, "*.json"))):
        doc = json.load(open(path))
        if os.path.basename(path) == "series.json":
            collect(doc.get("series"), urls)
            collect(doc.get("derived"), urls)
        else:
            collect(doc, urls)

    print("checking %d distinct source URLs\n" % len(urls))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(check, sorted(urls)))

    ok = redirected = broken = protected = 0
    for url, status, final, err in results:
        short = url.replace("https://", "").replace("http://", "")
        blocked = host_of(url) in BOT_PROTECTED
        if blocked and (err or status is None or status >= 400):
            protected += 1
            print("  BOT   %s\n        blocked to scripted clients, opens for a reader" % short[:88])
        elif err or status is None:
            broken += 1
            print("  FAIL  %s\n        %s" % (short[:88], err))
        elif status >= 400:
            broken += 1
            print("  %-5s %s" % (status, short[:88]))
        elif final.rstrip("/") != url.rstrip("/"):
            redirected += 1
            print("  %-5s %s\n        redirects to %s" % (status, short[:88], final))
        else:
            ok += 1

    print("\n%d ok, %d bot-protected, %d redirecting, %d broken" % (ok, protected, redirected, broken))
    if redirected:
        print("Redirects are canonicalisation candidates: store the URL the publisher "
              "settles on, so the sources list does not carry the same page twice.")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
