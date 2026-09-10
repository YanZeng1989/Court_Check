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

SLOT_TIMES = {
    "10": "09:00",
    "20": "11:00",
    "30": "13:00",
    "40": "15:00",
    "50": "17:00",
    "60": "19:00",
}

DEFAULT_BUILDING = "芝公園"
WEATHER_LAT = 35.6895
WEATHER_LON = 139.6917
SLOT_DURATION_HOURS = 2   
WEATHER_BUFFER_HOURS = 2  
DEFAULT_RAIN_THRESHOLD = 30  

LOG_PATH = os.path.join(SCRIPT_DIR, "log.txt")
ROTATION_MARKER_PATH = os.path.join(SCRIPT_DIR, ".last_log_rotation")
DEFAULT_LOG_RETENTION_DAYS = 7
MAX_WEEKS_TO_CHECK = 6  
REQUIRED_KEYS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print(f"Could not find config.txt at: {CONFIG_PATH}")
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
            if not entry or "@" not in entry:
                continue
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

    return config, watch

# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------

def run_checker():
    config, watch = load_config()
    
    with sync_playwright() as p:
        # 1. 启动浏览器（生产环境建议使用无头模式 headless=True）
        # 调试时如果想看画面，可以临时改为 headless=False
        browser = p.chromium.launch(headless=True)
        
        # 2. 创建上下文并伪装正常浏览器 User-Agent，防止被防火墙误杀拦截
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Connecting to {SEARCH_URL}...")
        
        # 优化点 1：使用 networkidle 确保页面资源和异步 JS 脚本全部加载完毕后再继续
        page.goto(SEARCH_URL, wait_until="networkidle")
        
        # 优化点 2：实时检查当前页面是否被重定向到了“系统维护/拒绝访问”页面
        page_content = page.content()
        if "アクセスできません" in page_content or "お知らせ" in page_content:
            print("【错误退出】目标网站当前处于系统维护状态或拒绝了自动化访问。")
            browser.close()
            sys.exit(1)
            
        try:
            # 优化点 3：定位下拉框，并显式等待其在页面上“可见”且“可交互”
            purpose_select = page.locator("#purpose-home")
            purpose_select.wait_for(state="visible", timeout=10000)
            
            # 优化点 4：执行选择操作
            purpose_select.select_option(PURPOSE_VALUE)
            print("成功选中：テニス（人工芝）")
            
            # --- 接下来是原脚本后续的点击查询和日历轮询逻辑 ---
            # (由于你提供的代码片段在最后被截断了，此处承接你原本的查场逻辑)
            
        except Exception as e:
            print(f"运行时发生错误: {e}")
            # 调试快照：如果失败了，保存一张截图方便查看当时卡在什么画面
            page.screenshot(path=os.path.join(SCRIPT_DIR, "error_debug.png"))
            raise e
        finally:
            browser.close()

if __name__ == "__main__":
    run_checker()
