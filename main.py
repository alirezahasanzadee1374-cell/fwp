"""
main.py — قلب سیستم FWP (Follow Whale Protocol)
"""

import os
import sys
import time
import base64
import hashlib
import logging
import importlib
import json
import tempfile
from typing import Optional

import requests

import platinum_manager

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    MASTER_KEY,
    MEME_COINS_WHITELIST,
    REPEAT_WHALE_PRICE_TOLERANCE,
    EARLY_CONFIRMATION_GAP_PCT,
    URGENT_SELL_OI_DROP_PCT,
    URGENT_SELL_POWER_SHIFT_RATIO,
    SIGNAL_PRIORITY,
    LISTING_HUNT_ENABLED,
    LISTING_HUNT_MIN_WHALE_USD,
    MIN_WHALE_WALL_USD,
    STEP1_USD,
    STEP2_USD,
    MAX_LOSS_USD,
    KLINE_LIMIT,
    LOG_LEVEL,
    LOSS_7PCT_NEW_LIMIT_MIN_USD,
    LOSS_7PCT_DEAD_MARKET_VOLUME_DROP,
    MINOR_CORRECTION_MAX_SELLER_RATIO,
    SMART_TARGET_MIN_WALL_PCT,
    ALERT_PRIORITY,
    STABLECOIN_BLACKLIST,
    STOCK_TOKEN_BLACKLIST,  # ✅ جدید
)

# ---------- Logging ----------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("FWP.Main")

# ============================================================
# ۱. رمزگشایی strategy.enc
# ============================================================
def load_strategy():
    from cryptography.fernet import Fernet
    if not os.path.exists("strategy.enc"):
        logger.error("strategy.enc not found")
        sys.exit(1)
    if not MASTER_KEY:
        logger.error("MASTER_KEY is empty")
        sys.exit(1)
    digest = hashlib.sha256(MASTER_KEY.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    fernet = Fernet(key)
    with open("strategy.enc", "rb") as f:
        ciphertext = f.read()
    try:
        plaintext = fernet.decrypt(ciphertext)
    except Exception as e:
        logger.error(f"decryption failed: {e}")
        sys.exit(1)
    tmp_dir = tempfile.gettempdir()
    tmp_path = os.path.join(tmp_dir, "strategy.py")
    with open(tmp_path, "wb") as f:
        f.write(plaintext)
    if tmp_dir not in sys.path:
        sys.path.insert(0, tmp_dir)
    sys.modules.pop("strategy", None)
    strategy_module = importlib.import_module("strategy")
    logger.info(f"strategy.enc decrypted and loaded from {tmp_path}")
    return strategy_module

# ============================================================
# ۲. ارسال تلگرام
# ============================================================
def send_telegram(message: str, priority: str = "profit_update") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("telegram credentials missing")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"telegram failed | {e}")
        return False

# ============================================================
# ۳. ذخیره‌سازی state (دو فایل جدا)
# ============================================================
saved_whale_orders: dict = {}
active_positions: dict = {}
_listed_symbols: set = set()
_saved_oi: dict = {}

STATE_FILE = "state.json"  # فقط برای مهاجرت یک‌باره

def save_state() -> None:
    """ذخیره state به دو فایل جدا (جلوگیری از conflict بین active و full)."""
    try:
        # فایل ۱: پوزیشن‌ها + نمادها (نویسنده: fwp_active)
        pos_state = {
            "active_positions": active_positions,
            "listed_symbols": list(_listed_symbols),
            "last_updated": time.time(),
        }
        with open("state_positions.json", "w", encoding="utf-8") as f:
            json.dump(pos_state, f, ensure_ascii=False, indent=2)

        # فایل ۲: سفارش‌های نهنگ + OI (نویسنده: fwp_full)
        whale_orders_serializable = {
            f"{k[0]}|{k[1]}|{k[2]}": v
            for k, v in saved_whale_orders.items()
        }
        whale_state = {
            "saved_whale_orders": whale_orders_serializable,
            "saved_oi": _saved_oi,
            "last_updated": time.time(),
        }
        with open("state_whales.json", "w", encoding="utf-8") as f:
            json.dump(whale_state, f, ensure_ascii=False, indent=2)

        logger.info(f"state saved | {len(active_positions)} positions | {len(saved_whale_orders)} whale orders")
    except Exception as e:
        logger.error(f"save_state failed | {e}")

