#!/usr/bin/env python3
"""Scrape NFL broadcasts on Las Vegas OTA channels from tvtv.us.

Mechanical capture only: NFL airings on the target channels (games, Sunday
Night Football, NFL studio shows; see INCLUDE_RE / EXCLUDE_RE) are written
verbatim to latest.json. College football, IFL/UFL and soccer are excluded.
No team/matchup parsing happens here.

How it works (see README.md for the investigation notes):
  tvtv.us sits behind Cloudflare, which 403s plain HTTP clients and the
  stripped-down Playwright headless_shell. Full Chromium in new-headless mode
  passes. We load the guide page once to get a Cloudflare-cleared browser
  session, then call the site's own grid endpoints with fetch() from inside
  the page:
    /partial/lineup/<LINEUP>                 channel list (HTML)
    /partial/source/<utcMidnightMs>/<srcId>  one UTC day for one station (HTML)
    /dlg/program?id=<programId>              program details dialog (HTML)
  The HTML fragments are parsed in-page with DOMParser and returned as JSON.
"""

import argparse
import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- config ---
ZIP = "89118"
LINEUP = "USA-OTA89118"  # tvtv.us lineup id for over-the-air in this zip
PAGE_URL = f"https://www.tvtv.us/nv/las-vegas/{ZIP}/lu{LINEUP}"

# Major channel number -> label. Every subchannel (5.1, 5.2, ...) of these
# majors is scanned.
TARGET_CHANNELS = {
    "3": "NBC (KSNV)",
    "5": "FOX (KVVU)",
    "8": "CBS (KLAS)",
    "13": "ABC (KTNV)",
}

DAYS_AHEAD = 14  # today through today + 14 (inclusive)
# NFL only. An airing is kept if its title or subtitle matches INCLUDE_RE and
# not EXCLUDE_RE. Games are titled "NFL Football" (Sunday Night Football
# included; its pregame is "Football Night in America"). "NFL" must be a whole
# word: a bare substring hits "DragoNFLyTV" and "iNFLuential". Plain
# "football" is deliberately not a keyword: on these channels it is college,
# IFL/UFL or soccer.
INCLUDE_RE = re.compile(
    r"\bNFL\b|Football Night in America|(Sunday|Monday|Thursday) Night Football|Super Bowl|Pro Bowl",
    re.IGNORECASE,
)
EXCLUDE_RE = re.compile(
    r"college|NCAA|\bIFL\b|\bUFL\b|soccer|\bMLS\b|FIFA|UEFA|Premier League|Liga MX|World Cup",
    re.IGNORECASE,
)
TZ = ZoneInfo("America/Los_Angeles")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
# tvtv.us rate-limits (429, then Cloudflare 403) if hammered. Grid partials
# tolerate light concurrency; the details dialog is stricter, so it goes one
# at a time with a gap, and results are cached between runs.
GRID_CONCURRENCY = 2
GRID_DELAY_MS = 250
DETAIL_DELAY_MS = 2000
DETAIL_CACHE_TTL = timedelta(days=2)

REPO_DIR = Path(__file__).resolve().parent
OUTPUT = REPO_DIR / "latest.json"
STATUS_OUTPUT = REPO_DIR / "last-run.json"
DETAIL_CACHE = REPO_DIR / "cache" / "program-details.json"
LOG_DIR = REPO_DIR / "logs"

log = logging.getLogger("scrape")

# ------------------------------------------------------------ in-page JS ---
# Fetch a list of same-origin URLs with limited concurrency, a gap between
# requests per worker, and backoff on 429. A 403 (Cloudflare block) stops
# all workers so we don't dig the hole deeper.
JS_FETCH = """
async ({urls, concurrency, delayMs}) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const out = new Array(urls.length);
  let next = 0, blocked = false;
  async function worker() {
    while (next < urls.length) {
      const i = next++;
      if (blocked) { out[i] = {url: urls[i], status: 0, text: '', error: 'skipped after 403'}; continue; }
      for (let attempt = 0; ; attempt++) {
        try {
          const r = await fetch(urls[i], {headers: {'hx-request': 'true'}, credentials: 'omit'});
          if (r.status === 429 && attempt < 3) {
            const ra = Number(r.headers.get('retry-after')) || 0;
            await sleep(Math.max(ra * 1000, 5000 * (attempt + 1)));
            continue;
          }
          if (r.status === 403) blocked = true;
          out[i] = {url: urls[i], status: r.status, text: await r.text(), attempts: attempt + 1};
        } catch (e) {
          out[i] = {url: urls[i], status: 0, text: '', error: String(e)};
        }
        break;
      }
      await sleep(delayMs);
    }
  }
  await Promise.all(Array.from({length: concurrency}, worker));
  return out;
}
"""

