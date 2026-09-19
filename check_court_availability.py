"""
Tokyo park tennis court availability checker (テニス（人工芝）)

What it does
------------
1. Opens the PUBLIC search page for each park you list (no login needed
   just to check availability)
2. For each park, searches テニス（人工芝） and walks forward week by
   week through the calendar, staying within the CURRENT month
3. Looks for cells marked "空き" (available), only for dates strictly
   after today, and (optionally) only at times you care about
4. If any open slot is found, sends a Telegram message to you (plain
   HTTPS, so it isn't blocked by networks that block email/SMTP ports)
5. Exits. Run it on a schedule (e.g. every 30 minutes) with Windows
   Task Scheduler — see the setup guide.

Setup
-----
    pip install playwright
    playwright install chromium

Create a Telegram bot via @BotFather and get your chat ID (see chat for
the walkthrough). Then create a file called "config.txt" in the SAME
FOLDER as this script, with:

    TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuVwxyZ
    TELEGRAM_CHAT_ID=987654321

Optional settings (add these lines if you want them):

    WATCH=芝公園@17:00,19:00;日比谷公園@19:00;浮間公園@19:00
        Lets you set a DIFFERENT time filter per park. Separate parks
        with ";", and within a park put the name and its times joined
        by "@", times separated by ",". If a park has no times after
        the "@" (e.g. "芝公園@"), it means "any time" for that park.
        If WATCH is set, it overrides BUILDINGS and TIMES below.

    WEEKEND_ALL_DAY=芝公園
        Comma-separated list of park names where, on Saturdays, Sundays,
        AND Japanese public holidays (法定節日／祝日 — checked via the
        jpholiday library, so e.g. a holiday that falls on a Wednesday
        counts too), the normal time filter (from WATCH or TIMES) is
        ignored and ANY available time counts. Ordinary (non-holiday)
        weekdays for that park still follow the normal filter. Leave
        unset if you don't need this.

    BUILDINGS=芝公園,日比谷公園
        Simple case: same time filter for every park. Comma-separated
        list of park names to check (see BUILDING_CODES below for the
        full list of supported names). Defaults to 芝公園 if not set.
        Ignored if WATCH is set.

    TIMES=15:00,17:00,19:00
        Goes with BUILDINGS above: comma-separated list of time slots
        you want to be notified about (from: 09:00, 11:00, 13:00,
        15:00, 17:00, 19:00). If not set, any time counts. Ignored if
        WATCH is set.

    LOG_RETENTION_DAYS=7
        The script keeps a running log.txt of every check. Once a day
        (the first run after midnight), entries older than this many
        days are automatically deleted so the file doesn't grow
        forever. Defaults to 7 if not set.

    SKIP_ON_RAIN=true
        If true (the default), before notifying about a slot the
        script checks the RAIN FORECAST for that specific slot's time
        window — 2 hours before the slot starts, through the 2-hour
        usage window itself, through 2 hours after it ends (so a
        6-hour window total). If the predicted rain chance in that
        window is too high, that slot is skipped (but still logged) —
        other slots with better weather still get notified normally.
        Set to "false" to always notify regardless of weather.

    RAIN_PROBABILITY_THRESHOLD=30
        Used with SKIP_ON_RAIN. The max acceptable rain probability
        (percent, 0-100) anywhere in the 6-hour window before a slot
        is skipped. Lower = stricter (fewer notifications, but higher
        confidence of dry weather). Defaults to 30 if not set.

    DEBUG=true
        If true, on ANY error the script saves a screenshot
        (debug_error.png) and the full page HTML (debug_error.html)
        next to this script, plus prints extra detail about what step
        it was on when it failed. Works fine in headless mode (e.g.
        GitHub Actions) — turn this on any time you want to see what
        the page actually looked like when something broke.

        DEBUG also turns on two extra diagnostics, unconditionally
        (not just on error), which is useful right now while we're
        tracking down a "some time slots never get checked" issue:
          - Every AJAX response body is logged IN FULL (no longer
            truncated to 500 chars).
          - The very first time the calendar successfully loads for
            the first park in this run, the script dumps the full
            page HTML + a screenshot to debug_calendar_sample.html /
            .png, and logs which time-slot codes it actually found
            rendered as clickable cells. This tells us whether the
            site is only rendering some time-of-day rows (e.g. only
            9:00) by the time we read the DOM.

    HEADED=true
        If true, runs with a VISIBLE browser window instead of
        headless. Only works on your own machine with a display —
        never set this on GitHub Actions or any other headless CI
        runner, the browser launch will just fail there.

You do NOT need to put your login ID/password anywhere — checking
availability doesn't require logging in. You'd only log in yourself,
manually, at the very end to actually make the booking.

Run once manually first, with DEBUG=true (and HEADED=true if you're on
your own machine with a display) to confirm each click/selector still
matches the live site before you schedule it unattended.
"""

import os
import re
import sys
import json
import time
import datetime
import urllib.request
import urllib.parse

import jpholiday
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEARCH_URL = "https://kouen.sports.metro.tokyo.lg.jp/web/index.jsp"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.txt")
DEBUG_SCREENSHOT_PATH = os.path.join(SCRIPT_DIR, "debug_error.png")
DEBUG_HTML_PATH = os.path.join(SCRIPT_DIR, "debug_error.html")

# One-time diagnostic dump of the first successfully-loaded calendar page,
# used while we're tracking down why some time slots never get detected.
DEBUG_CALENDAR_HTML_PATH = os.path.join(SCRIPT_DIR, "debug_calendar_sample.html")
DEBUG_CALENDAR_SCREENSHOT_PATH = os.path.join(SCRIPT_DIR, "debug_calendar_sample.png")

# Purpose ("sport category") codes, as seen in the #purpose-home dropdown
PURPOSE_ARTIFICIAL_TURF = "1000_1030"  # テニス（人工芝）
PURPOSE_HARD_COURT = "1000_1020"       # テニス（ハード）