def load_state() -> None:
    """بارگذاری state از دو فایل جدا. مهاجرت یک‌باره از state.json قدیمی."""

    # لود پوزیشن‌ها (نویسنده: fwp_active)
    if os.path.exists("state_positions.json"):
        try:
            with open("state_positions.json", "r", encoding="utf-8") as f:
                pos_state = json.load(f)
            active_positions.clear()
            active_positions.update(pos_state.get("active_positions", {}))
            _listed_symbols.clear()
            _listed_symbols.update(pos_state.get("listed_symbols", []))
        except Exception as e:
            logger.error(f"load state_positions.json failed | {e}")

    # لود سفارش‌های نهنگ (نویسنده: fwp_full)
    if os.path.exists("state_whales.json"):
        try:
            with open("state_whales.json", "r", encoding="utf-8") as f:
                whale_state = json.load(f)
            saved_whale_orders.clear()
            for key_str, val in whale_state.get("saved_whale_orders", {}).items():
                parts = key_str.split("|")
                if len(parts) == 3:
                    saved_whale_orders[(parts[0], parts[1], float(parts[2]))] = val
            _saved_oi.clear()
            _saved_oi.update(whale_state.get("saved_oi", {}))
        except Exception as e:
            logger.error(f"load state_whales.json failed | {e}")

    # مهاجرت یک‌باره از state.json قدیمی (اگه فایل‌های جدید خالی بودن)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            if not active_positions and "active_positions" in old:
                active_positions.update(old.get("active_positions", {}))
            if not saved_whale_orders and "saved_whale_orders" in old:
                for key_str, val in old.get("saved_whale_orders", {}).items():
                    parts = key_str.split("|")
                    if len(parts) == 3:
                        saved_whale_orders[(parts[0], parts[1], float(parts[2]))] = val
            if not _saved_oi and "saved_oi" in old:
                _saved_oi.update(old.get("saved_oi", {}))
            if not _listed_symbols and "listed_symbols" in old:
                _listed_symbols.update(old.get("listed_symbols", []))
            logger.info("old state.json imported for one-time migration")
        except Exception as e:
            logger.warning(f"migration from state.json failed | {e}")

    logger.info(f"state loaded | {len(active_positions)} positions | {len(saved_whale_orders)} whale orders")

# ============================================================
# ۴. ذخیره سفارش‌های نهنگ
# ============================================================
def _order_key(symbol: str, side: str, price: float) -> tuple:
    return (symbol, side, round(price, 8))

def update_saved_whale_orders(symbol: str, whale_walls: dict, current_ts: float) -> None:
    cutoff = current_ts - 1800
    keys_to_delete = [k for k, v in saved_whale_orders.items() if v["timestamp"] < cutoff]
    for k in keys_to_delete:
        del saved_whale_orders[k]
    for wall in whale_walls.get("bids", []):
        key = _order_key(symbol, "bid", wall["price"])
        if key not in saved_whale_orders:
            saved_whale_orders[key] = {"usd": wall["usd"], "timestamp": current_ts}
    for wall in whale_walls.get("asks", []):
        key = _order_key(symbol, "ask", wall["price"])
        if key not in saved_whale_orders:
            saved_whale_orders[key] = {"usd": wall["usd"], "timestamp": current_ts}

def detect_whale_cancel(symbol: str, whale_walls: dict) -> list:
    cancelled = []
    if not whale_walls:
        return cancelled
    current_bids = {_order_key(symbol, "bid", w["price"]) for w in whale_walls.get("bids", [])}
    current_asks = {_order_key(symbol, "ask", w["price"]) for w in whale_walls.get("asks", [])}
    current_all = current_bids | current_asks
    for key, old_data in saved_whale_orders.items():
        if key[0] != symbol:
            continue
        if key not in current_all:
            cancelled.append({"side": key[1], "price": key[2], "old_usd": old_data["usd"]})
    return cancelled

# ============================================================
# ۴.۵. تشخیص سفارش در حال پر شدن (قانون ۸)
# ============================================================
def detect_whale_fill(symbol: str, whale_walls: dict, min_fill_pct: float = 0.10) -> list:
    if not whale_walls:
        return []
    filling = []
    current_bids = {_order_key(symbol, "bid", w["price"]): w["usd"] for w in whale_walls.get("bids", [])}
    current_asks = {_order_key(symbol, "ask", w["price"]): w["usd"] for w in whale_walls.get("asks", [])}
    for key, old_data in saved_whale_orders.items():
        if key[0] != symbol:
            continue
        current_usd = current_bids.get(key) if key[1] == "bid" else current_asks.get(key)
        if current_usd is not None and current_usd < old_data["usd"]:
            fill_pct = (old_data["usd"] - current_usd) / old_data["usd"]
            if fill_pct >= min_fill_pct:
                filling.append({
                    "side": key[1],
                    "price": key[2],
                    "old_usd": old_data["usd"],
                    "current_usd": current_usd,
                    "fill_pct": fill_pct,
                })
    return filling

