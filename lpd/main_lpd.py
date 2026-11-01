"""
main_lpd.py — اجراکننده اصلی LPD با منطق چرخشی + اعلان لیست
"""

import os
import sys
import time
import json
import base64
import hashlib
import logging
import importlib
import tempfile
from typing import Optional

import requests
import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from lpd.lpd_config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MASTER_KEY,
    BYBIT_PROXY_URL, MAX_FOCUS_LIST_SIZE, ALERT_DISTANCE_PCT,
    LPD_STATE_FILE, SWING_LOOKBACK_4H,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("LPD.Main")

# ============================================================
# رمزگشایی استراتژی
# ============================================================
def load_lpd_strategy():
    from cryptography.fernet import Fernet
    enc_path = os.path.join(ROOT_DIR, "lpd", "lpd_strategy.enc")
    if not os.path.exists(enc_path):
        sys.exit(1)
    digest = hashlib.sha256(MASTER_KEY.encode("utf-8")).digest()
    fernet = Fernet(base64.urlsafe_b64encode(digest))
    plaintext = fernet.decrypt(open(enc_path, "rb").read())
    tmp_path = os.path.join(tempfile.gettempdir(), "lpd_strategy.py")
    with open(tmp_path, "wb") as f:
        f.write(plaintext)
    sys.path.insert(0, tempfile.gettempdir())
    sys.modules.pop("lpd_strategy", None)
    return importlib.import_module("lpd_strategy")

# ============================================================
# استیت
# ============================================================
def load_state() -> dict:
    if os.path.exists(LPD_STATE_FILE):
        try:
            return json.load(open(LPD_STATE_FILE, "r", encoding="utf-8"))
        except: pass
    return {"focus_list": {}, "scan_index": 0, "active_position": None}

def save_state(state: dict):
    try:
        with open(LPD_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logger.info(f"state saved | focus={len(state.get('focus_list', {}))} | active={bool(state.get('active_position'))} | scan_idx={state.get('scan_index', 0)}")
    except Exception as e:
        logger.error(f"save failed | {e}")

# ============================================================
# تلگرام
# ============================================================
def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        return True
    except: return False

# ============================================================
# لیست توبیت + تاپ ۵۰۰
# ============================================================
def load_toobit_whitelist() -> set:
    try:
        data = requests.get("https://api.toobit.com/api/v1/exchangeInfo", timeout=15).json()
        return {s["symbol"] for s in data.get("symbols", [])
                if s.get("status") == "TRADING"
                and s["symbol"].endswith("USDT")
                and not s["symbol"].endswith("ONUSDT")
                and not s["symbol"].endswith("XUSDT")
                and not s["symbol"].endswith("USDC")}
    except: return set()

def get_top_symbols(whitelist: set, limit: int = 500) -> list:
    try:
        tickers = requests.get("https://data-api.binance.vision/api/v3/ticker/24hr", timeout=15).json()
        volumes = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers if t.get("symbol") in whitelist}
        sorted_syms = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
        return [s for s, _ in sorted_syms[:limit]]
    except: return []

# ============================================================
# دریافت کندل و اردربوک
# ============================================================
def fetch_klines(symbol, interval, limit=300):
    try:
        data = requests.get("https://data-api.binance.vision/api/v3/klines",
                            params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10).json()
        if not data: return None
        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore"])[["time", "open", "high", "low", "close", "volume"]]
        for c in ["open", "high", "low", "close", "volume"]: df[c] = df[c].astype(float)
        return df
    except: return None

def fetch_order_book(symbol):
    try:
        base = symbol[:-4]
        js = requests.get("https://www.okx.com/api/v5/market/books", params={"instId": f"{base}-USDT", "sz": 100}, timeout=5).json()
        if js.get("data"):
            res = js["data"][0]
            return {"bids": [[float(p), float(q)] for p, q, *_ in res["bids"]], "asks": [[float(p), float(q)] for p, q, *_ in res["asks"]]}
    except: pass
    try:
        js = requests.get(f"{BYBIT_PROXY_URL}/v5/market/orderbook", params={"category": "linear", "symbol": symbol, "limit": 100}, timeout=5).json()
        if js.get("retCode") == 0 and js.get("result"):
            res = js["result"]
            return {"bids": [[float(p), float(q)] for p, q in res["b"]], "asks": [[float(p), float(q)] for p, q in res["a"]]}
    except: pass
    try:
        js = requests.get("https://api.mexc.com/api/v3/depth", params={"symbol": symbol, "limit": 100}, timeout=5).json()
        if js.get("bids"):
            return {"bids": [[float(p), float(q)] for p, q in js["bids"]], "asks": [[float(p), float(q)] for p, q in js["asks"]]}
    except: pass
    return None

