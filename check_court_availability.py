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
        Comma-separated list of park names where, on Saturdays and
        Sundays, the normal time filter (from WATCH or TIMES) is
        ignored and ANY available time counts. Weekdays for that park
        still follow the normal filter. Leave unset if you don't need
        this.

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

You do NOT need to put your login ID/password anywhere — checking
availability doesn't require logging in. You'd only log in yourself,
manually, at the very end to actually make the booking.

Run once manually first, with headless=False (see near the bottom), to
confirm each click/selector still matches the live site before you
schedule it unattended.
"""

import os
import re
import sys
import json
import datetime
import urllib.request
import urllib.parse

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEARCH_URL = "https://kouen.sports.metro.tokyo.lg.jp/web/index.jsp"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.txt")

PURPOSE_VALUE = "1000_1030"  # テニス（人工芝）

# Park name -> building code. Add more here if the site adds more parks.
BUILDING_CODES = {
    "日比谷公園": "1000",
    "芝公園": "1010",
    "猿江恩賜公園": "1040",
    "亀戸中央公園": "1050",
    "木場公園": "1060",
    "祖師谷公園": "1070",
    "東白鬚公園": "1090",
    "浮間公園": "1100",
    "城北中央公園": "1110",
    "赤塚公園": "1120",
    "東綾瀬公園": "1130",
    "舎人公園": "1140",
    "篠崎公園Ａ": "1150",
    "大島小松川公園": "1160",
    "汐入公園": "1170",
    "高井戸公園": "1175",
    "善福寺川緑地": "1180",
    "光が丘公園": "1190",
    "石神井公園Ｂ": "1205",
    "井の頭恩賜公園": "1220",
    "武蔵野中央公園": "1230",
    "小金井公園": "1240",
    "野川公園": "1260",
    "府中の森公園": "1270",
    "東大和南公園": "1280",
    "大井ふ頭海浜公園Ｂ": "1315",
    "有明テニスＣ人工芝コート": "1360",
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

MAX_WEEKS_TO_CHECK = 6  # safety cap; loop also stops once it leaves the current month

REQUIRED_KEYS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


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

    # Which parks to check, and which times matter for each one.
    # Result is stored as config["_watch"], a dict: {park_name: set_of_times}
    # An empty set of times means "any time" for that park.
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
        # Fall back to the simpler BUILDINGS + TIMES (same filter for all parks)
        buildings_raw = config.get("BUILDINGS", DEFAULT_BUILDING)
        building_names = [b.strip() for b in buildings_raw.split(",") if b.strip()]
        times_raw = config.get("TIMES", "")
        time_filter = {t.strip() for t in times_raw.split(",") if t.strip()}
        for name in building_names:
            watch[name] = set(time_filter)

    unknown_parks = [name for name in watch if name not in BUILDING_CODES]
    if unknown_parks:
        print(f"Unknown park name(s): {', '.join(unknown_parks)}")
        print(f"Supported names: {', '.join(BUILDING_CODES.keys())}")
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

    # Optional: parks where weekends ignore the time filter entirely
    weekend_raw = config.get("WEEKEND_ALL_DAY", "").strip()
    weekend_all_day = {name.strip() for name in weekend_raw.split(",") if name.strip()}
    unknown_weekend = [name for name in weekend_all_day if name not in BUILDING_CODES]
    if unknown_weekend:
        print(f"Unknown park name(s) in WEEKEND_ALL_DAY: {', '.join(unknown_weekend)}")
        print(f"Supported names: {', '.join(BUILDING_CODES.keys())}")
        sys.exit(1)
    config["_weekend_all_day"] = weekend_all_day

    # Optional log retention setting
    log_retention_raw = config.get("LOG_RETENTION_DAYS", "").strip()
    if log_retention_raw:
        try:
            config["_log_retention_days"] = int(log_retention_raw)
        except ValueError:
            print(f"LOG_RETENTION_DAYS should be a whole number, got: {log_retention_raw}")
            sys.exit(1)
    else:
        config["_log_retention_days"] = DEFAULT_LOG_RETENTION_DAYS

    # Optional rain check (defaults to on)
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

    return config


# ---------------------------------------------------------------------------
# Logging (with automatic cleanup of old entries)
# ---------------------------------------------------------------------------

def log_event(message: str):
    """Prints a timestamped line and appends it to log.txt."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"(could not write to log.txt: {e})")


def _already_rotated_today() -> bool:
    """Checks a small marker file to see if we've already rotated the log today."""
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
    """Only actually cleans log.txt once per calendar day (the first run after
    midnight), not on every single check — keeps things efficient even when
    running every few minutes."""
    if _already_rotated_today():
        return
    rotate_log(retention_days)
    _mark_rotated_today()


def rotate_log(retention_days: int):
    """Deletes log.txt entries older than retention_days."""
    if not os.path.exists(LOG_PATH):
        return

    cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)

    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return  # if we can't read it, just leave it alone

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


