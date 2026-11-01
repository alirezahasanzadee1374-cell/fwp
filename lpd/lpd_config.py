"""
lpd_config.py — تنظیمات LPD
"""
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MASTER_KEY = os.environ.get("MASTER_KEY", "")

# دو کلید جدا برای جلوگیری از بلاک شدن API
BYKARANTELI_KEY_LPD = os.environ.get("BYKARANTELI_KEY_LPD", "") # برای Whale Tape
BYKARANTELI_KEY_FWP = os.environ.get("BYKARANTELI_KEY_FWP", "") # برای LiqMap

BYBIT_PROXY_URL = "https://bybit-proxy.alirezahasanzadee1374.workers.dev"

MAX_FOCUS_LIST_SIZE = 20
SWING_LOOKBACK_4H = 60

# شروط پیدا کردن (آسان)
MIN_PROFIT_PCT = 0.07

# شروط ورود (سخت‌گیرانه)
FIB_DISCOUNT_LEVEL = 0.5
MIN_BID_WALL_USD = 100_000
MIN_POWER_RATIO = 1.5
MAX_RETAIL_ASK_USD = 50_000
ALERT_DISTANCE_PCT = 0.01

# شروط خروج
EARLY_EXIT_PCT = 0.03
HARD_STOP_PCT = 0.07
POSITION_CHECK_INTERVAL = 180  # 3 دقیقه

LPD_STATE_FILE = "lpd_state.json"