# ============================================================
# ۴.۶. تشخیص نهنگ تکراری (قانون ۱۰)
# ============================================================
def detect_repeat_whale(symbol: str, whale_walls: dict) -> int:
    if not whale_walls:
        return 0
    matches = 0
    for wall in whale_walls.get("bids", []):
        key = _order_key(symbol, "bid", wall["price"])
        if key in saved_whale_orders:
            old_price = key[2]
            if abs(wall["price"] - old_price) / old_price <= REPEAT_WHALE_PRICE_TOLERANCE:
                matches += 1
    return matches

# ============================================================
# ۵. تاییدیه زودهنگام (قانون ۲۹)
# ============================================================
def detect_early_confirmation(symbol: str, current_price: float, whale_walls: dict) -> Optional[dict]:
    if symbol not in active_positions:
        return None
    pos = active_positions[symbol]
    step1_price = pos["step1_price"]
    current_drop = (step1_price - current_price) / step1_price
    if current_drop < EARLY_CONFIRMATION_GAP_PCT:
        return None
    step2_price = step1_price * 0.925
    if current_price <= step2_price:
        return None
    if whale_walls and whale_walls.get("bid_count", 0) > 0:
        return {
            "symbol": symbol,
            "current_price": current_price,
            "current_drop": round(current_drop, 4),
            "whale_count": whale_walls["bid_count"],
        }
    return None

# ============================================================
# ۶. رد پای فروش فوری (قانون ۴۳)
# ============================================================
def detect_urgent_sell(symbol: str, order_book: dict, current_oi: Optional[float], power: dict, data_fetcher_module=None) -> Optional[dict]:
    triggers = []
    if current_oi is not None and symbol in _saved_oi:
        old_oi = _saved_oi[symbol]
        if old_oi > 0:
            oi_drop = (old_oi - current_oi) / old_oi
            if oi_drop >= URGENT_SELL_OI_DROP_PCT:
                triggers.append(f"OI drop {oi_drop*100:.1f}%")
    if power and power.get("winner") == "sellers" and power.get("ratio", 0) > 0:
        if (1 / power["ratio"]) >= URGENT_SELL_POWER_SHIFT_RATIO:
            triggers.append(f"power shift (ratio={power['ratio']:.2f})")
    if order_book:
        for p, q in order_book.get("asks", []):
            if p * q >= MIN_WHALE_WALL_USD:
                key = _order_key(symbol, "ask", p)
                if key not in saved_whale_orders:
                    triggers.append(f"new ask wall ${p*q:.0f}")

    # قانون ۴۳: Whale Tape — فشار فروش نهنگ‌ها
    if data_fetcher_module:
        try:
            wf = data_fetcher_module.get_whale_flow(symbol, hours=1)
            if wf and wf.get("pressure") == "sell" and wf.get("sell_usd", 0) > 500_000:
                triggers.append(f"whale selling ${wf['sell_usd']:.0f}")
        except Exception as e:
            logger.warning(f"whale tape check failed | {symbol} | {e}")

    if triggers:
        return {"symbol": symbol, "triggers": triggers}
    return None

# ============================================================
# ۷. شکار لیستینگ (قانون ۴۸)
# ============================================================
def detect_listing(symbol: str, whale_walls: dict) -> Optional[dict]:
    if not LISTING_HUNT_ENABLED:
        return None
    if symbol in _listed_symbols:
        return None
    if whale_walls and whale_walls.get("bid_count", 0) > 0:
        total_bid = sum(w["usd"] for w in whale_walls["bids"])
        if total_bid >= LISTING_HUNT_MIN_WHALE_USD:
            _listed_symbols.add(symbol)
            return {"symbol": symbol, "total_bid_usd": total_bid, "whale_count": whale_walls["bid_count"]}
    return None

# ============================================================
# ۸. فیلتر میم‌کوین (قانون ۱۵)
# ============================================================
def is_meme_coin(symbol: str) -> bool:
    base = symbol.replace("USDT", "").upper()
    return base in [m.upper() for m in MEME_COINS_WHITELIST]

# ============================================================
# ۸.۵. فیلتر استیبل‌کوین (قانون ۴۵ — نویز صفر)
# ============================================================
def is_stablecoin(symbol: str) -> bool:
    """چک می‌کنه آیا ارز استیبل‌کوین یا pegged هست (نباید سیگنال بده)"""
    base = symbol.replace("USDT", "").upper()
    return base in STABLECOIN_BLACKLIST

# ✅ جدید: فیلتر توکن‌های سهام و RWA
def is_stock_token(symbol: str) -> bool:
    """چک می‌کنه آیا ارز توکن سهام یا RWA هست (نباید سیگنال بده)"""
    base = symbol.replace("USDT", "").upper()
    return base in STOCK_TOKEN_BLACKLIST