# Park/facility name -> (purpose_value, building_code). The building
# dropdown (#bname-home) is refiltered by the site depending on which
# purpose is selected, and for most parks the same building id works
# under either purpose — EXCEPT Ariake, where each court type is its own
# separate facility with its own id. So we store the purpose alongside
# the building code per entry, rather than assuming one global purpose.
BUILDING_INFO = {
    # --- テニス（人工芝） (artificial turf) ---
    "日比谷公園": (PURPOSE_ARTIFICIAL_TURF, "1000"),
    "芝公園": (PURPOSE_ARTIFICIAL_TURF, "1010"),
    "猿江恩賜公園": (PURPOSE_ARTIFICIAL_TURF, "1040"),
    "亀戸中央公園": (PURPOSE_ARTIFICIAL_TURF, "1050"),
    "木場公園": (PURPOSE_ARTIFICIAL_TURF, "1060"),
    "祖師谷公園": (PURPOSE_ARTIFICIAL_TURF, "1070"),
    "東白鬚公園": (PURPOSE_ARTIFICIAL_TURF, "1090"),
    "浮間公園": (PURPOSE_ARTIFICIAL_TURF, "1100"),
    "城北中央公園": (PURPOSE_ARTIFICIAL_TURF, "1110"),
    "赤塚公園": (PURPOSE_ARTIFICIAL_TURF, "1120"),
    "東綾瀬公園": (PURPOSE_ARTIFICIAL_TURF, "1130"),
    "舎人公園": (PURPOSE_ARTIFICIAL_TURF, "1140"),
    "篠崎公園Ａ": (PURPOSE_ARTIFICIAL_TURF, "1150"),
    "大島小松川公園": (PURPOSE_ARTIFICIAL_TURF, "1160"),
    "汐入公園": (PURPOSE_ARTIFICIAL_TURF, "1170"),
    "高井戸公園": (PURPOSE_ARTIFICIAL_TURF, "1175"),
    "善福寺川緑地": (PURPOSE_ARTIFICIAL_TURF, "1180"),
    "光が丘公園": (PURPOSE_ARTIFICIAL_TURF, "1190"),
    "石神井公園Ｂ": (PURPOSE_ARTIFICIAL_TURF, "1205"),
    "井の頭恩賜公園": (PURPOSE_ARTIFICIAL_TURF, "1220"),
    "武蔵野中央公園": (PURPOSE_ARTIFICIAL_TURF, "1230"),
    "小金井公園": (PURPOSE_ARTIFICIAL_TURF, "1240"),
    "野川公園": (PURPOSE_ARTIFICIAL_TURF, "1260"),
    "府中の森公園": (PURPOSE_ARTIFICIAL_TURF, "1270"),
    "東大和南公園": (PURPOSE_ARTIFICIAL_TURF, "1280"),
    "大井ふ頭海浜公園Ｂ": (PURPOSE_ARTIFICIAL_TURF, "1315"),
    "有明テニスＣ人工芝コート": (PURPOSE_ARTIFICIAL_TURF, "1360"),

    # --- テニス（ハード） (hard court) ---
    "大井ふ頭海浜公園Ａ（ハード）": (PURPOSE_HARD_COURT, "1310"),
    "大井ふ頭海浜公園Ｂ（ハード）": (PURPOSE_HARD_COURT, "1315"),
    "有明テニスＡ屋外ハードコート": (PURPOSE_HARD_COURT, "1350"),
    "有明テニスＢインドアコート": (PURPOSE_HARD_COURT, "1370"),
}

# Calendar time-slot code (the part of a cell id after the underscore,
# e.g. "..._30") -> human-readable time
SLOT_TIMES = {
    "10": "09:00",
    "20": "11:00",
    "30": "13:00",
    "40": "15:00",
    "50": "17:00",
    "60": "19:00",
}

# Monday=0 ... Sunday=6 (matches date.weekday()), used to show the day of
# the week next to each date in Telegram notifications so it's easier to
# tell at a glance whether a slot is on a weekday or weekend.
WEEKDAY_KANJI = ["月", "火", "水", "木", "金", "土", "日"]

DEFAULT_BUILDING = "芝公園"

# Tokyo center coordinates, used for the rain check (SKIP_ON_RAIN)
WEATHER_LAT = 35.6895
WEATHER_LON = 139.6917

SLOT_DURATION_HOURS = 2   # each booking is a 2-hour block
WEATHER_BUFFER_HOURS = 2  # also check this many hours before/after the slot
DEFAULT_RAIN_THRESHOLD = 30  # percent

LOG_PATH = os.path.join(SCRIPT_DIR, "log.txt")
ROTATION_MARKER_PATH = os.path.join(SCRIPT_DIR, ".last_log_rotation")
DEFAULT_LOG_RETENTION_DAYS = 7

# Which watched park to START checking from next run. This file is
# committed back into the repo by the GitHub Actions workflow after each
# run (see the "Persist rotation state" step), because a fresh
# `actions/checkout` gives us a clean working directory every time — a
# plain temp file here would NOT survive between runs on its own.
#
# Why this exists: check_availability() stops checking further parks as
# soon as ANY park has an available slot (so you get notified — and can
# go book — as fast as possible, see check_availability()'s docstring).
# Without this rotation, whichever park happens to be checked first would
# "starve out" the rest forever: if that park keeps having openings, the
# later-listed parks in WATCH would never get checked again. Rotating the
# starting point each run guarantees every watched park eventually gets a
# turn, while still keeping the "stop at the first find" speed benefit.
NEXT_PARK_STATE_PATH = os.path.join(SCRIPT_DIR, "next_park_state.txt")

MAX_WEEKS_TO_CHECK = 6  # safety cap; loop also stops once it leaves the current month

REQUIRED_KEYS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]

# Common cookie/agreement dialog selectors seen on JP municipal sites.
# We try each one briefly; if none show up, we just move on.
COOKIE_DIALOG_SELECTORS = [
    "text=同意する",
    "text=OK",
    "text=閉じる",
    "#agree-btn",
    "#cookie-agree",
    ".modal-close",
]

