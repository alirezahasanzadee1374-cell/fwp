"""
lpd_data.py — دریافت دیتاهای LPD
"""
import os, time, requests, logging
import pandas as pd
from typing import Optional
from lpd.lpd_config import *

logger = logging.getLogger("LPD.Data")

def get_klines_binance(symbol: str, interval: str, limit: int = 300) -> Optional[pd.DataFrame]:
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data: return None
        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore"])[["time", "open", "high", "low", "close", "volume"]]
        for c in ["open", "high", "low", "close", "volume"]: df[c] = df[c].astype(float)
        return df
    except Exception as e:
        logger.warning(f"⚠️ Binance Klines failed | {symbol} | {e}")
        return None

def get_orderbook_okx_futures(symbol: str) -> Optional[dict]:
    try:
        base = symbol[:-4]
        r = requests.get("https://www.okx.com/api/v5/market/books", params={"instId": f"{base}-USDT-SWAP", "sz": 100}, timeout=5)
        r.raise_for_status()
        js = r.json()
        if js.get("data"):
            res = js["data"][0]
            bids = [[float(p), float(q)] for p, q, *_ in res["bids"]]
            asks = [[float(p), float(q)] for p, q, *_ in res["asks"]]
            if bids or asks: return {"bids": bids, "asks": asks}
    except Exception:
        pass
    logger.debug(f"Falling back to Spot OB for {symbol} (OKX Futures failed)")
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/depth", params={"symbol": symbol, "limit": 100}, timeout=5)
        js = r.json()
        if js.get("bids"): return {"bids": [[float(p), float(q)] for p, q in js["bids"]], "asks": [[float(p), float(q)] for p, q in js["asks"]]}
    except Exception as e:
        logger.error(f"❌ Binance Spot OB failed | {symbol} | {e}")
    return None

# ============================================================
# ✅ جدید: دریافت معاملات واقعی از OKX (برای تایید ۱۰۰٪)
# ============================================================
def get_recent_trades_okx(symbol: str, limit: int = 100) -> Optional[list]:
    """
    دریافت ۱۰۰ معامله اخیر از OKX فیوچرز.
    این تنها راه تشخیص واقعی «پر شدن سفارش» از «لغو شدن» هست.
    """
    try:
        base = symbol[:-4]
        r = requests.get(
            "https://www.okx.com/api/v5/market/trades",
            params={"instId": f"{base}-USDT-SWAP", "limit": limit},
            timeout=5
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
                        "side": t["side"],          # "buy" یا "sell"
                        "ts": int(t["ts"])           # timestamp (ms)
                    })
                except (KeyError, ValueError):
                    continue
            logger.debug(f"trades OK | {symbol} | {len(trades)} trades")
            return trades
    except Exception as e:
        logger.warning(f"⚠️ OKX Trades failed | {symbol} | {e}")
    return None

def _fetch_bykaranteli(path: str, params: dict, key_type: str) -> Optional[dict]:
    keys = [BYKARANTELI_KEY_LPD, BYKARANTELI_KEY_FWP] if key_type == "LPD" else [BYKARANTELI_KEY_FWP, BYKARANTELI_KEY_LPD]
    for key in keys:
        if not key: continue
        try:
            r = requests.get(f"https://bykaranteli.com{path}", params=params, headers={"Authorization": f"Bearer {key}"}, timeout=10)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                logger.warning(f"⚠️ Rate limit on {key_type} key, trying fallback...")
                continue
        except Exception:
            pass
    return None

def get_whale_tape(symbol: str) -> Optional[dict]:
    data = _fetch_bykaranteli("/api/public/whales", {}, "LPD")
    if not data or not data.get("recent"): return None
    
    cutoff = time.time() - 3600
    buy, sell = 0.0, 0.0
    for t in data["recent"]:
        if str(t.get("symbol", "")).upper() not in (symbol.upper(), symbol.replace("USDT", "").upper()): continue
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(t.get("trade_time", "").replace("Z", "+00:00")).timestamp()
        except: continue
        if ts < cutoff: continue
        usd = float(t.get("quote_qty", 0))
        if t.get("side") == "BUY": buy += usd
        elif t.get("side") == "SELL": sell += usd

    pressure = "sell" if sell > buy * 1.3 and sell > 100_000 else "buy" if buy > sell * 1.3 and buy > 100_000 else "neutral"
    return {"buy_usd": buy, "sell_usd": sell, "pressure": pressure}

def get_liqmap(symbol: str) -> Optional[dict]:
    base = symbol.replace("USDT", "")
    data = _fetch_bykaranteli("/api/liqmap/public", {"symbol": base}, "FWP")
    if not data or not data.get("ok"): return None
    points = data.get("real_liquidations", {}).get("points", [])
    if not points: return None

    now = time.time() * 1000
    h0, h1 = now - 3_600_000, now - 7_200_000
    
    short_h0 = sum(float(p.get("notional_usd", 0)) for p in points if p.get("side") == "short" and p.get("time", 0) >= h0)
    short_h1 = sum(float(p.get("notional_usd", 0)) for p in points if p.get("side") == "short" and h1 <= p.get("time", 0) < h0)
    
    return {"fuel_exhausted": (short_h1 > 500_000 and short_h0 < short_h1 * 0.3)}
