"""
lpd_data.py — دریافت دیتاهای LPD
"""
import os, time, requests, logging, html, json
import pandas as pd
from typing import Optional
from lpd.lpd_config import *

logger = logging.getLogger("LPD.Data")

# ============================================================
# ✅ کش پایدار (ذخیره تو فایل تا بین اجراها گم نشه)
# ============================================================
_API_CACHE_FILE = "lpd_api_cache.json"

def _load_cache() -> dict:
    if os.path.exists(_API_CACHE_FILE):
        try:
            return json.load(open(_API_CACHE_FILE, "r", encoding="utf-8"))
        except Exception:
            pass
    return {"fail": {}, "health_last": 0, "health_state": {}}

def _save_cache(cache: dict) -> None:
    try:
        json.dump(cache, open(_API_CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ save api cache failed | {e}")

_API_FAIL_COOLDOWN = 1800   # ۳۰ دقیقه
_API_HEALTH_COOLDOWN = 600  # ۱۰ دقیقه

def _send_telegram_simple(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return True
    except Exception:
        return False

def notify_api_failure(component: str, error: object = "") -> None:
    """اعلام خرابی با کول‌داون ۳۰ دقیقه‌ای که تو فایل ذخیره میشه."""
    cache = _load_cache()
    now = time.time()
    last = cache["fail"].get(component, 0)
    if now - last < _API_FAIL_COOLDOWN:
        return
    cache["fail"][component] = now
    _save_cache(cache)

    err_text = html.escape(str(error)[:200]) if error else "unknown"
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    msg = (
        f"⚠️ <b>API DOWN</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• بخش: <b>{html.escape(component)}</b>\n"
        f"• خطا: {err_text}\n"
        f"• زمان (UTC): {ts}\n"
        f"• کول‌داون: ۳۰ دقیقه"
    )
    _send_telegram_simple(msg)
    logger.error(f"API DOWN | {component} | {err_text}")

def notify_api_recovered(component: str) -> None:
    """اعلام بازیابی."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    msg = (
        f"✅ <b>API RECOVERED</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• بخش: <b>{html.escape(component)}</b>\n"
        f"• زمان (UTC): {ts}"
    )
    _send_telegram_simple(msg)
    logger.info(f"API RECOVERED | {component}")

def check_api_health() -> None:
    """هر ۱۰ دقیقه یک‌بار (بر اساس فایل) هر سه کلید رو چک می‌کنه."""
    cache = _load_cache()
    now = time.time()
    if now - cache.get("health_last", 0) < _API_HEALTH_COOLDOWN:
        return
    cache["health_last"] = now

    pairs = [
        ("LPDtop", BYKARANTELI_KEY_LPD),
        ("FWP-Bot", BYKARANTELI_KEY_FWP),
        ("Backup", BYKARANTELI_KEY_BACKUP),
    ]

    for name, key in pairs:
        if not key:
            status = "missing"
        else:
            try:
                r = requests.get(
                    "https://bykaranteli.com/api/v1/usage",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=10,
                )
                if r.status_code == 200:
                    status = "ok"
                elif r.status_code == 401:
                    status = "invalid_key"
                elif r.status_code == 429:
                    status = "rate_limit"
                else:
                    status = f"HTTP {r.status_code}"
            except Exception:
                status = "timeout"

        prev = cache["health_state"].get(name)
        cache["health_state"][name] = status

        if status != "ok":
            notify_api_failure(f"ByKaranteli ({name} key)", status)
        else:
            if prev is not None and prev != "ok":
                notify_api_recovered(f"ByKaranteli ({name} key)")

    _save_cache(cache)

# ============================================================
# کندل بایننس
# ============================================================
def get_klines_binance(symbol: str, interval: str, limit: int = 300) -> Optional[pd.DataFrame]:
    try:
        r = requests.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        df = pd.DataFrame(
            data,
            columns=[
                "time", "open", "high", "low", "close", "volume",
                "close_time", "qav", "trades", "taker_base",
                "taker_quote", "ignore",
            ],
        )[["time", "open", "high", "low", "close", "volume"]]
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        logger.warning(f"⚠️ Binance Klines failed | {symbol} | {e}")
        notify_api_failure("Binance Klines", e)
        return None

# ============================================================
# اردربوک: OKX Futures + فالبک Binance Spot
# ============================================================
def get_orderbook_okx_futures(symbol: str) -> Optional[dict]:
    try:
        base = symbol[:-4]
        r = requests.get(
            "https://www.okx.com/api/v5/market/books",
            params={"instId": f"{base}-USDT-SWAP", "sz": 100},
            timeout=5,
        )
        r.raise_for_status()
        js = r.json()
        if js.get("data"):
            res = js["data"][0]
            bids = [[float(p), float(q)] for p, q, *_ in res["bids"]]
            asks = [[float(p), float(q)] for p, q, *_ in res["asks"]]
            if bids or asks:
                return {"bids": bids, "asks": asks}
    except Exception as e:
        logger.debug(f"OKX Futures OB failed | {symbol} | {e}")

    try:
        r = requests.get(
            "https://data-api.binance.vision/api/v3/depth",
            params={"symbol": symbol, "limit": 100},
            timeout=5,
        )
        js = r.json()
        if js.get("bids"):
            return {
                "bids": [[float(p), float(q)] for p, q in js["bids"]],
                "asks": [[float(p), float(q)] for p, q in js["asks"]],
            }
    except Exception as e:
        logger.error(f"❌ Binance Spot OB failed | {symbol} | {e}")
        notify_api_failure("OrderBook (OKX + Binance fallback)", e)
        return None

    notify_api_failure("OrderBook (OKX + Binance fallback)", "no bids/asks")
    return None

# ============================================================
# معاملات واقعی OKX
# ============================================================
def get_recent_trades_okx(symbol: str, limit: int = 100) -> Optional[list]:
    try:
        base = symbol[:-4]
        r = requests.get(
            "https://www.okx.com/api/v5/market/trades",
            params={"instId": f"{base}-USDT-SWAP", "limit": limit},
            timeout=5,
        )
        r.raise_for_status()
        js = r.json()
        if js.get("data"):
            trades = []
            for t in js["data"]:
                try:
                    trades.append({
                        "price": float(t["px"]),
                        "size": float(t["sz"]),
                        "side": t["side"],
                        "ts": int(t["ts"]),
                    })
                except (KeyError, ValueError):
                    continue
            logger.debug(f"trades OK | {symbol} | {len(trades)} trades")
            return trades
    except Exception as e:
        logger.warning(f"⚠️ OKX Trades failed | {symbol} | {e}")
        notify_api_failure("OKX Trades", e)
    return None

# ============================================================
# ByKaranteli با سه کلید و Fallback
# ============================================================
def _fetch_bykaranteli(path: str, params: dict, key_type: str) -> Optional[dict]:
    if key_type == "LPD":
        ordered = [
            ("LPDtop", BYKARANTELI_KEY_LPD),
            ("Backup", BYKARANTELI_KEY_BACKUP),
            ("FWP-Bot", BYKARANTELI_KEY_FWP),
        ]
    else:
        ordered = [
            ("FWP-Bot", BYKARANTELI_KEY_FWP),
            ("Backup", BYKARANTELI_KEY_BACKUP),
            ("LPDtop", BYKARANTELI_KEY_LPD),
        ]

    last_err = None
    for name, key in ordered:
        if not key:
            continue
        try:
            r = requests.get(
                f"https://bykaranteli.com{path}",
                params=params,
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 401:
                last_err = "invalid_key (401)"
                notify_api_failure(f"ByKaranteli ({name} key)", last_err)
                logger.warning(f"⚠️ ByKaranteli 401 | {name} | trying next...")
                continue
            elif r.status_code == 429:
                last_err = "rate_limit (429)"
                notify_api_failure(f"ByKaranteli ({name} key)", last_err)
                logger.warning(f"⚠️ ByKaranteli 429 | {name} | trying next...")
                continue
            else:
                last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = e
            logger.warning(f"⚠️ ByKaranteli exception | {name} | {e}")

    logger.error(f"❌ ByKaranteli ALL KEYS FAILED | {key_type} | {last_err}")
    notify_api_failure(f"ByKaranteli ({key_type} ALL KEYS)", last_err or "no valid key")
    return None

def get_whale_tape(symbol: str) -> Optional[dict]:
    data = _fetch_bykaranteli("/api/public/whales", {}, "LPD")
    if not data or not data.get("recent"):
        return None

    cutoff = time.time() - 3600
    buy, sell = 0.0, 0.0
    for t in data["recent"]:
        if str(t.get("symbol", "")).upper() not in (
            symbol.upper(),
            symbol.replace("USDT", "").upper(),
        ):
            continue
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(
                t.get("trade_time", "").replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue
        usd = float(t.get("quote_qty", 0))
        if t.get("side") == "BUY":
            buy += usd
        elif t.get("side") == "SELL":
            sell += usd

    if sell > buy * 1.3 and sell > 100_000:
        pressure = "sell"
    elif buy > sell * 1.3 and buy > 100_000:
        pressure = "buy"
    else:
        pressure = "neutral"

    return {"buy_usd": buy, "sell_usd": sell, "pressure": pressure}

def get_liqmap(symbol: str) -> Optional[dict]:
    base = symbol.replace("USDT", "")
    data = _fetch_bykaranteli("/api/liqmap/public", {"symbol": base}, "FWP")
    if not data or not data.get("ok"):
        return None
    points = data.get("real_liquidations", {}).get("points", [])
    if not points:
        return None

    now = time.time() * 1000
    h0, h1 = now - 3_600_000, now - 7_200_000

    short_h0 = sum(
        float(p.get("notional_usd", 0))
        for p in points
        if p.get("side") == "short" and p.get("time", 0) >= h0
    )
    short_h1 = sum(
        float(p.get("notional_usd", 0))
        for p in points
        if p.get("side") == "short" and h1 <= p.get("time", 0) < h0
    )

    return {
        "fuel_exhausted": (short_h1 > 500_000 and short_h0 < short_h1 * 0.3)
    }