# ============================================================
# ۹. تحلیل یک ارز
# ============================================================
def analyze_symbol(symbol, strategy_module, all_news, btc_df, data_fetcher_module):
    # فیلتر استیبل‌کوین‌ها (FDUSD، USDC، RLUSD، ...)
    if is_stablecoin(symbol):
        return None
    # ✅ فیلتر توکن‌های سهام (NVDAB، MSTRB، INTCB، ...)
    if is_stock_token(symbol):
        return None

    is_meme = is_meme_coin(symbol)
    df_4h = data_fetcher_module.get_klines(symbol, "4h", KLINE_LIMIT)
    df_1d = data_fetcher_module.get_klines(symbol, "1d", KLINE_LIMIT)
    if df_4h is None or len(df_4h) < 50:
        return None
    current_price = float(df_4h["close"].iloc[-1])
    if is_meme:
        if df_1d is None or len(df_1d) < 50:
            return None
        historical_low = df_1d["low"].min()
        if current_price > historical_low * 1.05:
            return None

    static_levels_4h = strategy_module.find_static_levels(df_4h, current_price)
    static_levels_1d = strategy_module.find_static_levels(df_1d, current_price) if df_1d is not None else []
    all_zones = [(z, "4h") for z in static_levels_4h] + [(z, "1d") for z in static_levels_1d]
    if not all_zones:
        return None
    all_zones.sort(key=lambda x: x[0]["distance_pct"])
    closest_zone, closest_tf = all_zones[0]
    if closest_zone["distance_pct"] > 0.05:
        return None

    order_book = data_fetcher_module.get_order_book(symbol, 100)
    if order_book is None:
        return None
    current_oi = data_fetcher_module.get_open_interest(symbol)
    market_cap = data_fetcher_module.get_market_cap(symbol)
    if current_oi is not None:
        _saved_oi[symbol] = current_oi

    bunker = strategy_module.find_whale_bunker(closest_zone, order_book)
    power = strategy_module.calculate_power(order_book, current_price)
    whale_walls = strategy_module.detect_whale_walls(order_book)

    repeat_whale_count = detect_repeat_whale(symbol, whale_walls)
    if repeat_whale_count > 0:
        logger.info(f"repeat whale | {symbol} | matches={repeat_whale_count}")

    balance_shift = strategy_module.detect_balance_shift(power, bunker)
    if balance_shift:
        logger.info(f"balance shift confirmed | {symbol}")

    filling_orders = detect_whale_fill(symbol, whale_walls)
    if filling_orders and symbol in active_positions:
        for f in filling_orders:
            side_fa = "خرید" if f["side"] == "bid" else "فروش"
            logger.info(f"whale fill | {symbol} | {side_fa} | {f['fill_pct']*100:.0f}%")
            send_telegram(
                f"🟢 <b>سفارش نهنگ داره پر می‌شه</b>\n"
                f"ارز: {symbol}\n"
                f"سمت: {side_fa}\n"
                f"قیمت: {f['price']}\n"
                f"حجم قبلی: ${f['old_usd']:.0f}\n"
                f"حجم فعلی: ${f['current_usd']:.0f}\n"
                f"پر شده: {f['fill_pct']*100:.0f}%",
                priority="minor_correction"
            )

    if market_cap and market_cap.get("volume_24h", 0) > 0:
        volume_24h = market_cap["volume_24h"]
    else:
        try:
            vol_url = f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={symbol}"
            vr = requests.get(vol_url, timeout=5)
            if vr.status_code == 200:
                vol_data = vr.json()
                volume_24h = float(vol_data.get("quoteVolume", 0))
            else:
                volume_24h = 0
        except Exception:
            volume_24h = 0

    whale_power_walls = strategy_module.detect_whale_power(whale_walls, market_cap, volume_24h)
    ob_all = data_fetcher_module.get_order_book_all_exchanges(symbol)
    overlap_list = strategy_module.detect_overlap(ob_all) if ob_all else []

    clear_path = strategy_module.detect_clear_path(order_book, current_price)
    wall_power = strategy_module.detect_wall_power(whale_walls)
    whale_intent = strategy_module.check_whale_intent(bunker, power, overlap_list, clear_path)
    decoupling = strategy_module.check_decoupling(btc_df, df_4h)

    news_positive = strategy_module.check_news_positive(all_news, symbol)
    news_negative = strategy_module.check_news_negative(all_news, symbol)

    if wall_power["dominant"] == "ask" and wall_power["bid_power"] >= 100_000:
        send_telegram(
            f"⚠️ <b>نهنگ فروشنده ظاهر شد</b>\n"
            f"ارز: {symbol}\n"
            f"قدرت خرید: ${wall_power['bid_power']:.0f}\n"
            f"قدرت فروش: ${wall_power['ask_power']:.0f}\n"
            f"اقدام: وارد نشوید",
            priority="minor_correction"
        )
        logger.info(f"ask dominant | {symbol} | bid={wall_power['bid_power']:.0f} ask={wall_power['ask_power']:.0f}")
        return None

    signal = strategy_module.generate_signal(
        symbol=symbol,
        timeframe=closest_tf,
        static_level=closest_zone,
        whale_walls=whale_walls,
        whale_power_walls=whale_power_walls,
        overlap_list=overlap_list,
        power=power,
        decoupling=decoupling,
        news_negative=news_negative,
        news_positive=news_positive,
        market_cap_info=market_cap,
        bunker=bunker,
        clear_path=clear_path,
        wall_power=wall_power,
        whale_intent=whale_intent,
        balance_shift=balance_shift,
    )

    if whale_walls and whale_walls.get("bid_count", 0) > 0:
        cancelled = detect_whale_cancel(symbol, whale_walls)
        if cancelled and signal:
            logger.warning(f"signal rejected | {symbol} | whale cancelled")
            return None
        update_saved_whale_orders(symbol, whale_walls, time.time())

    # قانون ۱۶: تایید Whale Tape برای پلاتینیوم (آلت‌کوین‌ها)
    if signal and signal.get("type") == "platinum":
        wf = data_fetcher_module.get_whale_flow(symbol, hours=1)
        if wf and wf.get("pressure") == "sell" and wf.get("sell_usd", 0) > 100_000:
            logger.warning(f"platinum rejected | {symbol} | whale selling ${wf['sell_usd']:.0f}")
            return None
        logger.info(f"platinum whale check OK | {symbol} | pressure={wf.get('pressure') if wf else 'no_data'}")

    return signal