# ============================================================
# اسکن کامل (چرخشی)
# ============================================================
def run_full_scan(state: dict, lpd_strategy):
    focus_list = state.get("focus_list", {})
    
    if len(focus_list) >= MAX_FOCUS_LIST_SIZE:
        logger.info(f"⏸ Focus list full ({len(focus_list)}/{MAX_FOCUS_LIST_SIZE}) — skipping scan")
        return
    
    whitelist = load_toobit_whitelist()
    all_symbols = get_top_symbols(whitelist, 500)
    if not all_symbols:
        logger.error("no symbols available")
        return
    
    scan_idx = state.get("scan_index", 0)
    if scan_idx >= len(all_symbols):
        scan_idx = 0
        logger.info("♻️ All symbols scanned - cycling back")
    
    batch = all_symbols[scan_idx:scan_idx+100]
    logger.info(f"🔍 Scanning batch {scan_idx}-{scan_idx+len(batch)} of {len(all_symbols)} | focus={len(focus_list)}")
    
    new_setups = 0
    for i, symbol in enumerate(batch, 1):
        if symbol in focus_list:
            continue
        
        try:
            df_4h = fetch_klines(symbol, "4h")
            df_1d = fetch_klines(symbol, "1d")
            if df_4h is None or df_1d is None: continue
            
            current_price = float(df_4h["close"].iloc[-1])
            setup = lpd_strategy.evaluate_lpd_setup(df_4h, df_1d, fetch_order_book(symbol), current_price)
            
            if setup:
                setup["symbol"] = symbol
                setup["status"] = "waiting"
                setup["current_price"] = current_price
                focus_list[symbol] = setup
                new_setups += 1
                logger.info(f"⭐ SETUP | {symbol} | profit={setup['profit_pct']}% | dist={setup['distance_to_entry']}%")
                
                # 📢 اعلان تلگرام: اضافه شد به لیست
                send_telegram(
                    f"➕ <b>به لیست بررسی اضافه شد</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"ارز: <b>{symbol}</b>\n"
                    f"💰 قیمت فعلی: {current_price}\n"
                    f"🎯 قیمت ورود (OB): {setup['entry_price']}\n"
                    f"📊 فاصله تا ورود: {setup['distance_to_entry']}%\n"
                    f"📈 هدف: {setup['target_price']} ({setup['profit_pct']}% سود)\n"
                    f"🐋 قدرت خرید: {setup['bid_power']}x\n"
                    f"⏱ تایم: {setup['trend']}\n"
                    f"📋 لیست: {len(focus_list)}/{MAX_FOCUS_LIST_SIZE}"
                )
                
                if len(focus_list) >= MAX_FOCUS_LIST_SIZE:
                    logger.info(f"🎯 Focus list reached {MAX_FOCUS_LIST_SIZE} — stopping")
                    send_telegram(f"🎯 <b>لیست تمرکز پر شد!</b>\n{MAX_FOCUS_LIST_SIZE} ارز آماده پایش.\nاسکن جدید متوقف شد.")
                    break
        except Exception as e:
            logger.error(f"scan {symbol} failed | {e}")
        
        if i % 10 == 0:
            logger.info(f"  progress | {i}/{len(batch)} | total_focus={len(focus_list)} | new={new_setups}")
        
        time.sleep(0.1)
    
    state["focus_list"] = focus_list
    state["scan_index"] = scan_idx + 100
    save_state(state)
    logger.info(f"✅ SCAN finished | +{new_setups} new | total focus={len(focus_list)}/{MAX_FOCUS_LIST_SIZE}")