# Module-level flag: have we already done the one-time calendar diagnostic
# dump this run? (Simpler and more explicit than stashing state on a
# function object.) Reset at the top of check_availability() each run.
_calendar_dump_done = False


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"Could not find config.txt at: {CONFIG_PATH}")
        print("Create it in the same folder as this script. See the top of this file for the format.")
        sys.exit(1)

    config = {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                print(f"config.txt line {line_num} looks wrong (no '='): {line}")
                sys.exit(1)
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()

    missing = [k for k in REQUIRED_KEYS if not config.get(k)]
    if missing:
        print(f"config.txt is missing: {', '.join(missing)}")
        sys.exit(1)

    watch_raw = config.get("WATCH", "").strip()
    watch = {}

    if watch_raw:
        for entry in watch_raw.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            if "@" not in entry:
                print(f"WATCH entry looks wrong (missing '@'): {entry}")
                print('Expected format like: 芝公園@17:00,19:00')
                sys.exit(1)
            name, times_str = entry.split("@", 1)
            name = name.strip()
            times = {t.strip() for t in times_str.split(",") if t.strip()}
            watch[name] = times
    else:
        buildings_raw = config.get("BUILDINGS", DEFAULT_BUILDING)
        building_names = [b.strip() for b in buildings_raw.split(",") if b.strip()]
        times_raw = config.get("TIMES", "")
        time_filter = {t.strip() for t in times_raw.split(",") if t.strip()}
        for name in building_names:
            watch[name] = set(time_filter)

    unknown_parks = [name for name in watch if name not in BUILDING_INFO]
    if unknown_parks:
        print(f"Unknown park name(s): {', '.join(unknown_parks)}")
        print(f"Supported names: {', '.join(BUILDING_INFO.keys())}")
        sys.exit(1)

    all_times = set()
    for times in watch.values():
        all_times |= times
    unknown_times = [t for t in all_times if t not in SLOT_TIMES.values()]
    if unknown_times:
        print(f"Unknown time(s): {', '.join(unknown_times)}")
        print(f"Supported times: {', '.join(SLOT_TIMES.values())}")
        sys.exit(1)

    config["_watch"] = watch

    weekend_raw = config.get("WEEKEND_ALL_DAY", "").strip()
    weekend_all_day = {name.strip() for name in weekend_raw.split(",") if name.strip()}
    unknown_weekend = [name for name in weekend_all_day if name not in BUILDING_INFO]
    if unknown_weekend:
        print(f"Unknown park name(s) in WEEKEND_ALL_DAY: {', '.join(unknown_weekend)}")
        print(f"Supported names: {', '.join(BUILDING_INFO.keys())}")
        sys.exit(1)
    config["_weekend_all_day"] = weekend_all_day

    log_retention_raw = config.get("LOG_RETENTION_DAYS", "").strip()
    if log_retention_raw:
        try:
            config["_log_retention_days"] = int(log_retention_raw)
        except ValueError:
            print(f"LOG_RETENTION_DAYS should be a whole number, got: {log_retention_raw}")
            sys.exit(1)
    else:
        config["_log_retention_days"] = DEFAULT_LOG_RETENTION_DAYS

    skip_on_rain_raw = config.get("SKIP_ON_RAIN", "true").strip().lower()
    config["_skip_on_rain"] = skip_on_rain_raw not in ("false", "0", "no", "off")

    threshold_raw = config.get("RAIN_PROBABILITY_THRESHOLD", "").strip()
    if threshold_raw:
        try:
            config["_rain_threshold"] = int(threshold_raw)
        except ValueError:
            print(f"RAIN_PROBABILITY_THRESHOLD should be a whole number, got: {threshold_raw}")
            sys.exit(1)
    else:
        config["_rain_threshold"] = DEFAULT_RAIN_THRESHOLD

    # DEBUG only controls extra logging + a screenshot/HTML dump on error
    # (plus, for now, the full-body AJAX logging and the one-time calendar
    # sample dump described at the top of this file). It does NOT force a
    # visible browser — headless CI runners (e.g. GitHub Actions) have no
    # display and would crash if we tried. Screenshots work fine in
    # headless mode too.
    debug_raw = config.get("DEBUG", "false").strip().lower()
    config["_debug"] = debug_raw in ("true", "1", "yes", "on")

    # HEADED forces a visible browser window. Only use this on your own
    # machine with a display — never set it true in GitHub Actions or any
    # other headless CI runner, or the browser launch will fail.
    headed_raw = config.get("HEADED", "false").strip().lower()
    config["_headed"] = headed_raw in ("true", "1", "yes", "on")

    # STOP_ON_FIRST_FIND=true (default): as soon as ANY watched park has an
    # available slot, notify immediately and stop checking the rest of the
    # watched parks for this run (they'll be checked next run — see the
    # rotation logic in check_availability so this doesn't starve
    # later-listed parks). Prioritizes speed: fewer seconds between "a slot
    # opened up" and "you got the Telegram message", at the cost of not
    # checking every park every run.
    #
    # STOP_ON_FIRST_FIND=false: check EVERY watched park every run,
    # regardless of what earlier ones found. Still notifies immediately
    # (one Telegram message per park) the moment each park's check
    # finishes and has availability — it just doesn't stop the run early.
    # Slower per run (checks all parks every time) but guarantees every
    # park gets checked on every single run.
    stop_on_first_find_raw = config.get("STOP_ON_FIRST_FIND", "true").strip().lower()
    config["_stop_on_first_find"] = stop_on_first_find_raw not in ("false", "0", "no", "off")

    return config


# ---------------------------------------------------------------------------
# Logging (with automatic cleanup of old entries)
# ---------------------------------------------------------------------------

def log_event(message: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"(could not write to log.txt: {e})")


def _already_rotated_today() -> bool:
    today_str = datetime.date.today().isoformat()
    if not os.path.exists(ROTATION_MARKER_PATH):
        return False
    try:
        with open(ROTATION_MARKER_PATH, "r", encoding="utf-8") as f:
            return f.read().strip() == today_str
    except Exception:
        return False


def _mark_rotated_today():
    try:
        with open(ROTATION_MARKER_PATH, "w", encoding="utf-8") as f:
            f.write(datetime.date.today().isoformat())
    except Exception:
        pass


def rotate_log_if_new_day(retention_days: int):
    if _already_rotated_today():
        return
    rotate_log(retention_days)
    _mark_rotated_today()


def rotate_log(retention_days: int):
    if not os.path.exists(LOG_PATH):
        return

    cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)

    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return

    kept_lines = []
    removed = 0
    for line in lines:
        m = re.match(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
        if m:
            try:
                ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                if ts < cutoff:
                    removed += 1
                    continue
            except ValueError:
                pass
        kept_lines.append(line)

    if removed:
        try:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(kept_lines)
        except Exception:
            pass


def load_next_park_name():
    """Returns the park name saved (by save_next_park_name) at the end of
    the previous run, or None if there's no saved state yet (first-ever
    run, or the state file was deleted/never committed)."""
    if not os.path.exists(NEXT_PARK_STATE_PATH):
        return None
    try:
        with open(NEXT_PARK_STATE_PATH, "r", encoding="utf-8") as f:
            name = f.read().strip()
        return name or None
    except Exception as e:
        log_event(f"(could not read {NEXT_PARK_STATE_PATH}: {e})")
        return None


def save_next_park_name(name: str):
    """Records which park the NEXT run should start checking from. The
    GitHub Actions workflow commits this file back into the repo after
    each run so it actually persists (see NEXT_PARK_STATE_PATH)."""
    try:
        with open(NEXT_PARK_STATE_PATH, "w", encoding="utf-8") as f:
            f.write(name)
    except Exception as e:
        log_event(f"(could not write {NEXT_PARK_STATE_PATH}: {e})")


# ---------------------------------------------------------------------------
# Weather check
# ---------------------------------------------------------------------------

def _get_hourly_rain_probabilities(date_obj: datetime.date, lat: float = WEATHER_LAT, lon: float = WEATHER_LON):
    date_str = date_obj.isoformat()
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&hourly=precipitation_probability"
        f"&timezone=Asia%2FTokyo&start_date={date_str}&end_date={date_str}"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        times = data.get("hourly", {}).get("time", [])
        probs = data.get("hourly", {}).get("precipitation_probability", [])
        result = {}
        for t, p in zip(times, probs):
            hour = int(t.split("T")[1].split(":")[0])
            result[hour] = p
        return result or None
    except Exception as e:
        log_event(f"(weather forecast fetch failed for {date_str}: {e})")
        return None


def is_slot_weather_ok(slot_date: datetime.date, slot_time: str, threshold: int) -> bool:
    if not slot_time or ":" not in slot_time:
        return True

    try:
        start_hour = int(slot_time.split(":")[0])
    except ValueError:
        return True

    hourly_probs = _get_hourly_rain_probabilities(slot_date)
    if hourly_probs is None:
        return True

    window_start = start_hour - WEATHER_BUFFER_HOURS
    window_end = start_hour + SLOT_DURATION_HOURS + WEATHER_BUFFER_HOURS
    relevant_probs = [
        hourly_probs[h] for h in range(window_start, window_end)
        if h in hourly_probs
    ]
    if not relevant_probs:
        return True

    return max(relevant_probs) < threshold


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

def send_telegram(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

def save_debug_snapshot(page, label: str):
    try:
        page.screenshot(path=DEBUG_SCREENSHOT_PATH, full_page=True)
        with open(DEBUG_HTML_PATH, "w", encoding="utf-8") as f:
            f.write(page.content())
        log_event(
            f"[debug] Saved snapshot for '{label}' -> "
            f"{DEBUG_SCREENSHOT_PATH} and {DEBUG_HTML_PATH} "
            f"(current URL: {page.url})"
        )
    except Exception as snap_err:
        log_event(f"[debug] Could not save debug snapshot: {snap_err}")


def dump_calendar_sample_once(page, building_name: str):
    """One-time (per run) diagnostic dump of a successfully-loaded calendar
    page: full HTML, a screenshot, and which time-slot codes are actually
    present as clickable cells right now. This is here specifically to
    figure out whether the site is only rendering some time-of-day rows
    (e.g. only 9:00) by the time we read the DOM, which would explain any
    time slot silently never being checked regardless of real
    availability. Safe to call every time; it no-ops after the first
    successful dump in a given run."""
    global _calendar_dump_done
    if _calendar_dump_done:
        return

    try:
        with open(DEBUG_CALENDAR_HTML_PATH, "w", encoding="utf-8") as f:
            f.write(page.content())
        page.screenshot(path=DEBUG_CALENDAR_SCREENSHOT_PATH, full_page=True)

        # NOTE: the site's calendar <td> cells no longer carry an
        # onclick="...setReserv..." attribute (confirmed via a debug dump
        # on 2026-09-17) — clicking is apparently handled by a listener
        # attached higher up rather than inline per-cell. Cells are still
        # identified by id="YYYYMMDD_SS" (date_slotcode), so we match on
        # that instead. `cells` here may include a few non-calendar <td>s
        # with unrelated ids (e.g. header cells) — that's fine, the caller
        # already validates the id format (8-digit date + known slot code)
        # before using anything.
        cells = page.query_selector_all("td[id]")
        suffixes = set()
        for c in cells:
            cell_id = c.get_attribute("id")
            if cell_id and "_" in cell_id:
                suffixes.add(cell_id.split("_", 1)[1])

        log_event(
            f"[debug] Dumped calendar sample for {building_name} -> "
            f"{DEBUG_CALENDAR_HTML_PATH} / {DEBUG_CALENDAR_SCREENSHOT_PATH}. "
            f"Found {len(cells)} setReserv cell(s); distinct time-slot "
            f"codes present in the DOM right now: {sorted(suffixes)} "
            f"(for reference, SLOT_TIMES = {SLOT_TIMES})"
        )
        _calendar_dump_done = True
    except Exception as dump_err:
        log_event(f"[debug] Calendar sample dump failed: {dump_err}")


def dismiss_cookie_dialog_if_present(page):
    for selector in COOKIE_DIALOG_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1000):
                locator.click(timeout=2000)
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    return False


def attach_ajax_logger(page, debug: bool):
    """In debug mode, logs every request/response to a *.do endpoint (the
    site's server actions) so we can see whether the week/month calendar
    AJAX call actually fires, and what it comes back with, without needing
    to reproduce the site locally.

    Response bodies are truncated to 500 chars to keep the log readable —
    this was temporarily widened to the full body while diagnosing the
    2026-09-17 issue (root cause found: the calendar <td> cells had lost
    their onclick="...setReserv..." attribute after a site redesign; see
    dump_calendar_sample_once()). Widen it again (e.g. body[:2000] or
    body[:] for no limit) if you need to dig into a future AJAX-shaped
    issue."""
    if not debug:
        return

    def on_request(request):
        if ".do" in request.url:
            log_event(f"[debug][ajax->] {request.method} {request.url}")

    def on_response(response):
        if ".do" in response.url:
            try:
                body = response.text()
            except Exception as e:
                body = f"(could not read body: {e})"
            snippet = body[:500].replace("\n", " ")
            log_event(
                f"[debug][ajax<-] {response.status} {response.url} "
                f"body[:500]={snippet!r}"
            )

    page.on("request", on_request)
    page.on("response", on_response)


# Keywords that indicate the site itself is showing a maintenance/blocked
# notice rather than the normal reservation UI. Kept broad but specific
# enough to avoid false positives on ordinary error messages.
MAINTENANCE_KEYWORDS = [
    "現在、ご指定のページはアクセスできません",  # "this page can't be accessed right now"
    "しばらく経ってから、アクセスしてください",   # "please try again later"
    "施設予約システムからのお知らせ",             # the blocked interstitial's own page heading/title
]
# NOTE: we deliberately do NOT match on bare words like "メンテナンス" —
# the site's normal homepage has a "お知らせ" (notices) list that includes
# routine, HISTORICAL announcements mentioning past maintenance windows
# (e.g. "2026/09/03 スポレクシステムのメンテナンスのお知らせ"). That text is
# always there even when the site is working completely normally, so
# matching on it caused false positives — the script kept thinking the
# site was down for maintenance when it was actually fine. The three
# phrases above are specific to the actual full-page "can't access this /
# come back later" block we've verified only appears when something is
# genuinely wrong.

# The blocked/notice interstitial has a "ホームへ" (back to home) button that
# just does location.href='/web/index.jsp'. Manually testing showed that
# reloading the SAME stuck URL keeps failing, but clicking this button (an
# in-page navigation back to the home page) recovers cleanly. So we try
# clicking it automatically before giving up and treating this as a full
# site outage.
HOME_BUTTON_SELECTOR = "#btn-light, button:has-text('ホームへ')"


class MaintenanceDetected(Exception):
    """Raised when the site appears to be down for maintenance (or stuck on
    a blocked interstitial we couldn't click our way out of), so the caller
    can skip this run quietly instead of treating it as a script bug."""
    pass


def _matched_maintenance_keyword(content: str):
    for keyword in MAINTENANCE_KEYWORDS:
        idx = content.find(keyword)
        if idx != -1:
            return keyword, idx
    return None, -1


def _log_match_context(content: str, keyword: str, idx: int):
    """Logs the raw HTML surrounding a matched keyword so we can tell
    whether it's a real, currently-active notice or just incidental text
    (e.g. a static FAQ line, a JS variable name, a hidden/collapsed block)
    that happens to contain the same words."""
    start = max(0, idx - 150)
    end = min(len(content), idx + len(keyword) + 150)
    snippet = content[start:end].replace("\n", " ")
    log_event(f"[maintenance-check] matched {keyword!r}, context: ...{snippet}...")


def try_recover_via_home_button(page, debug=False) -> bool:
    """Attempts the same recovery a human found worked by hand: click the
    'ホームへ' button (real in-page navigation) instead of reloading.
    Returns True if we're back on a normal (non-blocked) page afterward."""
    try:
        home_button = page.locator(HOME_BUTTON_SELECTOR).first
        if not home_button.is_visible(timeout=2000):
            log_event("[maintenance-check] No 'ホームへ' button found on this page to click")
            return False
    except Exception:
        return False

    log_event("[maintenance-check] Blocked/notice page detected — clicking 'ホームへ' to try to recover")

    try:
        home_button.click(timeout=5000)
        page.wait_for_load_state("networkidle")
    except Exception as e:
        log_event(f"[maintenance-check] Clicking home button failed: {e}")
        return False

    try:
        content = page.content()
    except Exception:
        return False

    keyword, idx = _matched_maintenance_keyword(content)
    if keyword:
        log_event("[maintenance-check] Still showing the notice after clicking home:")
        _log_match_context(content, keyword, idx)
        return False  # still stuck even after clicking home

    log_event("[maintenance-check] Recovered — back on a normal page after clicking home button")
    return True


def check_for_maintenance(page, debug=False) -> bool:
    """Checks whether the current page is showing a maintenance/blocked
    notice. If so, first tries clicking the 'ホームへ' recovery button:
      - If that works, returns True (caller should treat this as 'we just
        navigated back to the home page mid-flow, please restart from
        there') instead of raising.
      - If the notice is still showing (or there's no home button to
        click), raises MaintenanceDetected as before.
    Returns False if there was no maintenance/blocked notice at all."""
    try:
        content = page.content()
    except Exception:
        return False

    keyword, idx = _matched_maintenance_keyword(content)
    if not keyword:
        return False

    log_event(f"[maintenance-check] Page at {page.url} matched a maintenance/blocked keyword.")
    _log_match_context(content, keyword, idx)
    if debug:
        save_debug_snapshot(page, "maintenance_keyword_matched")

    if try_recover_via_home_button(page, debug=debug):
        return True

    raise MaintenanceDetected(
        f"Site shows a maintenance/blocked notice (matched keyword: {keyword!r}) "
        f"at {page.url}, and clicking the home button did not recover it."
    )


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class _RestartBuildingCheck(Exception):
    """Internal signal: we recovered from a blocked page mid-flow by
    clicking 'ホームへ', which lands us back on index.jsp — so the per-park
    flow (select purpose, select park, search, read calendar) needs to
    start over from the top rather than continuing from wherever it was."""
    pass


MAX_RECOVERY_RESTARTS = 2  # how many times to restart a single park's check
                            # after an in-flow recovery, before giving up
RETRY_PAUSE_MS = 5000      # pause before retrying a failed park
PARK_PAUSE_MS = 3000       # pause between parks, to avoid a request burst


def _check_building_once(page, building_name, purpose_value, building_code, today, current_year, current_month, time_filter, weekend_unrestricted, debug=False):
    """One attempt at checking a single park. Raises _RestartBuildingCheck if
    a blocked page was recovered from mid-flow (caller should retry from the
    top), or MaintenanceDetected if it couldn't recover at all."""
    found = set()

    page.goto(SEARCH_URL, wait_until="networkidle")
    if check_for_maintenance(page, debug=debug):
        # Recovered — we're freshly back on index.jsp now, same as if we'd
        # just navigated here normally, so just carry on with this same
        # attempt rather than restarting (no time wasted).
        pass

    if debug:
        log_event(f"[debug] Loaded {page.url} (title: {page.title()!r}) before selecting purpose for {building_name}")

    dismiss_cookie_dialog_if_present(page)

    try:
        page.wait_for_selector("#purpose-home", state="visible", timeout=15000)
    except PlaywrightTimeoutError:
        if debug:
            save_debug_snapshot(page, f"purpose-home-not-visible_{building_name}")
        raise RuntimeError(
            "#purpose-home never became visible after loading the search page "
            f"for {building_name}. Check debug_error.png / debug_error.html "
            "(set DEBUG=true in config.txt) to see what the page showed."
        )

    page.select_option("#purpose-home", purpose_value)
    page.wait_for_timeout(1000)

    try:
        page.wait_for_selector("#bname-home:not([disabled])", state="visible", timeout=15000)
        page.select_option("#bname-home", building_code)
    except PlaywrightTimeoutError:
        if debug:
            save_debug_snapshot(page, f"bname-home-not-visible_{building_name}")
        raise RuntimeError(
            f"#bname-home never became visible/ready for {building_name} "
            "after selecting the purpose. It may depend on an AJAX call "
            "triggered by changePurpose() that hadn't finished yet."
        )

    page.wait_for_timeout(500)
    page.click("#btn-go")

    # This is the step that was previously a single hard-coded wait_for_timeout.
    # If the search doesn't actually navigate to a calendar (e.g. a validation
    # error, an alert() dialog, or the click landing on a stale/detached
    # element after a re-render), #week-head will never appear and we want a
    # clear, specific error instead of a bare 30s timeout on inner_text.
    page.wait_for_load_state("networkidle")
    if check_for_maintenance(page, debug=debug):
        # Blocked on the RESULTS page specifically — this is the case where
        # reloading the same URL kept failing but clicking home worked. We
        # just clicked home and are back on index.jsp, so the rest of this
        # attempt (which expects to be on the results page) can't continue.
        # Signal the caller to restart this park's check from the top.
        raise _RestartBuildingCheck()

    try:
        page.wait_for_selector("#week-head", state="visible", timeout=15000)
    except PlaywrightTimeoutError:
        if debug:
            save_debug_snapshot(page, f"week-head-not-visible_{building_name}")
        raise RuntimeError(
            f"#week-head never appeared for {building_name} after clicking "
            "#btn-go. Possible causes: the click triggered a JS alert/confirm "
            "dialog that's blocking navigation, a validation error was shown "
            "instead of the calendar, or the calendar page uses a different "
            "element id than expected. Check debug_error.png / "
            "debug_error.html (set DEBUG=true) to see what actually rendered."
        )

    # Diagnostic only (see dump_calendar_sample_once docstring): does
    # nothing after the first successful dump in a run. This is here to
    # figure out whether the site is only rendering some time-of-day rows
    # (e.g. only 9:00) by the time we get here, before we ever touch the
    # week-by-week loop below.
    if debug:
        dump_calendar_sample_once(page, building_name)

    for _ in range(MAX_WEEKS_TO_CHECK):
        header_text = page.inner_text("#week-head")  # e.g. "2026年7月"
        m = re.search(r"(\d+)年(\d+)月", header_text)
        if not m:
            break
        year, month = int(m.group(1)), int(m.group(2))
        if year != current_year or month != current_month:
            break

        for cell in page.query_selector_all("td[id]"):
            cell_id = cell.get_attribute("id")
            if not cell_id or "_" not in cell_id:
                continue
            date_str, slot_code = cell_id.split("_", 1)
            try:
                slot_date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
            except ValueError:
                continue

            if slot_date <= today:
                continue

            slot_time = SLOT_TIMES.get(slot_code)
            # "Weekend" here also counts Japanese public holidays (法定
            #節日/祝日) via jpholiday — e.g. a Wednesday that's a national
            # holiday is treated the same as a Saturday for the
            # WEEKEND_ALL_DAY exemption below.
            is_weekend_or_holiday = (
                slot_date.weekday() >= 5 or jpholiday.is_holiday(slot_date)
            )
            if time_filter and slot_time not in time_filter:
                if not (weekend_unrestricted and is_weekend_or_holiday):
                    continue

            img = cell.query_selector("img.calendar-status")
            alt = img.get_attribute("alt") if img else ""
            if alt == "空き":
                found.add((building_name, slot_date, slot_time or ""))

        next_week_btn = page.query_selector("#next-week")
        if not next_week_btn:
            break
        next_week_btn.click()
        page.wait_for_timeout(1500)

    return sorted(found)


def check_building(page, building_name, purpose_value, building_code, today, current_year, current_month, time_filter, weekend_unrestricted, debug=False):
    """Returns a list of (building_name, date, time_str) tuples found
    available for one park. Retries from the top (up to
    MAX_RECOVERY_RESTARTS times) both for the known "blocked interstitial"
    case AND for generic failures (e.g. network-level errors like the site
    briefly throttling/rejecting a request after a lot of sustained
    traffic) — a single bad request for one park should not sacrifice the
    whole park's check if trying again would likely succeed."""
    last_error = None
    for restart_num in range(MAX_RECOVERY_RESTARTS + 1):
        try:
            return _check_building_once(
                page, building_name, purpose_value, building_code, today, current_year,
                current_month, time_filter, weekend_unrestricted, debug=debug,
            )
        except _RestartBuildingCheck:
            if debug:
                log_event(
                    f"[debug] Recovered from a blocked page mid-flow for "
                    f"{building_name}, restarting this park's check "
                    f"(attempt {restart_num + 2}/{MAX_RECOVERY_RESTARTS + 1})"
                )
            continue
        except MaintenanceDetected:
            raise  # already retried internally; no point retrying again here
        except Exception as e:
            last_error = e
            if restart_num < MAX_RECOVERY_RESTARTS:
                log_event(
                    f"(attempt {restart_num + 1} failed for {building_name}, "
                    f"pausing then retrying: {e})"
                )
                page.wait_for_timeout(RETRY_PAUSE_MS)
                continue

    raise last_error if last_error is not None else MaintenanceDetected(
        f"Kept hitting a blocked page for {building_name} even after "
        f"{MAX_RECOVERY_RESTARTS} home-button recovery attempts."
    )


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------

def notify_found_slots(
    slots,
    bot_token: str,
    chat_id: str,
    skip_on_rain: bool,
    rain_threshold: int,
) -> bool:
    """
    Send notifications immediately, with ONE Telegram message per park.

    If one park has multiple available slots, all of that park's slots are
    combined into a single message. The caller invokes this function as soon
    as that park's check finishes, so there is no need to wait for the other
    parks.

    Returns:
        True if at least one Telegram message was actually sent. False if
        `slots` was empty, or every slot found was skipped (currently only
        possible reason: SKIP_ON_RAIN). Callers use this to tell "we found
        something and told you" apart from "we found something but it
        wasn't actually worth telling you about" — see how
        stop_on_first_find uses this in check_availability().
    """
    if not slots:
        return False

    notified_anything = False

    # Group slots by park. Normally `slots` comes from one park, but keeping
    # this grouping makes the helper safe if it is called with multiple parks.
    slots_by_building = {}
    for building_name, slot_date, slot_time in slots:
        slots_by_building.setdefault(building_name, []).append(
            (slot_date, slot_time)
        )

    # One Telegram message for EACH park.
    for building_name, building_slots in slots_by_building.items():
        notify_slots = []
        skipped_slots = []

        for slot_date, slot_time in building_slots:
            weekday_kanji = WEEKDAY_KANJI[slot_date.weekday()]
            label = f"{building_name} {slot_date}({weekday_kanji}) {slot_time}".strip()

            if (
                skip_on_rain
                and not is_slot_weather_ok(
                    slot_date,
                    slot_time,
                    rain_threshold,
                )
            ):
                skipped_slots.append(label)
            else:
                notify_slots.append(label)

        if skipped_slots:
            log_event(
                f"Skipped due to rain forecast: {skipped_slots}"
            )

        if not notify_slots:
            log_event(
                f"{building_name}: availability found, but every slot "
                "was skipped due to rain forecast."
            )
            continue

        text = (
            "🎾 空きあり: テニス（人工芝）\n\n"
            f"📍 {building_name}\n\n"
            + "\n".join(f"• {slot}" for slot in notify_slots)
            + f"\n\n{SEARCH_URL}"
        )

        send_telegram(
            bot_token,
            chat_id,
            text,
        )

        log_event(
            f"Availability found at {building_name}. "
            f"Telegram message sent immediately for "
            f"{len(notify_slots)} slot(s): {notify_slots}"
        )
        notified_anything = True

    return notified_anything


# ---------------------------------------------------------------------------
# Scraper / availability check
# ---------------------------------------------------------------------------

def check_availability(
    watch: dict,
    weekend_all_day: set,
    headless: bool = True,
    debug: bool = False,
    on_slots_found=None,
    stop_on_first_find: bool = True,
):
    """
    Check parks one by one.

    As soon as a park finishes checking and has available slots,
    `on_slots_found(found)` is called immediately — a Telegram
    notification does NOT wait for all parks to finish checking, in
    EITHER mode below.

    If stop_on_first_find is True (the config default, STOP_ON_FIRST_FIND):
    as soon as `on_slots_found` reports that it actually sent a
    notification, this run STOPS checking the remaining watched parks
    entirely (they'll simply get checked on the next scheduled run) — the
    whole point is to get you a notification, and a chance to book, as
    fast as possible, rather than spending more time checking parks you
    may not even need anymore. Note this is keyed on an actual
    notification being sent, NOT merely on raw availability being found:
    e.g. SKIP_ON_RAIN can cause `on_slots_found` to find real
    availability and still send nothing, in which case we do NOT stop —
    stopping there would waste the whole run (no message went out, and
    the rest of WATCH never got checked either).

    To keep that early-stop from starving out later-listed parks (if an
    earlier park in WATCH keeps having openings, the ones after it in the
    list would otherwise never get checked), each run RESUMES from
    wherever the previous run left off rather than always starting from
    the top of WATCH — see load_next_park_name()/save_next_park_name().

    If stop_on_first_find is False: every watched park gets checked every
    run, no matter what earlier ones found — you may get several separate
    Telegram messages in one run (one per park with availability), and
    the rotation/resume logic above simply isn't used (there's nothing to
    resume — the whole list gets checked every time).

    Returns:
        All slots found during this run (from every park checked before
        the run stopped, per the mode above).
    """
    global _calendar_dump_done
    _calendar_dump_done = False  # allow one fresh diagnostic dump per run

    today = datetime.date.today()
    current_month = today.month
    current_year = today.year
    all_found = []
    failed_parks = []

    # Rotate the check order so early-stop-on-first-find (below) doesn't
    # always favor whichever park happens to be listed first in WATCH.
    # `names` is the fixed order as configured; `rotated_names` is what we
    # actually iterate this run, starting wherever the last run left off.
    # None of this matters when stop_on_first_find is False — every park
    # gets checked every run regardless of order, so we just use the
    # configured order as-is.
    names = list(watch.keys())
    if stop_on_first_find:
        saved_start_name = load_next_park_name()
        if saved_start_name in names:
            start_index = names.index(saved_start_name)
        else:
            # No saved state yet (first run ever), or the saved park is no
            # longer in WATCH (config changed) — just start from the top.
            start_index = 0
        rotated_names = names[start_index:] + names[:start_index]
    else:
        rotated_names = names

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        attach_ajax_logger(page, debug)

        try:
            for name in rotated_names:
                time_filter = watch[name]
                purpose_value, code = BUILDING_INFO[name]
                weekend_unrestricted = name in weekend_all_day

                try:
                    found = check_building(
                        page,
                        name,
                        purpose_value,
                        code,
                        today,
                        current_year,
                        current_month,
                        time_filter,
                        weekend_unrestricted,
                        debug=debug,
                    )

                    if found:
                        log_event(
                            f"Availability found at {name}: {found}"
                        )

                        all_found.extend(found)

                        # IMPORTANT:
                        # Notify immediately after THIS park is checked.
                        # Do not wait for the remaining parks — this
                        # happens regardless of stop_on_first_find.
                        #
                        # on_slots_found returns whether it actually sent a
                        # Telegram message. "Found" alone isn't enough to
                        # justify stopping early — e.g. SKIP_ON_RAIN can
                        # find real availability and still send nothing.
                        # Stopping (and rotating past this park) in that
                        # case would waste the whole run: no notification
                        # went out, AND the remaining watched parks never
                        # got checked. So we only stop when something was
                        # actually sent.
                        notified = (
                            on_slots_found(found)
                            if on_slots_found is not None
                            else True
                        )

                        if stop_on_first_find and notified:
                            # Stop checking the rest of the watched parks
                            # this run — you already have a slot to go
                            # book, and every extra park checked is extra
                            # time before you can act on it. The remaining
                            # parks just get checked again on the next
                            # scheduled run.
                            #
                            # Resume point for NEXT run: the park right
                            # after this one, in the ORIGINAL (unrotated)
                            # order — not index 0 — so a park that keeps
                            # having openings can't starve out the ones
                            # listed after it forever.
                            next_start = names[(names.index(name) + 1) % len(names)]
                            save_next_park_name(next_start)

                            log_event(
                                f"Stopping this run early after finding "
                                f"availability at {name} — not checking "
                                f"the remaining watched parks. Next run "
                                f"will start from {next_start}."
                            )
                            break
                        elif stop_on_first_find:
                            # Found availability, but on_slots_found sent
                            # no actual message (e.g. every slot was
                            # rain-skipped) — don't treat this as a stop
                            # condition or advance the rotation. Keep
                            # checking the remaining watched parks this
                            # run, same as if nothing had been found here.
                            log_event(
                                f"{name} had availability but nothing was "
                                f"actually notified — continuing to check "
                                f"the remaining watched parks this run."
                            )
                        # else: STOP_ON_FIRST_FIND=false — notification
                        # already sent above, just fall through and keep
                        # checking the remaining parks below.

                except Exception as e:
                    # Isolate the failure to THIS park — log it and move on
                    # to the rest rather than losing the whole run.
                    log_event(
                        f"Skipping {name} this run after repeated failures: {e}"
                    )

                    if debug:
                        save_debug_snapshot(page, f"gave_up_{name}")

                    failed_parks.append(name)
                    continue

                # Small pause between parks to avoid sending a rapid burst of
                # requests that might trigger throttling on the site's side.
                page.wait_for_timeout(PARK_PAUSE_MS)

            else:
                # Loop finished WITHOUT an early break — every watched park
                # got checked this run (found or not). Full cycle done, so
                # next run can just start from the top again. (Only
                # meaningful in stop_on_first_find mode — when it's False
                # every run always checks everyone anyway, so there's no
                # rotation state to maintain.)
                if stop_on_first_find:
                    save_next_park_name(names[0])

        finally:
            browser.close()

    if failed_parks and len(failed_parks) == len(watch):
        # Every single park failed — this looks like a site-wide problem.
        raise MaintenanceDetected(
            f"All {len(watch)} parks failed this run: "
            f"{', '.join(failed_parks)}"
        )

    if failed_parks:
        log_event(
            f"Note: these parks could not be checked this run "
            f"and were skipped: {failed_parks}"
        )

    return all_found


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

MAINTENANCE_RETRY_WAIT_SECONDS = 30  # how long to pause before retrying.
                                     # Shortened for now while diagnosing
                                     # false positives — once confirmed
                                     # real maintenance windows are being
                                     # detected correctly, feel free to
                                     # raise this back up (e.g. to 5*60).
MAINTENANCE_MAX_RETRIES = 1          # retry once, then give up for this run


def gha_flag_incomplete_run(message: str):
    """Marks this run as 'didn't actually complete a real check' WITHOUT
    making the job fail — so it never triggers GitHub's failure-notification
    emails. Instead:
      - Prints a '::warning::' line, which makes GitHub Actions show a
        yellow warning triangle on the run in the Actions list.
      - Appends to the run's step summary (if running in GitHub Actions),
        so the reason is visible right at the top of the run page without
        having to open the raw log.
    Call this any time the script exits early without having actually
    checked the calendar, so you never mistake a blocked/broken run for a
    real 'no availability' result.
    """
    print(f"::warning::{message}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"### ⚠️ {message}\n\n")
        except Exception:
            pass


def main():
    config = load_config()
    rotate_log_if_new_day(config["_log_retention_days"])

    # Callback used by check_availability(). It is called immediately after
    # each individual park has been checked and availability is found.
    # Its return value (whether a Telegram message was actually sent) is
    # what check_availability uses to decide whether to stop early — see
    # the comment above `notified = on_slots_found(found)` there.
    def on_slots_found(slots):
        return notify_found_slots(
            slots=slots,
            bot_token=config["TELEGRAM_BOT_TOKEN"],
            chat_id=config["TELEGRAM_CHAT_ID"],
            skip_on_rain=config["_skip_on_rain"],
            rain_threshold=config["_rain_threshold"],
        )

    attempt = 0

    while True:
        try:
            slots = check_availability(
                config["_watch"],
                config["_weekend_all_day"],
                headless=not config["_headed"],
                debug=config["_debug"],
                on_slots_found=on_slots_found,
                stop_on_first_find=config["_stop_on_first_find"],
            )
            break

        except MaintenanceDetected as e:
            if attempt >= MAINTENANCE_MAX_RETRIES:
                # Give up for this run. Exit code stays 0 (no failure email),
                # but gha_flag_incomplete_run() makes sure this doesn't look
                # like a normal successful "checked, nothing available" run.
                msg = (
                    f"网站疑似维护/拦截，本次未能完成实际检查"
                    f"（重试 {attempt + 1} 次后放弃）："
                    f"{e}"
                )
                log_event(msg)
                gha_flag_incomplete_run(msg)
                return

            log_event(
                f"Site under maintenance: {e}. Waiting "
                f"{MAINTENANCE_RETRY_WAIT_SECONDS // 60} minutes "
                "before retrying..."
            )
            time.sleep(MAINTENANCE_RETRY_WAIT_SECONDS)
            attempt += 1

        except Exception as e:
            msg = f"脚本出错，本次未能完成实际检查：{e}"
            log_event(msg)
            gha_flag_incomplete_run(msg)
            return

    if not slots:
        log_event("No availability this run.")
        return

    # Notifications have already been sent immediately after each park was
    # checked. At this point the full run is simply complete.
    log_event(
        f"Check completed. Total availability found: {len(slots)}"
    )


if __name__ == "__main__":
    main()