# ============================================================
# ۱۰. پردازش سیگنال (با وضعیت آنچین بیت‌کوین)
# ============================================================
def process_signal(signal: dict, current_price: float) -> None:
    symbol = signal["symbol"]
    entry_price = signal["entry_price"]
    signal_type = signal["type"]

    qty_step1 = round(STEP1_USD / entry_price, 8) if entry_price > 0 else 0
    active_positions[symbol] = {
        "step1_price": entry_price,
        "step1_usd": STEP1_USD,
        "step1_qty": qty_step1,
        "entries": [{"price": entry_price, "usd": STEP1_USD, "qty": qty_step1}],
        "signal_type": signal_type,
        "timeframe": signal["timeframe"],
        "opened_at": time.time(),
    }

    emoji = {"diamond": "💎", "platinum": "🏆", "golden": "🥇", "silver": "🥈"}.get(signal_type, "📊")

    # وضعیت آنچین بیت‌کوین
    btc_sentiment = platinum_manager.get_btc_onchain_sentiment()

    send_telegram(
        f"{emoji} <b>سیگنال {signal_type.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 مدل: {signal_type}\n"
        f"⏱ تایم‌فریم: {signal['timeframe']}\n"
        f"💰 قیمت ورود: {entry_price}\n"
        f"\n"
        f"🕵️ <b>تحلیل نهنگ:</b>\n"
        f"• تعداد: {signal['bid_count']}\n"
        f"• قدرت خرید: {signal['power_ratio']:.2f}x\n"
        f"• هم‌پوشانی: {signal['overlap_count']}\n"
        f"• دلیل: {signal['reason']}\n"
        f"\n"
        f"📈 <b>مدیریت سرمایه:</b>\n"
        f"• پله اول: ${STEP1_USD:.0f}\n"
        f"• سقف ضرر: ${MAX_LOSS_USD:.0f}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📡 <b>وضعیت آنچین بیت‌کوین:</b>\n"
        f"• {btc_sentiment}",
        priority="profit_update"
    )