JS_PARSE_LINEUP = """
(html) => {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  // Channels live inside <template id="channels-template">.
  const roots = [doc, ...[...doc.querySelectorAll('template')].map(t => t.content)];
  const links = new Map();
  roots.forEach(r => r.querySelectorAll('a[data-id][data-ch]').forEach(a => links.set(a.dataset.id, a)));
  return [...links.values()].map(a => {
    const m = (a.getAttribute('href') || '').match(/stn\\d+-[\\d.]+-(.+)$/);
    return {source_id: a.dataset.id, channel: a.dataset.ch,
            call_sign: m ? decodeURIComponent(m[1]) : (a.textContent || '').trim()};
  });
}
"""

JS_PARSE_AIRINGS = """
(html) => {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  return [...doc.querySelectorAll('.gridAiring')].map(el => {
    // Long airings repeat their label in a second "...Title" div (sticky
    // label while scrolling); the first child div is the real one.
    const label = el.querySelector(':scope > div') || el;
    const sub = label.querySelector('.gridSubtitle');
    const clone = label.cloneNode(true);
    clone.querySelectorAll('.gridSubtitle').forEach(s => s.remove());
    return {program_id: el.dataset.id, time_ms: Number(el.dataset.time),
            runtime: Number(el.dataset.runtime), qualifiers: el.dataset.qualifiers || '',
            title: clone.textContent.trim(), subtitle: sub ? sub.textContent.trim() : ''};
  });
}
"""

# Details dialog: <h1>title</h1> ... <h2>subtitle</h2> <p><span>Genre</span>...</p>
# ... <p>description</p> <p><span>year</span><br><span>venue</span></p>
JS_PARSE_PROGRAM = """
(html) => {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const panel = doc.querySelector('#main-panel') || doc.body;
  const txt = (el) => el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
  const paras = [...panel.querySelectorAll(':scope > p')].filter(p => !p.id);
  const isGenre = (p) => p.children.length > 0 && !p.querySelector('br') &&
                         [...p.children].every(c => c.tagName === 'SPAN');
  const genres = paras.filter(isGenre).flatMap(p => [...p.children].map(txt)).filter(Boolean);
  const description = paras.filter(p => !isGenre(p))
    .map(p => [...p.childNodes].map(n => n.nodeName === 'BR' ? ' ' : n.textContent).join('').replace(/\\s+/g, ' ').trim())
    .filter(Boolean).join('\\n');
  return {title: txt(doc.querySelector('h1')), subtitle: txt(panel.querySelector('h2')),
          genres, description};
}
"""


# ---------------------------------------------------------------- helpers ---
def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / "scrape.log", maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    log.addHandler(sh)
    log.addHandler(fh)


def utc_day_buckets(start_local, end_local):
    """UTC-midnight epoch-ms values whose 24h block overlaps [start, end)."""
    day = start_local.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end_utc = end_local.astimezone(timezone.utc)
    out = []
    while day < end_utc:
        out.append(int(day.timestamp() * 1000))
        day += timedelta(days=1)
    return out


def fetch_all(page, urls, concurrency=GRID_CONCURRENCY, delay_ms=GRID_DELAY_MS):
    return page.evaluate(JS_FETCH, {"urls": urls, "concurrency": concurrency, "delayMs": delay_ms})


def load_detail_cache():
    try:
        return json.loads(DETAIL_CACHE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_detail_cache(cache):
    DETAIL_CACHE.parent.mkdir(exist_ok=True)
    DETAIL_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))


def is_nfl(airing):
    text = f"{airing['title']} {airing['subtitle']}"
    return bool(INCLUDE_RE.search(text)) and not EXCLUDE_RE.search(text)


class Blocked(Exception):
    pass


