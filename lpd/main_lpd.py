"""
main_lpd.py — اجراکننده اصلی LPD
"""
import os, sys, time, json, base64, hashlib, logging, importlib, tempfile
from typing import Optional
import requests

from lpd.lpd_config import *
from lpd.lpd_data import get_klines_binance, get_orderbook_okx_futures, get_whale_tape, get_liqmap

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("LPD.Main")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path: sys.path.insert(0, ROOT_DIR)

def load_lpd_strategy():
    from cryptography.fernet import Fernet
    enc_path = os.path.join(ROOT_DIR, "lpd", "lpd_strategy.enc")
    if not os.path.exists(enc_path): sys.exit(1)
    if not MASTER_KEY: sys.exit(1)
    digest = hashlib.sha256(MASTER_KEY.encode("utf-8")).digest()
    fernet = Fernet(base64.urlsafe_b64encode(digest))
    try: plaintext = fernet.decrypt(open(enc_path, "rb").read())
    except Exception as e: sys.exit(1)
    tmp_path = os.path.join(tempfile.gettempdir(), "lpd_strategy.py")
    with open(tmp_path, "wb") as f: f.write(plaintext)
    sys.path.insert(0, tempfile.gettempdir())
    sys.modules.pop("lpd_strategy", None)
    logger.info("✅ strategy decrypted")
    return importlib.import_module("lpd_strategy")

def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        logger.info("📨 Telegram sent")
    except Exception as e: logger.error(f"❌ Telegram failed | {e}")

def load_state() -> dict:
    if os.path.exists(LPD_STATE_FILE):
        try: return json.load(open(LPD_STATE_FILE, "r", encoding="utf-8"))
        except: pass
    return {"focus_list": {}, "scan_index": 0, "active_position": None, "last_pos_check": 0}

def save_state(state: dict):
    try:
        with open(LPD_STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e: logger.error(f"❌ Save failed | {e}")

def run_full_scan(state, strategy):
    if len(state["focus_list"]) >= MAX_FOCUS_LIST_SIZE: return
    if state.get("active_position"): return

    try:
        r = requests.get("https://api.toobit.com/api/v1/exchangeInfo", timeout=15).json()
        wl = {s["symbol"] for s in r.get("symbols", []) if s.get("status") == "TRADING" and s["symbol"].endswith("USDT") and not s["symbol"].endswith("ONUSDT")}
        t = requests.get("https://data-api.binance.vision/api/v3/ticker/24hr", timeout=15).json()
        vol = {x["symbol"]: float(x.get("quoteVolume", 0)) for x in t if x.get("symbol") in wl}
        symbols = [k for k, v in sorted(vol.items(), key=lambda i: i[1], reverse=True)[:500]]
    except: return

    idx = state.get("scan_index", 0)
    if idx >= len(symbols): idx = 0
    batch = symbols[idx:idx+100]
    
    logger.info(f"🔍 Scan batch {idx}-{idx+len(batch)} | focus={len(state['focus_list'])}")
    new_setups = 0

    for sym in batch:
        if sym in state["focus_list"]: continue
        df = get_klines_binance(sym, "4h")
        if df is None: continue
        
        setup = strategy.screen_symbol(sym, df, float(df["close"].iloc[-1]))
        if setup:
            setup["added_at"] = time.time()
            state["focus_list"][sym] = setup
            new_setups += 1
            send_telegram(f"➕ <b>به لیست اضافه شد</b>\nارز: {sym}\nهدف: {setup['target_price']}\n📋 لیست: {len(state['focus_list'])}/20")
            if len(state["focus_list"]) >= MAX_FOCUS_LIST_SIZE:
                send_telegram(f"🎯 <b>لیست تمرکز پر شد!</b>")
                break
        time.sleep(0.1)

    state["scan_index"] = idx + 100
    save_state(state)
    logger.info(f"✅ Scan done | +{new_setups} | total {len(state['focus_list'])}")

def run_active_scan(state, strategy):
    pos = state.get("active_position")
    
    if pos:
        if time.time() - state.get("last_pos_check", 0) < POSITION_CHECK_INTERVAL:
            logger.info(f"⏸ Pos throttled")
            return
        logger.info(f"🎯 MONITOR | {pos['symbol']}")
        df = get_klines_binance(pos['symbol'], "4h", 5)
        if df is None: return
        price = float(df["close"].iloc[-1])
        pnl = (price - pos["entry_price"]) / pos["entry_price"] * 100
        
        if price >= pos["target_price"]:
            send_telegram(f"🎯 <b>TARGET HIT</b>\n{pos['symbol']} | سود: {round(pnl,2)}%")
            del state["focus_list"][pos['symbol']]; state["active_position"] = None
        elif pnl <= -7:
            send_telegram(f"🚨 <b>HARD STOP</b>\n{pos['symbol']} | ضرر: {round(pnl,2)}%")
            del state["focus_list"][pos['symbol']]; state["active_position"] = None
        elif pnl <= -3:
            ob = get_orderbook_okx_futures(pos['symbol'])
            if ob and sum(p*q for p,q in ob["asks"]) > sum(p*q for p,q in ob["bids"]) * 1.2:
                send_telegram(f"🚨 <b>Emergency Exit</b>\n{pos['symbol']} | فشار فروش")
                del state["focus_list"][pos['symbol']]; state["active_position"] = None
        else:
            liq = get_liqmap(pos['symbol'])
            if liq and liq.get("fuel_exhausted"):
                send_telegram(f"⛽ <b>Fuel Exhausted</b>\n{pos['symbol']} | سود: {round(pnl,2)}%")
                del state["focus_list"][pos['symbol']]; state["active_position"] = None

        if state.get("active_position"): state["last_pos_check"] = time.time()
        save_state(state)
        return

    if not state["focus_list"]: return
    
    sorted_focus = sorted(state["focus_list"].items(), key=lambda x: x[1].get("added_at", 0), reverse=True)
    for sym, setup in sorted_focus:
        df = get_klines_binance(sym, "4h", 100)
        if df is None: continue
        price = float(df["close"].iloc[-1])
        
        if not strategy.detect_nouveau_trend(df):
            logger.info(f"➖ {sym} | Trend down")
            del state["focus_list"][sym]
            send_telegram(f"➖ <b>حذف از لیست</b>\n{sym} | روند نزولی شد")
            continue

        ob_range = strategy.find_order_block(df)
        if ob_range and price > ob_range[0]:
            dist = (price - ob_range[0]) / ob_range[0]
            if dist <= ALERT_DISTANCE_PCT and setup.get("status") != "ready":
                send_telegram(f"⏳ <b>LPD Ready</b>\n{sym} | ۱٪ تا ورود فاصله دارد")
                setup["status"] = "ready"

        ob = get_orderbook_okx_futures(sym)
        if not ob: continue
        
        whale = get_whale_tape(sym)
        if strategy.check_entry_conditions(sym, df, ob, price, setup, whale_tape=whale):
            logger.info(f"🚀🚀 ENTRY | {sym}")
            send_telegram(f"🚀🔥 <b>LPD ENTRY</b>\n{sym}\nورود: {price}\nهدف: {setup['target_price']}\n📈 سود: {setup['profit_pct']}%")
            state["active_position"] = {"symbol": sym, "entry_price": price, "target_price": setup["target_price"]}
            state["last_pos_check"] = time.time()
            save_state(state)
            return

    save_state(state)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    logger.info(f"▶️ LPD {mode.upper()}")
    strategy = load_lpd_strategy()
    state = load_state()
    if mode == "active": run_active_scan(state, strategy)
    else: run_full_scan(state, strategy)

if __name__ == "__main__":
    main()