# ============================================================
# ۱۱. مدیریت پوزیشن‌های فعال
# ============================================================
def manage_active_positions(strategy_module, data_fetcher_module, capital_manager_module, all_news):
    if not active_positions:
        return
    for symbol in list(active_positions.keys()):
        pos = active_positions[symbol]
        df = data_fetcher_module.get_klines(symbol, "4h", 5)
        if df is None or len(df) == 0:
            continue
        current_price = float(df["close"].iloc[-1])
        entries = pos["entries"]
        avg_price = capital_manager_module.calculate_avg_price(entries)

        alerts_buffer = []
        should_delete = False
        skip_lower = False

        if capital_manager_module.check_max_loss(entries, current_price):
            alerts_buffer.append(("threat_alert", f"🚨 <b>سقف ضرر!</b>\nارز: {symbol}\nاقدام: خارج شوید!"))
            should_delete = True
            skip_lower = True

        if not skip_lower:
            target = capital_manager_module.check_profit_targets(avg_price, current_price)
            if target:
                if target["is_final"]:
                    alerts_buffer.append(("profit_update", f"🎯 <b>هدف ۵۰٪!</b>\nارز: {symbol}\nسود: {target['profit_pct']*100:.1f}%\nاقدام: خروج کامل!"))
                    should_delete = True
                    skip_lower = True
                else:
                    alerts_buffer.append(("profit_update", f"📈 <b>سود {target['target']*100:.0f}%</b>\nارز: {symbol}\nسود فعلی: {target['profit_pct']*100:.1f}%"))

        order_book = None
        power = {"winner": "none", "ratio": 0}
        current_oi = None

        if not skip_lower:
            order_book = data_fetcher_module.get_order_book(symbol, 100)
            current_oi = data_fetcher_module.get_open_interest(symbol)
            power = strategy_module.calculate_power(order_book, current_price) if order_book else {"winner": "none", "ratio": 0}

            urgent = detect_urgent_sell(symbol, order_book, current_oi, power, data_fetcher_module)
            if urgent:
                alerts_buffer.append(("urgent_sell", f"🚨 <b>رد پای فروش!</b>\nارز: {symbol}\nنشانه‌ها: {', '.join(urgent['triggers'])}\nاقدام: خروج!"))
                skip_lower = True

        if not skip_lower:
            klines_5m = data_fetcher_module.get_klines(symbol, "5m", 30)
            if klines_5m is not None:
                fuel = strategy_module.check_short_fuel(klines_5m)
                if fuel.get("fuel_exhausted"):
                    logger.info(f"fuel exhausted | {symbol}")
                    alerts_buffer.append(("urgent_sell", f"⛽ <b>سوخت شورت تموم شد</b>\nارز: {symbol}\nاقدام: با سود فعلی خارج شوید."))
                    skip_lower = True

        # قانون ۲۲: ByKaranteli — لیکویید واقعی شورت‌ها
        if not skip_lower:
            try:
                liq_state = data_fetcher_module.get_liquidation_data(symbol)
                if liq_state and liq_state.get("fuel_exhausted"):
                    logger.info(f"fuel exhausted (bykaranteli) | {symbol} | {liq_state.get('reason')}")
                    alerts_buffer.append(("urgent_sell", f"⛽ <b>سوخت شورت تموم شد</b>\nارز: {symbol}\nدلیل: {liq_state.get('reason')}"))
                    skip_lower = True
            except Exception as e:
                logger.warning(f"bykaranteli liq check failed | {symbol} | {e}")

        if not skip_lower and order_book:
            clear_path_check = strategy_module.detect_clear_path(order_book, current_price)
            if not clear_path_check.get("clear"):
                logger.info(f"clear path blocked | {symbol}")
                alerts_buffer.append(("minor_correction", f"🚧 <b>جاده ۱۰٪ بسته شد</b>\nارز: {symbol}\nاقدام: بررسی خروج"))

        if not skip_lower:
            current_loss_pct = (avg_price - current_price) / avg_price if avg_price > 0 else 0
            if current_loss_pct >= 0.07:
                whale_walls = strategy_module.detect_whale_walls(order_book) if order_book else {"bid_count": 0, "bids": []}
                if whale_walls.get("bids") and whale_walls["bids"][0]["usd"] >= LOSS_7PCT_NEW_LIMIT_MIN_USD:
                    logger.info(f"  case1: new limit | {symbol}")
                    alerts_buffer.append(("minor_correction", f"🟡 <b>۷٪ ضرر + لیمیت جدید</b>\nارز: {symbol}\nنهنگ: ${whale_walls['bids'][0]['usd']:.0f}\nاقدام: پله دوم رو بررسی کن."))
                    skip_lower = True
                elif df is not None and len(df) >= 20:
                    avg_volume = df["volume"].iloc[-20:].mean()
                    recent_volume = df["volume"].iloc[-1]
                    if recent_volume < avg_volume * LOSS_7PCT_DEAD_MARKET_VOLUME_DROP:
                        logger.info(f"  case3: dead market | {symbol}")
                        alerts_buffer.append(("threat_alert", f"🚨 <b>بی‌رمقی بازار!</b>\nارز: {symbol}\nحجم: {recent_volume/avg_volume*100:.0f}% میانگین\nاقدام: خروج فوری!"))
                        should_delete = True
                        skip_lower = True
                if not skip_lower and power["winner"] == "sellers":
                    logger.info(f"  case4: sell pressure | {symbol}")
                    alerts_buffer.append(("threat_alert", f"🚨 <b>فشار فروش!</b>\nارز: {symbol}\nزور سلرها: {1/power['ratio']:.2f}x\nاقدام: خروج فوری!"))
                    should_delete = True
                    skip_lower = True

        if not skip_lower:
            if power["winner"] == "sellers" and power.get("ratio", 0) > 0:
                seller_ratio = 1 / power["ratio"]
                if seller_ratio < MINOR_CORRECTION_MAX_SELLER_RATIO:
                    logger.info(f"minor correction | {symbol} | seller_ratio={seller_ratio:.2f}")
                    alerts_buffer.append(("minor_correction", f"🌊 <b>اصلاح کوچک</b>\nارز: {symbol}\nزور سلرها: {seller_ratio:.2f}x\nپوزیشن حفظ شود."))

            if order_book and order_book.get("asks"):
                for ask_p, ask_q in order_book["asks"]:
                    ask_pct = (ask_p - current_price) / current_price
                    if 0.05 <= ask_pct <= 0.11 and ask_p * ask_q >= MIN_WHALE_WALL_USD:
                        logger.info(f"smart target | {symbol} | ask={ask_pct*100:.1f}%")
                        alerts_buffer.append(("minor_correction", f"🎯 <b>دیوار فروش!</b>\nارز: {symbol}\nدیوار: ${ask_p*ask_q:.0f} در {ask_pct*100:.1f}%\nتارگت رو {ask_pct*100:.0f}% تنظیم کن."))
                        break

            whale_walls_early = strategy_module.detect_whale_walls(order_book) if order_book else {"bid_count": 0}
            early = detect_early_confirmation(symbol, current_price, whale_walls_early)
            if early:
                alerts_buffer.append(("minor_correction", f"🔄 <b>تاییدیه زودهنگام</b>\nارز: {symbol}\nریزش: {early['current_drop']*100:.1f}%\nنهنگ جدید: {early['whale_count']}"))

        if alerts_buffer:
            try:
                best = min(alerts_buffer, key=lambda x: ALERT_PRIORITY.index(x[0]) if x[0] in ALERT_PRIORITY else 999)
            except (ValueError, IndexError):
                best = alerts_buffer[0]
            send_telegram(best[1], priority=best[0])
            if len(alerts_buffer) > 1:
                logger.info(f"alerts suppressed | {symbol} | buffered={len(alerts_buffer)} | sent={best[0]}")

        if should_delete:
            del active_positions[symbol]

