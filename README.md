# vegas-nfl-tv-listings

Once a day, this scrapes the over-the-air TV guide for Las Vegas (zip 89118)
from [tvtv.us](https://www.tvtv.us/nv/las-vegas/89118/luUSA-OTA89118). It
keeps every airing on the FOX, CBS, NBC and ABC stations whose title or
subtitle mentions **NFL** or **Football**, and commits them, verbatim, to
`latest.json`.

The script doesn't interpret anything: there's no team matching and no
local-vs-streaming logic. Another process reads `latest.json` and does that.
Git history on `latest.json` is the audit trail.

## Files

| File | What |
|---|---|
| `latest.json` | Array of matched airings, overwritten each run (schema below) |
| `last-run.json` | Run metadata: when, which stations, which days the guide had published, errors |
| `scrape.py` | The scraper |
| `run.sh` | Daily wrapper: `git pull` → scrape → commit → push |
| `logs/` | `scrape.log` (rotating) and `cron.log`. Not in git |
| `cache/` | Cached program descriptions (2-day TTL). Not in git |

### `latest.json` entry

```json
{
  "date": "2026-10-04",
  "weekday": "Sunday",
  "start_time_pt": "1:25 PM",
  "start_iso": "2026-10-04T13:25:00-07:00",
  "runtime_minutes": 185,
  "channel": "8.1",
  "call_sign": "KLASDT",
  "station_group": "CBS (KLAS)",
  "title": "NFL Football",
  "subtitle": "Kansas City Chiefs at Las Vegas Raiders",
  "description": "The Las Vegas Raiders host the Kansas City Chiefs at Allegiant Stadium ...",
  "genres": ["Football"],
  "qualifiers": "Live,CC,HD 1080i,HDTV,Stereo",
  "program_id": "EP000031285740"
}
```

For games, the matchup is in `subtitle`. `title`, `subtitle` and
`description` are exactly as tvtv.us shows them. Times are Pacific. Every
subchannel of the four stations is included (e.g. 5.2 KVVUDT2, which runs
classic college games), and so are shows such as "FOX NFL Sunday", "College
Football" and "IFL Football". The downstream reader decides what matters.

## How it works

- **Cloudflare.** tvtv.us is behind Cloudflare. Plain HTTP (`curl`,
  `requests`) gets a 403 on every page and endpoint, and so does Playwright's
  default `chromium-headless-shell`. Full Chromium in new headless mode
  (`channel="chromium"`) passes, so no GUI is needed.
- **No JSON API.** The grid is built from HTML fragments that the page fetches
  htmx-style with an `hx-request: true` header:
  - `/partial/lineup/USA-OTA89118`: channel list, with each channel's
    `source_id`.
  - `/partial/source/<UTC-midnight-epoch-ms>/<source_id>`: one UTC day of one
    station. Each `.gridAiring` cell has `data-time` (UTC ms),
    `data-runtime`, `data-qualifiers`, the title and a `.gridSubtitle`.
  - `/dlg/program?id=<program_id>`: the details dialog, which holds the
    description.
- **Hybrid approach.** The script loads the guide page once, waits for a real
  `.gridAiring` cell, then calls those endpoints with `fetch()` from inside
  the page and parses them with `DOMParser`. That reuses the browser's
  Cloudflare clearance without scrolling the grid.
- **Guide horizon.** tvtv.us publishes only about 9 days ahead and returns
  HTTP 400 past that. The script asks for every day from today through
  today + 14 and stops at the first day nothing is published for; it records
  those days in `last-run.json` under `utc_days_not_published`. Later days
  are picked up automatically once the guide adds them.
- **Rate limits.** The details dialog returns 429, then a Cloudflare 403, if
  you hit it quickly. Details are fetched one at a time with 2s gaps and
  cached for 2 days, and any 403 aborts the run (exit 3).
- **Keyword filter.** The filter runs on title and subtitle:
  `\bnfl|football`, case-insensitive. "NFL" must start a word because a bare
  substring also matches "Drago**nFl**yTV" and "I**nfl**uential". Matching on
  the description too would need one details fetch per airing (thousands), so
  descriptions are only fetched for airings that already matched.
- **Failure handling.** A failed or blocked run leaves `latest.json` untouched
  and commits nothing.

## Running

Runs on Edgar (`~/NFL`) from cron, daily at 05:30 Pacific (the box's
timezone is America/Los_Angeles):

```cron
30 5 * * * /home/msimone/NFL/run.sh >> /home/msimone/NFL/logs/cron.log 2>&1
```

Manual run:

```bash
cd ~/NFL
./run.sh                          # scrape + commit + push
.venv/bin/python scrape.py        # scrape only, writes latest.json
.venv/bin/python scrape.py --dry-run   # print JSON, write nothing
```

Check on it with `tail -50 ~/NFL/logs/cron.log`, or look at `last-run.json`.
`run.sh` exit codes: 0 ok, 1 scrape error, 3 blocked by Cloudflare, 4 git
error.

### Setup on a new box

```bash
git clone git@github.com:mikesimone/vegas-nfl-tv-listings.git ~/NFL && cd ~/NFL
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium
```

On Windows, run `scrape.py` from Task Scheduler with the same steps. `run.sh`
needs Git Bash. If Cloudflare ever starts blocking headless, run with a
headed browser on a desktop session.

## Changing things

All settings are at the top of `scrape.py`:

- **Zip / market.** Set `ZIP` and `LINEUP`. Find the lineup id by browsing to
  your zip on tvtv.us and copying the `lu<LINEUP>` part of the URL, e.g.
  `.../89118/luUSA-OTA89118` gives `USA-OTA89118`. Cable and satellite
  lineups have their own ids. Change `PAGE_URL` if the city slug differs.
- **Channels.** `TARGET_CHANNELS` maps major channel numbers to a label.
  Every subchannel `N.x` of a listed major is scanned. Add `"10": "PBS
  (KLVX)"` for example, or restrict to main channels only by filtering
  `stations` to `.endswith(".1")`.
- **Keywords.** `KEYWORD_RE`.
- **Lookahead.** `DAYS_AHEAD`.
- **Schedule.** `crontab -e`.