def open_session(browser):
    ctx = browser.new_context(
        user_agent=USER_AGENT, locale="en-US", timezone_id="America/Los_Angeles",
        viewport={"width": 1366, "height": 900}, service_workers="block",
    )
    page = ctx.new_page()
    log.info("Loading %s", PAGE_URL)
    resp = page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60_000)
    status = resp.status if resp else None
    try:
        # The grid renders asynchronously; wait for a real airing cell.
        page.wait_for_selector(".gridAiring", timeout=45_000)
    except PlaywrightError:
        title = page.title()
        body = page.inner_text("body")[:300].replace("\n", " ")
        if "Attention Required" in title or "blocked" in body.lower() or status == 403:
            raise Blocked(f"Cloudflare blocked the headless browser (HTTP {status}, title {title!r}). "
                          "This may need a GUI (headed) environment.")
        raise RuntimeError(f"Guide grid never populated (HTTP {status}, title {title!r}): {body}")
    log.info("Page loaded (HTTP %s, title %r)", status, page.title())
    return page


# ------------------------------------------------------------------- main ---
def scrape():
    now = datetime.now(TZ)
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(days=DAYS_AHEAD + 1)
    days = utc_day_buckets(window_start, window_end)
    log.info("Window: %s through %s (Pacific), %d UTC day blocks",
             window_start.date(), (window_end - timedelta(days=1)).date(), len(days))

    status = {
        "scraped_at": now.isoformat(timespec="seconds"),
        "source": PAGE_URL,
        "window_start": window_start.date().isoformat(),
        "window_end": (window_end - timedelta(days=1)).date().isoformat(),
        "stations": [],
        "utc_days_published": [],
        "utc_days_not_published": [],
        "errors": [],
        "match_count": 0,
    }

    with sync_playwright() as p:
        # channel="chromium" = full Chromium in new headless mode. The default
        # headless_shell build is blocked by Cloudflare on this site.
        browser = p.chromium.launch(channel="chromium", headless=True,
                                    args=["--disable-blink-features=AutomationControlled"])
        try:
            page = open_session(browser)

            lineup = fetch_all(page, [f"/partial/lineup/{LINEUP}"])[0]
            if lineup["status"] != 200:
                raise RuntimeError(f"Lineup fetch failed: HTTP {lineup['status']}")
            channels = page.evaluate(JS_PARSE_LINEUP, lineup["text"])
            stations = [c for c in channels if c["channel"].split(".")[0] in TARGET_CHANNELS]
            log.info("Lineup has %d channels; %d target stations: %s", len(channels), len(stations),
                     ", ".join(f"{s['channel']} {s['call_sign']}" for s in stations))
            if not stations:
                raise RuntimeError("No target stations found in lineup (did channel numbers change?)")
            status["stations"] = [f"{s['channel']} {s['call_sign']}" for s in stations]

            # One UTC day at a time across all stations. The guide only
            # publishes ~9 days out (HTTP 400 beyond that); once a whole day
            # is unpublished, later days are too, so stop asking.
            airings = {}
            ok_fetches = total_fetches = 0
            for i, day in enumerate(days):
                label = datetime.fromtimestamp(day / 1000, timezone.utc).date().isoformat()
                results = fetch_all(page, [f"/partial/source/{day}/{s['source_id']}" for s in stations])
                total_fetches += len(results)
                ok = missing = 0
                for station, res in zip(stations, results):
                    if res["status"] == 200:
                        ok += 1
                        for a in page.evaluate(JS_PARSE_AIRINGS, res["text"]):
                            # Programs crossing block/day boundaries appear more than once.
                            airings[(station["source_id"], a["program_id"], a["time_ms"])] = (station, a)
                    elif res["status"] in (400, 404):
                        missing += 1
                    else:
                        msg = f"{res['url']}: HTTP {res['status']} {res.get('error', '')}".strip()
                        log.error("Fetch failed %s", msg)
                        status["errors"].append(msg)
                ok_fetches += ok
                log.info("UTC day %s: %d/%d stations OK%s", label, ok, len(stations),
                         f", {missing} not published yet" if missing else "")
                (status["utc_days_published"] if ok else status["utc_days_not_published"]).append(label)
                if any(r["status"] == 403 for r in results):
                    raise Blocked("Cloudflare started returning 403 mid-run (rate limit?). "
                                  "If this persists, it may need a GUI (headed) environment.")
                if missing == len(stations):
                    rest = [datetime.fromtimestamp(d / 1000, timezone.utc).date().isoformat() for d in days[i + 1:]]
                    status["utc_days_not_published"].extend(rest)
                    if rest:
                        log.info("Guide horizon reached; skipping %d later UTC days (%s..%s)",
                                 len(rest), rest[0], rest[-1])
                    break
            log.info("Fetched %d/%d station-days OK; %d unique airings on target stations",
                     ok_fetches, total_fetches, len(airings))
            if ok_fetches == 0:
                raise RuntimeError("Every station-day fetch failed; refusing to overwrite latest.json")

            start_ms = window_start.timestamp() * 1000
            end_ms = window_end.timestamp() * 1000
            matches = [
                (st, a) for st, a in airings.values()
                if start_ms <= a["time_ms"] < end_ms and is_nfl(a)
            ]
            matches.sort(key=lambda m: (m[1]["time_ms"], float(m[0]["channel"])))

            # Pull the program details dialog (description) for each matched program.
            # Cached for DETAIL_CACHE_TTL so a daily run only asks for new ones.
            cache = load_detail_cache()
            cutoff = (now - DETAIL_CACHE_TTL).isoformat()
            pids = sorted({a["program_id"] for _, a in matches})
            todo = [pid for pid in pids if cache.get(pid, {}).get("fetched_at", "") < cutoff]
            log.info("Details: %d matched programs, %d cached, fetching %d (%.0fs between requests)",
                     len(pids), len(pids) - len(todo), len(todo), DETAIL_DELAY_MS / 1000)
            for res, pid in zip(fetch_all(page, [f"/dlg/program?id={pid}" for pid in todo],
                                          concurrency=1, delay_ms=DETAIL_DELAY_MS), todo):
                if res["status"] == 200:
                    cache[pid] = {**page.evaluate(JS_PARSE_PROGRAM, res["text"]),
                                  "fetched_at": now.isoformat(timespec="seconds")}
                else:
                    msg = f"details {pid}: HTTP {res['status']} {res.get('error', '')}".strip()
                    log.warning("Could not fetch %s (description left empty; using stale cache if any)", msg)
                    status["errors"].append(msg)
            save_detail_cache({k: v for k, v in cache.items() if v.get("fetched_at", "") >= cutoff or k in pids})
            details = {pid: cache[pid] for pid in pids if pid in cache}
        finally:
            browser.close()

    entries = []
    for st, a in matches:
        start = datetime.fromtimestamp(a["time_ms"] / 1000, TZ)
        d = details.get(a["program_id"], {})
        entries.append({
            "date": start.date().isoformat(),
            "weekday": start.strftime("%A"),
            "start_time_pt": start.strftime("%-I:%M %p"),
            "start_iso": start.isoformat(),
            "runtime_minutes": a["runtime"],
            "channel": st["channel"],
            "call_sign": st["call_sign"],
            "station_group": TARGET_CHANNELS[st["channel"].split(".")[0]],
            "title": a["title"],
            "subtitle": a["subtitle"],
            "description": d.get("description", ""),
            "genres": d.get("genres", []),
            "qualifiers": a["qualifiers"],
            "program_id": a["program_id"],
        })
        log.info("MATCH %s %-8s %-5s %-9s %s%s", start.strftime("%a %Y-%m-%d"), start.strftime("%-I:%M%p"),
                 st["channel"], st["call_sign"], a["title"], f" | {a['subtitle']}" if a["subtitle"] else "")

    status["match_count"] = len(entries)
    return entries, status


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print JSON instead of writing files")
    args = ap.parse_args()
    setup_logging()
    log.info("=== scrape start ===")
    try:
        entries, status = scrape()
    except Blocked as e:
        log.critical("BLOCKED: %s", e)
        return 3
    except Exception:
        log.exception("Scrape failed; latest.json left untouched")
        return 1

    if args.dry_run:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
    else:
        OUTPUT.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
        STATUS_OUTPUT.write_text(json.dumps(status, indent=2) + "\n")
        log.info("Wrote %d entries to %s", len(entries), OUTPUT)
    if status["errors"]:
        log.warning("%d non-fatal errors: %s", len(status["errors"]), status["errors"])
    log.info("=== scrape done: %d matches ===", len(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