# ============================================================
# اسکن سریع (Active)
# ============================================================
def run_active_scan(state: dict):
    focus_list = state.get("focus_list", {})
    active_pos = state.get("active_position")
    
    if active_pos:
        _monitor_active_position(state)
        return
    
    if not focus_list:
        logger.info("focus list empty")
        return
    
    logger.info(f"👁 Active scan | {len(focus_list)} symbols")
    
    for symbol, setup in list(focus_list.items()):
        try:
            df = fetch_klines(symbol, "4h", 5)
            if df is None: continue
            current_price = float(df["close"].iloc[-1])
            entry_price = setup["entry_price"]
            dist = abs(current_price - entry_price) / entry_price
            
            # هشدار ۱٪
            if dist <= ALERT_DISTANCE_PCT and setup["status"] == "waiting":
                send_telegram(
                    f"⏳ <b>LPD Ready</b>\n"
                    f"ارز: {symbol}\n"
                    f"قیمت: {current_price}\n"
                    f"ورود: {entry_price}\n"
                    f"فاصله: {round(dist*100,2)}%\n"
                    f"🎯 هدف: {setup['target_price']} ({setup['profit_pct']}%)"
                )
                setup["status"] = "ready"
                logger.info(f"⏳ READY | {symbol}")
            
            # سیگنال ورود
            elif current_price <= entry_price and setup["status"] == "ready":
                send_telegram(
                    f"🚀🔥 <b>LPD ENTRY</b>\n"
                    f"ارز: {symbol}\n"
                    f"ورود: {current_price}\n"
                    f"🎯 هدف: {setup['target_price']}\n"
                    f"📈 سود: {setup['profit_pct']}%"
                )
                state["active_position"] = {
                    "symbol": symbol,
                    "entry_price": current_price,
                    "target_price": setup["target_price"],
                    "profit_target_pct": setup["profit_pct"],
                    "entry_time": time.time(),
                }
                logger.info(f"🚀 ENTRY | {symbol}")
                save_state(state)
                return
            
            # ریست (خیلی دور شد)
            if dist > 0.05:
                del focus_list[symbol]
                logger.info(f"♻️ removed | {symbol} | too far")
                
                # 📢 اعلان تلگرام: حذف شد از لیست
                send_telegram(
                    f"➖ <b>از لیست بررسی حذف شد</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"ارز: <b>{symbol}</b>\n"
                    f"دلیل: قیمت خیلی از اوردر بلاک دور شد ({round(dist*100, 2)}%)\n"
                    f"📋 لیست: {len(focus_list)}/{MAX_FOCUS_LIST_SIZE}"
                )
        except Exception as e:
            logger.error(f"active {symbol} failed | {e}")
    
    state["focus_list"] = focus_list
    save_state(state)

def _monitor_active_position(state: dict):
    pos = state["active_position"]
    symbol = pos["symbol"]
    
    try:
        df = fetch_klines(symbol, "4h", 5)
        if df is None: return
        current_price = float(df["close"].iloc[-1])
        
        entry = pos["entry_price"]
        target = pos["target_price"]
        profit_pct = (current_price - entry) / entry * 100
        
        logger.info(f"📊 MONITOR | {symbol} | price={current_price} | profit={round(profit_pct,2)}%")
        
        if current_price >= target:
            send_telegram(f"🎯 <b>LPD TARGET HIT</b>\nارز: {symbol}\nسود: {round(profit_pct,2)}%\n✅ خارج شو!")
            _reset_active(state)
            return
        
        if profit_pct <= -7:
            send_telegram(f"🚨 <b>LPD HARD STOP</b>\nارز: {symbol}\nضرر: {round(profit_pct,2)}%\n🚪 خروج فوری!")
            _reset_active(state)
            return
        
        if profit_pct <= -3:
            send_telegram(f"⚠️ <b>LPD Warning -3%</b>\nارز: {symbol}\nضرر: {round(profit_pct,2)}%\n🧐 مراقب باش")
    except Exception as e:
        logger.error(f"monitor failed | {e}")

def _reset_active(state: dict):
    pos = state.get("active_position")
    if pos:
        symbol = pos["symbol"]
        focus_list = state.get("focus_list", {})
        if symbol in focus_list:
            del focus_list[symbol]
        state["focus_list"] = focus_list
        state["active_position"] = None
        logger.info(f"♻️ POSITION CLOSED | {symbol} | focus_list={len(focus_list)}")
        
        # 📢 اعلان تلگرام: ریست پوزیشن
        send_telegram(
            f"🔄 <b>پوزیشن بسته شد</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"ارز: <b>{symbol}</b>\n"
            f"لیست ریست شد. اسکن ادامه می‌یابد.\n"
            f"📋 لیست: {len(focus_list)}/{MAX_FOCUS_LIST_SIZE}"
        )
        save_state(state)

# ============================================================
# تابع اصلی
# ============================================================
def main():
    lpd_strategy = load_lpd_strategy()
    state = load_state()
    
    if len(sys.argv) > 1 and sys.argv[1] == "active":
        run_active_scan(state)
    else:
        run_full_scan(state, lpd_strategy)

if __name__ == "__main__":
    main()