# ============================================================
# ۱۲. مدیریت پله دوم خودکار
# ============================================================
def manage_step2_entries(strategy_module, data_fetcher_module, capital_manager_module):
    if not active_positions:
        return
    for symbol in list(active_positions.keys()):
        pos = active_positions[symbol]
        if len(pos["entries"]) >= 2:
            continue
        df = data_fetcher_module.get_klines(symbol, "4h", 5)
        if df is None or len(df) == 0:
            continue
        current_price = float(df["close"].iloc[-1])
        order_book = data_fetcher_module.get_order_book(symbol, 100)
        if order_book is None:
            continue
        whale_walls = strategy_module.detect_whale_walls(order_book)
        whale_confirmed = whale_walls.get("bid_count", 0) > 0
        should_enter = capital_manager_module.should_enter_step2(
            step1_price=pos["step1_price"],
            current_price=current_price,
            whale_confirmed=whale_confirmed,
            already_in_step2=False,
        )
        if not should_enter:
            continue
        qty_step2 = round(STEP2_USD / current_price, 8) if current_price > 0 else 0
        if qty_step2 <= 0:
            continue
        pos["entries"].append({"price": current_price, "usd": STEP2_USD, "qty": qty_step2})
        new_avg = capital_manager_module.calculate_avg_price(pos["entries"])
        total_qty = sum(e["qty"] for e in pos["entries"])
        stop_price = new_avg - (MAX_LOSS_USD / total_qty) if total_qty > 0 else 0
        send_telegram(
            f"🟡 <b>پله دوم!</b>\nارز: {symbol}\n"
            f"قیمت: {current_price:.8f}\n"
            f"میانگین: {new_avg:.8f}\n"
            f"نهنگ: {whale_walls['bid_count']}\n"
            f"---\nسفارش Sell Limit جدید: {stop_price:.8f}",
            priority="minor_correction"
        )