# ---------------------------------------------------------------------------
# Weather check
# ---------------------------------------------------------------------------

def _get_hourly_rain_probabilities(date_obj: datetime.date, lat: float = WEATHER_LAT, lon: float = WEATHER_LON):
    """Returns {hour_0_to_23: precipitation_probability_percent} for the
    given date, or None if the forecast couldn't be fetched (e.g. the date
    is too far in the future for Open-Meteo's free forecast range)."""
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
            # t looks like "2026-08-05T17:00"
            hour = int(t.split("T")[1].split(":")[0])
            result[hour] = p
        return result or None
    except Exception as e:
        log_event(f"(weather forecast fetch failed for {date_str}: {e})")
        return None


def is_slot_weather_ok(slot_date: datetime.date, slot_time: str, threshold: int) -> bool:
    """Checks the rain forecast across [slot_start - buffer, slot_end + buffer].
    Returns True if it's safe to notify (low rain chance, OR forecast simply
    isn't available — we fail open rather than hide a real opening), False
    if the max rain probability in that window meets/exceeds the threshold."""
    if not slot_time or ":" not in slot_time:
        return True  # no time info to check against; fail open

    try:
        start_hour = int(slot_time.split(":")[0])
    except ValueError:
        return True

    hourly_probs = _get_hourly_rain_probabilities(slot_date)
    if hourly_probs is None:
        return True  # couldn't get a forecast — fail open

    window_start = start_hour - WEATHER_BUFFER_HOURS
    window_end = start_hour + SLOT_DURATION_HOURS + WEATHER_BUFFER_HOURS  # exclusive
    relevant_probs = [
        hourly_probs[h] for h in range(window_start, window_end)
        if h in hourly_probs
    ]
    if not relevant_probs:
        return True  # nothing in range to judge by — fail open

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
# Scraper
# ---------------------------------------------------------------------------

def check_building(page, building_name, building_code, today, current_year, current_month, time_filter, weekend_unrestricted):
    """Returns a list of (building_name, date, time_str) tuples found available for one park."""
    found = set()

    page.goto(SEARCH_URL)
    page.select_option("#purpose-home", PURPOSE_VALUE)
    page.wait_for_timeout(1000)
    page.select_option("#bname-home", building_code)
    page.wait_for_timeout(500)
    page.click("#btn-go")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    for _ in range(MAX_WEEKS_TO_CHECK):
        header_text = page.inner_text("#week-head")  # e.g. "2026年7月"
        m = re.search(r"(\d+)年(\d+)月", header_text)
        if not m:
            break
        year, month = int(m.group(1)), int(m.group(2))
        if year != current_year or month != current_month:
            break  # left the current month; stop here

        for cell in page.query_selector_all("td[onclick*='setReserv']"):
            cell_id = cell.get_attribute("id")  # e.g. "20260731_30"
            if not cell_id or "_" not in cell_id:
                continue
            date_str, slot_code = cell_id.split("_", 1)
            try:
                slot_date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
            except ValueError:
                continue

            if slot_date <= today:
                continue  # never today or the past

            slot_time = SLOT_TIMES.get(slot_code)
            is_weekend = slot_date.weekday() >= 5  # Saturday=5, Sunday=6
            if time_filter and slot_time not in time_filter:
                if not (weekend_unrestricted and is_weekend):
                    continue  # not a time the user asked about (and no weekend override)

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


def check_availability(watch: dict, weekend_all_day: set, headless: bool = True):
    today = datetime.date.today()
    current_month = today.month
    current_year = today.year
    all_found = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        for name, time_filter in watch.items():
            code = BUILDING_CODES[name]
            weekend_unrestricted = name in weekend_all_day
            all_found.extend(
                check_building(page, name, code, today, current_year, current_month, time_filter, weekend_unrestricted)
            )

        browser.close()

    return all_found


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    rotate_log_if_new_day(config["_log_retention_days"])

    try:
        slots = check_availability(config["_watch"], config["_weekend_all_day"], headless=True)
    except Exception as e:
        log_event(f"Error while checking availability: {e}")
        return

    if not slots:
        log_event("No availability this run.")
        return

    notify_slots = []
    skipped_slots = []
    for building_name, slot_date, slot_time in slots:
        label = f"{building_name} {slot_date} {slot_time}".strip()
        if config["_skip_on_rain"] and not is_slot_weather_ok(slot_date, slot_time, config["_rain_threshold"]):
            skipped_slots.append(label)
        else:
            notify_slots.append(label)

    if skipped_slots:
        log_event(f"Skipped due to rain forecast: {skipped_slots}")

    if notify_slots:
        text = (
            "空きあり: テニス（人工芝）\n\n"
            + "\n".join(notify_slots)
            + f"\n\n{SEARCH_URL}"
        )
        send_telegram(config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHAT_ID"], text)
        log_event(f"Availability found, Telegram message sent for: {notify_slots}")
    else:
        log_event("Availability found, but every slot was skipped due to rain forecast.")


if __name__ == "__main__":
    main()
