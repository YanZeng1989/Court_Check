#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tokyo Metropolitan Park tennis-court time-slot diagnostic.

Purpose
-------
This script does NOT send Telegram notifications and does NOT book anything.
It visits every tennis facility currently listed in BUILDING_INFO, opens the
weekly calendar, and records the site's actual:

    facility -> purpose -> building code -> slot code -> displayed time

The result is written to:
    facility_time_diagnostic.json
    facility_time_diagnostic.txt

Why this exists
---------------
Different facilities can expose different time rows even when they share the
same broad court type. We therefore discover the mapping from the live page
instead of assuming one global TURF/HARD schedule.

Run:
    pip install playwright
    playwright install chromium
    python discover_facility_times.py
"""

import datetime
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


SEARCH_URL = "https://kouen.sports.metro.tokyo.lg.jp/web/index.jsp"

PURPOSE_ARTIFICIAL_TURF = "1000_1030"  # テニス（人工芝）
PURPOSE_HARD_COURT = "1000_1020"       # テニス（ハード）

# Kept in the same naming/codes as your existing checker.
BUILDING_INFO = {
    # --- テニス（人工芝） ---
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

    # --- テニス（ハード） ---
    "大井ふ頭海浜公園Ａ（ハード）": (PURPOSE_HARD_COURT, "1310"),
    "大井ふ頭海浜公園Ｂ（ハード）": (PURPOSE_HARD_COURT, "1315"),
    "有明テニスＡ屋外ハードコート": (PURPOSE_HARD_COURT, "1350"),
    "有明テニスＢインドアコート": (PURPOSE_HARD_COURT, "1370"),
}

PURPOSE_NAMES = {
    PURPOSE_ARTIFICIAL_TURF: "テニス（人工芝）",
    PURPOSE_HARD_COURT: "テニス（ハード）",
}

OUT_JSON = Path("facility_time_diagnostic.json")
OUT_TXT = Path("facility_time_diagnostic.txt")

COOKIE_SELECTORS = [
    "text=同意する",
    "text=OK",
    "text=閉じる",
    "#agree-btn",
    "#cookie-agree",
    ".modal-close",
]


def dismiss_cookie(page):
    for selector in COOKIE_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=300):
                loc.click(timeout=1500)
                return
        except Exception:
            pass


def normalize_time(text):
    """Convert row labels such as '7時', '07:00', '19 時' to HH:00."""
    if not text:
        return None

    text = text.replace("\n", " ").strip()

    m = re.search(r"(?<!\d)(\d{1,2})\s*時", text)
    if m:
        hour = int(m.group(1))
        if 0 <= hour <= 23:
            return f"{hour:02d}:00"

    m = re.search(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    return None


def extract_slot_mapping(page):
    """
    Discover slot-code -> displayed-time directly from rendered rows.

    Calendar cells have ids like YYYYMMDD_10. For each such cell we inspect
    its containing <tr> and nearby row header/first cells to obtain the
    displayed Japanese time label.
    """
    raw = page.evaluate(
        r"""() => {
            const out = [];
            for (const cell of document.querySelectorAll('td[id]')) {
                const id = cell.id || '';
                const m = id.match(/^(\d{8})_(.+)$/);
                if (!m) continue;

                const tr = cell.closest('tr');
                if (!tr) continue;

                const candidates = [];

                // Row headers are the strongest signal.
                for (const el of tr.querySelectorAll('th')) {
                    candidates.push(el.innerText || el.textContent || '');
                }

                // On this site the time label may be in the first table cell.
                const first = tr.querySelector('td');
                if (first) {
                    candidates.push(first.innerText || first.textContent || '');
                }

                // Keep the full row text as a final fallback.
                candidates.push(tr.innerText || tr.textContent || '');

                out.push({
                    cell_id: id,
                    date: m[1],
                    slot_code: m[2],
                    candidates: candidates
                });
            }
            return out;
        }"""
    )

    mapping = {}
    evidence = {}

    for item in raw:
        code = item["slot_code"]
        detected = None
        used_text = None

        for candidate in item.get("candidates", []):
            t = normalize_time(candidate)
            if t:
                detected = t
                used_text = candidate.strip()
                break

        evidence.setdefault(code, [])
        if used_text and used_text not in evidence[code]:
            evidence[code].append(used_text)

        if detected:
            if code in mapping and mapping[code] != detected:
                raise RuntimeError(
                    f"slot code {code!r} mapped to conflicting displayed times: "
                    f"{mapping[code]} vs {detected}"
                )
            mapping[code] = detected

    return mapping, evidence, raw


def open_facility(page, name, purpose_value, building_code):
    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
    dismiss_cookie(page)

    page.wait_for_selector("#purpose-home", state="visible", timeout=15000)
    page.select_option("#purpose-home", purpose_value)

    page.wait_for_selector(
        "#bname-home:not([disabled])",
        state="visible",
        timeout=25000,
    )
    page.select_option("#bname-home", building_code)

    try:
        page.wait_for_selector("#loadmsg", state="hidden", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    page.click("#btn-go")
    page.wait_for_load_state("domcontentloaded")

    page.wait_for_selector("#week-head", state="visible", timeout=25000)
    page.wait_for_selector("td[id]", state="attached", timeout=15000)

    # Give the calendar's own JS a very short settling window. This script
    # is diagnostic, so reliability matters more than shaving a second.
    page.wait_for_timeout(500)


def scan_facility(page, name, purpose_value, building_code):
    print(f"\n=== {name} ===", flush=True)
    open_facility(page, name, purpose_value, building_code)

    mapping, evidence, raw = extract_slot_mapping(page)

    # If the first rendered week did not expose enough rows, move forward once
    # and merge what is visible there as well.
    try:
        next_btn = page.locator("#next-week")
        if next_btn.count() and next_btn.first.is_visible():
            old_head = page.locator("#week-head").inner_text()
            next_btn.first.click()
            try:
                page.wait_for_function(
                    """old => {
                        const h = document.querySelector('#week-head');
                        return h && h.innerText !== old;
                    }""",
                    arg=old_head,
                    timeout=8000,
                )
            except PlaywrightTimeoutError:
                page.wait_for_timeout(800)

            mapping2, evidence2, raw2 = extract_slot_mapping(page)
            for code, displayed_time in mapping2.items():
                if code in mapping and mapping[code] != displayed_time:
                    raise RuntimeError(
                        f"slot code {code!r} changed mapping between weeks: "
                        f"{mapping[code]} vs {displayed_time}"
                    )
                mapping[code] = displayed_time
            for code, values in evidence2.items():
                evidence.setdefault(code, [])
                for value in values:
                    if value not in evidence[code]:
                        evidence[code].append(value)
            raw.extend(raw2)
    except Exception as exc:
        print(f"  [note] next-week extra sample skipped: {exc}", flush=True)

    mapping = dict(
        sorted(
            mapping.items(),
            key=lambda kv: (int(kv[0]) if kv[0].isdigit() else 9999, kv[0]),
        )
    )

    if mapping:
        for code, displayed_time in mapping.items():
            print(f"  {code} -> {displayed_time}", flush=True)
    else:
        print("  WARNING: no slot-code/time mapping could be derived.", flush=True)

    return {
        "purpose_value": purpose_value,
        "purpose_name": PURPOSE_NAMES.get(purpose_value, purpose_value),
        "building_code": building_code,
        "slot_times": mapping,
        "evidence": evidence,
        "calendar_cell_count_sampled": len(raw),
    }


def write_outputs(results):
    payload = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(),
        "source": SEARCH_URL,
        "facilities": results,
    }

    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = []
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"Source: {SEARCH_URL}")
    lines.append("")

    for name, info in results.items():
        lines.append(f"=== {name} ===")
        lines.append(
            f"purpose={info.get('purpose_name')} "
            f"purpose_value={info.get('purpose_value')} "
            f"building_code={info.get('building_code')}"
        )

        if info.get("error"):
            lines.append(f"ERROR: {info['error']}")
        elif info.get("slot_times"):
            for code, displayed_time in info["slot_times"].items():
                lines.append(f"{code} -> {displayed_time}")
        else:
            lines.append("NO MAPPING DETECTED")

        lines.append("")

    OUT_TXT.write_text("\n".join(lines), encoding="utf-8")


def main():
    results = {}

    print(
        f"Scanning {len(BUILDING_INFO)} supported tennis facilities...",
        flush=True,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        for index, (name, (purpose_value, building_code)) in enumerate(
            BUILDING_INFO.items(), start=1
        ):
            print(
                f"[{index}/{len(BUILDING_INFO)}] {name}",
                flush=True,
            )

            try:
                results[name] = scan_facility(
                    page,
                    name,
                    purpose_value,
                    building_code,
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                print(f"  ERROR: {message}", flush=True)
                results[name] = {
                    "purpose_value": purpose_value,
                    "purpose_name": PURPOSE_NAMES.get(
                        purpose_value, purpose_value
                    ),
                    "building_code": building_code,
                    "slot_times": {},
                    "error": message,
                }

            # Write after every facility, so a partial result survives if
            # GitHub Actions is interrupted later.
            write_outputs(results)

            # Be polite to the reservation site. This is a one-off diagnostic,
            # not the high-frequency availability monitor.
            time.sleep(1)

        context.close()
        browser.close()

    write_outputs(results)

    ok = sum(1 for x in results.values() if x.get("slot_times"))
    failed = len(results) - ok

    print("\n========================================", flush=True)
    print(f"Finished. Mapping detected for {ok} facilities.", flush=True)
    print(f"Facilities needing review: {failed}", flush=True)
    print(f"JSON: {OUT_JSON.resolve()}", flush=True)
    print(f"TXT : {OUT_TXT.resolve()}", flush=True)
    print("========================================", flush=True)

    # Do not fail the whole Action just because one facility needs manual
    # review; the output files are still valuable.
    return 0


if __name__ == "__main__":
    sys.exit(main())