# ============================================================
# ۱۳. دریافت لیست ارزها (تاپ ۱۰۰)
# ============================================================
def get_all_symbols(data_fetcher_module) -> list:
    try:
        url = "https://data-api.binance.vision/api/v3/exchangeInfo"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        ticker_url = "https://data-api.binance.vision/api/v3/ticker/24hr"
        tr = requests.get(ticker_url, timeout=15)
        tr.raise_for_status()
        tickers = tr.json()
        volume_map = {}
        for t in tickers:
            sym = t.get("symbol", "")
            try:
                volume_map[sym] = float(t.get("quoteVolume", 0))
            except (ValueError, TypeError):
                volume_map[sym] = 0.0
        symbols = [
            s["symbol"] for s in data.get("symbols", [])
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
        ]
        symbols_with_volume = [(sym, volume_map.get(sym, 0)) for sym in symbols]
        symbols_with_volume.sort(key=lambda x: x[1], reverse=True)
        top_symbols = [sym for sym, _ in symbols_with_volume[:100]]
        logger.info(f"get_all_symbols | total={len(symbols)} | top100 selected")
        logger.info(f"  top5: {[(s, round(v/1e6, 1)) for s, v in symbols_with_volume[:5]]}")
        return top_symbols
    except Exception as e:
        logger.error(f"get_all_symbols failed | {e}")
        return []

# ============================================================
# ۱۴. تابع اصلی
# ============================================================
def main():
    start_ts = time.time()
    logger.info("=" * 60)
    logger.info("FWP cycle started")
    logger.info("=" * 60)

    load_state()
    strategy_module = load_strategy()

    import data_fetcher as data_fetcher_module
    import capital_manager as capital_manager_module
    import news_radar as news_radar_module

    if active_positions:
        logger.info(f"managing {len(active_positions)} active positions")
        try:
            all_news_for_positions = news_radar_module.fetch_all_news()
            manage_active_positions(
                strategy_module, data_fetcher_module, capital_manager_module, all_news_for_positions,
            )
        except Exception as e:
            logger.error(f"manage_active_positions failed | {e}")

    if active_positions:
        try:
            manage_step2_entries(strategy_module, data_fetcher_module, capital_manager_module)
        except Exception as e:
            logger.error(f"manage_step2_entries failed | {e}")

    if len(sys.argv) > 1 and sys.argv[1] == "active_only":
        logger.info("Active-only mode finished.")
        save_state()
        return

    try:
        all_news = news_radar_module.fetch_all_news()
        logger.info(f"news loaded | {len(all_news)} items")
    except Exception as e:
        logger.error(f"news fetch failed | {e}")
        all_news = []

    try:
        btc_df = data_fetcher_module.get_klines("BTCUSDT", "4h", KLINE_LIMIT)
        if btc_df is None:
            logger.error("BTC data failed — aborting")
            return
    except Exception as e:
        logger.error(f"BTC data failed | {e}")
        return

    symbols = get_all_symbols(data_fetcher_module)
    if not symbols:
        logger.error("no symbols — aborting")
        return

    symbols_to_scan = symbols
    logger.info(f"scanning {len(symbols_to_scan)} top-volume symbols")

    signals_found = 0
    for i, symbol in enumerate(symbols_to_scan, 1):
        if symbol in active_positions:
            continue
        try:
            signal = analyze_symbol(
                symbol=symbol,
                strategy_module=strategy_module,
                all_news=all_news,
                btc_df=btc_df,
                data_fetcher_module=data_fetcher_module,
            )
            if signal:
                signals_found += 1
                logger.info(f"signal found | {symbol} | {signal['type']}")
                process_signal(signal, signal.get("entry_price", 0))
        except Exception as e:
            logger.error(f"analyze {symbol} failed | {e}")
            continue

        if i % 20 == 0:
            logger.info(f"progress | {i}/{len(symbols_to_scan)} | signals={signals_found}")

        time.sleep(0.2)

    duration = time.time() - start_ts
    logger.info("=" * 60)
    logger.info(f"FWP cycle finished | duration={duration:.1f}s | signals={signals_found}")
    logger.info(f"active_positions={len(active_positions)} | saved_whale_orders={len(saved_whale_orders)}")
    logger.info("=" * 60)

    status_msg = (
        f"🤖 <b>FWP Status</b>\n"
        f"✅ اسکن کامل شد\n"
        f"⏱ مدت زمان: {duration:.0f} ثانیه\n"
        f"📊 ارزهای بررسی‌شده: {len(symbols_to_scan)}\n"
        f"🚀 سیگنال‌های پیدا شده: {signals_found}\n"
        f"📈 پوزیشن‌های فعال: {len(active_positions)}\n"
    )
    send_telegram(status_msg, priority="profit_update")

    try:
        tmp_path = os.path.join(tempfile.gettempdir(), "strategy.py")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
            logger.info(f"strategy.py cleaned up")
    except Exception as e:
        logger.warning(f"cleanup failed | {e}")

    save_state()

# ============================================================
# اجرا
# ============================================================
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"FATAL ERROR | {e}", exc_info=True)
        try:
            send_telegram(f"🚨 <b>FATAL ERROR</b>\n{str(e)[:500]}", priority="threat_alert")
        except Exception:
            pass
        sys.exit(1)
