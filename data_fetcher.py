"""
data_fetcher.py — فقط دریافت دیتا برای پروژه FWP
هیچ منطق معامله‌ای اینجا نیست. فقط دیتا.
"""

import time
import logging
from typing import Optional

import requests
import pandas as pd
import feedparser
import ccxt

from config import (
    LOG_LEVEL,
    MIN_WHALE_WALL_USD,
    OVERLAP_PRICE_TOLERANCE,
    MIN_OVERLAP_EXCHANGES,
    OVERLAP_EXCHANGES,
)

# ---------- تنظیمات logging ----------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("FWP.DataFetcher")

# ---------- ثابت‌ها ----------
TIMEOUT = 10  # ثانیه

BINANCE_SPOT_MIRROR = "https://data-api.binance.vision"
BINANCE_FUTURES = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"
OKX_BASE = "https://www.okx.com"
COINGECKO_BASE = "https://api.coingecko.com"

DEFAULT_RSS_FEEDS = [
    "https://coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]

# ---------- کش سراسری CoinGecko ----------
_COINGECKO_CACHE: dict = {
    "data": None,
    "fetched_at": 0.0,
    "ttl": 300,  # ۵ دقیقه
}

# ============================================================
# توابع کمکی
# ============================================================
def _base_asset(symbol: str) -> str:
    """BTCUSDT → BTC | 1000PEPEUSDT → 1000PEPE"""
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if symbol.upper().endswith(quote):
            return symbol[: -len(quote)]
    return symbol

def _to_okx_symbol(symbol: str) -> str:
    """BTCUSDT → BTC-USDT"""
    base = _base_asset(symbol)
    quote = symbol[len(base):]
    return f"{base}-{quote}"

def _to_ccxt_symbol(symbol: str) -> str:
    """BTCUSDT → BTC/USDT"""
    base = _base_asset(symbol)
    quote = symbol[len(base):]
    return f"{base}/{quote}"

def _bybit_interval(interval: str) -> str:
    """4h → 240 | 1d → D | 1m → 1"""
    mapping = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}
    return mapping.get(interval, "240")

def _okx_interval(interval: str) -> str:
    """4h → 4H | 1d → 1D | 1m → 1m"""
    mapping = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D"}
    return mapping.get(interval, "4H")

# ============================================================
# ۱. کندل‌ها (OHLCV)
# ============================================================
def get_klines(symbol: str, interval: str = "4h", limit: int = 300) -> Optional[pd.DataFrame]:
    """دریافت کندل OHLCV با fallback سه‌مرحله‌ای."""

    # ---- بایننس ----
    try:
        url = f"{BINANCE_SPOT_MIRROR}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            df = pd.DataFrame(
                data,
                columns=[
                    "time", "open", "high", "low", "close", "volume",
                    "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore",
                ],
            )
            df = df[["time", "open", "high", "low", "close", "volume"]]
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = df[c].astype(float)
            df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
            logger.info(f"get_klines OK (binance) | {symbol} | {interval} | {len(df)}")
            return df
    except Exception as e:
        logger.warning(f"get_klines binance failed | {symbol} | {e}")

    # ---- بای‌بیت ----
    try:
        url = f"{BYBIT_BASE}/v5/market/kline"
        params = {"category": "linear", "symbol": symbol, "interval": _bybit_interval(interval), "limit": limit}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        raw = js.get("result", {}).get("list", [])
        if raw:
            raw = list(reversed(raw))
            df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume", "turnover"])
            df = df[["time", "open", "high", "low", "close", "volume"]]
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = df[c].astype(float)
            df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
            logger.info(f"get_klines OK (bybit) | {symbol} | {interval} | {len(df)}")
            return df
    except Exception as e:
        logger.warning(f"get_klines bybit failed | {symbol} | {e}")

    # ---- اوکی‌اکس ----
    try:
        url = f"{OKX_BASE}/api/v5/market/candles"
        params = {"instId": _to_okx_symbol(symbol), "bar": _okx_interval(interval), "limit": limit}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        raw = js.get("data", [])
        if raw:
            raw = list(reversed(raw))
            rows = [row[:6] for row in raw]
            df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = df[c].astype(float)
            df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
            logger.info(f"get_klines OK (okx) | {symbol} | {interval} | {len(df)}")
            return df
    except Exception as e:
        logger.error(f"get_klines okx failed | {symbol} | {e}")

    logger.error(f"get_klines ALL FAILED | {symbol} | {interval}")
    return None

# ============================================================
# ۲. اردربوک تک‌صرافی (۷ صرافی)
# ============================================================
def get_order_book(symbol: str, limit: int = 100) -> Optional[dict]:
    """
    اردربوک با fallback ۷ صرافی.
    ترتیب: OKX → Kucoin → MEXC → Bitget → Gate → Binance → Bybit
    """

    # ---- ۱. OKX ----
    try:
        url = f"{OKX_BASE}/api/v5/market/books"
        params = {"instId": _to_okx_symbol(symbol), "sz": limit}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        raw = js.get("data", [])
        if raw:
            res = raw[0]
            bids = [[float(p), float(q)] for p, q, *_ in res.get("bids", [])]
            asks = [[float(p), float(q)] for p, q, *_ in res.get("asks", [])]
            if bids or asks:
                logger.info(f"get_order_book OK (okx) | {symbol} | bids={len(bids)} asks={len(asks)}")
                return {"bids": bids, "asks": asks}
    except Exception as e:
        logger.warning(f"get_order_book okx failed | {symbol} | {e}")

    # ---- ۲. Kucoin ----
    try:
        kc_symbol = _to_ccxt_symbol(symbol).replace("/", "-")
        url = "https://api.kucoin.com/api/v1/market/orderbook/level2_100"
        params = {"symbol": kc_symbol}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        data = js.get("data", {}) or {}
        bids = [[float(p), float(q)] for p, q, *_ in data.get("bids", [])]
        asks = [[float(p), float(q)] for p, q, *_ in data.get("asks", [])]
        if bids or asks:
            logger.info(f"get_order_book OK (kucoin) | {symbol} | bids={len(bids)} asks={len(asks)}")
            return {"bids": bids, "asks": asks}
    except Exception as e:
        logger.warning(f"get_order_book kucoin failed | {symbol} | {e}")

    # ---- ۳. MEXC ----
    try:
        url = "https://api.mexc.com/api/v3/depth"
        params = {"symbol": symbol, "limit": limit}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        bids = [[float(p), float(q)] for p, q in js.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in js.get("asks", [])]
        if bids or asks:
            logger.info(f"get_order_book OK (mexc) | {symbol} | bids={len(bids)} asks={len(asks)}")
            return {"bids": bids, "asks": asks}
    except Exception as e:
        logger.warning(f"get_order_book mexc failed | {symbol} | {e}")

    # ---- ۴. Bitget ----
    try:
        url = "https://api.bitget.com/api/v2/spot/market/orderbook"
        params = {"symbol": symbol, "limit": min(limit, 150)}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        data = js.get("data", {}) or {}
        bids = [[float(p), float(q)] for p, q in data.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in data.get("asks", [])]
        if bids or asks:
            logger.info(f"get_order_book OK (bitget) | {symbol} | bids={len(bids)} asks={len(asks)}")
            return {"bids": bids, "asks": asks}
    except Exception as e:
        logger.warning(f"get_order_book bitget failed | {symbol} | {e}")

    # ---- ۵. Gate.io ----
    try:
        gate_symbol = _to_ccxt_symbol(symbol).replace("/", "_")
        url = "https://api.gateio.ws/api/v4/spot/order_book"
        params = {"currency_pair": gate_symbol, "limit": limit}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        bids = [[float(p), float(q)] for p, q in js.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in js.get("asks", [])]
        if bids or asks:
            logger.info(f"get_order_book OK (gate) | {symbol} | bids={len(bids)} asks={len(asks)}")
            return {"bids": bids, "asks": asks}
    except Exception as e:
        logger.warning(f"get_order_book gate failed | {symbol} | {e}")

    # ---- ۶. Binance (fallback آخر) ----
    try:
        url = f"{BINANCE_FUTURES}/fapi/v1/depth"
        params = {"symbol": symbol, "limit": limit}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        bids = [[float(p), float(q)] for p, q in js.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in js.get("asks", [])]
        if bids or asks:
            logger.info(f"get_order_book OK (binance) | {symbol} | bids={len(bids)} asks={len(asks)}")
            return {"bids": bids, "asks": asks}
    except Exception as e:
        logger.warning(f"get_order_book binance failed | {symbol} | {e}")

    # ---- ۷. Bybit (fallback آخر) ----
    try:
        url = f"{BYBIT_BASE}/v5/market/orderbook"
        params = {"category": "linear", "symbol": symbol, "limit": limit}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        res = js.get("result", {})
        bids = [[float(p), float(q)] for p, q in res.get("b", [])]
        asks = [[float(p), float(q)] for p, q in res.get("a", [])]
        if bids or asks:
            logger.info(f"get_order_book OK (bybit) | {symbol} | bids={len(bids)} asks={len(asks)}")
            return {"bids": bids, "asks": asks}
    except Exception as e:
        logger.error(f"get_order_book bybit failed | {symbol} | {e}")

    logger.error(f"get_order_book ALL FAILED | {symbol}")
    return None

# ============================================================
# ۳. OI (OKX اول)
# ============================================================
def get_open_interest(symbol: str) -> Optional[float]:
    """OI (OKX اول)."""

    try:
        url = f"{OKX_BASE}/api/v5/public/open-interest"
        params = {"instType": "SWAP", "instId": _to_okx_symbol(symbol) + "-SWAP"}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        data = js.get("data", [])
        if data:
            oi = float(data[0].get("oi", 0))
            if oi > 0:
                logger.info(f"get_open_interest OK (okx) | {symbol} | {oi}")
                return oi
    except Exception as e:
        logger.warning(f"get_open_interest okx failed | {symbol} | {e}")

    try:
        url = f"{BINANCE_FUTURES}/fapi/v1/openInterest"
        params = {"symbol": symbol}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        oi = float(r.json().get("openInterest", 0))
        if oi > 0:
            logger.info(f"get_open_interest OK (binance) | {symbol} | {oi}")
            return oi
    except Exception as e:
        logger.warning(f"get_open_interest binance failed | {symbol} | {e}")

    try:
        url = f"{BYBIT_BASE}/v5/market/open-interest"
        params = {"category": "linear", "symbol": symbol}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        lst = r.json().get("result", {}).get("list", [])
        if lst:
            oi = float(lst[0].get("openInterest", 0))
            if oi > 0:
                logger.info(f"get_open_interest OK (bybit) | {symbol} | {oi}")
                return oi
    except Exception as e:
        logger.error(f"get_open_interest bybit failed | {symbol} | {e}")

    logger.error(f"get_open_interest ALL FAILED | {symbol}")
    return None

# ============================================================
# ۴. فاندینگ ریت
# ============================================================
def get_funding_rate(symbol: str) -> Optional[float]:
    """فاندینگ از بایننس → بای‌بیت."""

    try:
        url = f"{BINANCE_FUTURES}/fapi/v1/premiumIndex"
        params = {"symbol": symbol}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        fr = float(r.json().get("lastFundingRate", "NaN"))
        if fr == fr:
            logger.info(f"get_funding_rate OK (binance) | {symbol} | {fr}")
            return fr
    except Exception as e:
        logger.warning(f"get_funding_rate binance failed | {symbol} | {e}")

    try:
        url = f"{BYBIT_BASE}/v5/market/funding/history"
        params = {"category": "linear", "symbol": symbol, "limit": 1}
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        lst = r.json().get("result", {}).get("list", [])
        if lst:
            fr = float(lst[0].get("fundingRate", "NaN"))
            if fr == fr:
                logger.info(f"get_funding_rate OK (bybit) | {symbol} | {fr}")
                return fr
    except Exception as e:
        logger.error(f"get_funding_rate bybit failed | {symbol} | {e}")

    logger.error(f"get_funding_rate ALL FAILED | {symbol}")
    return None

# ============================================================
# ۵. مارکت‌کپ CoinGecko + fallback
# ============================================================
def _refresh_coingecko_cache(retry_on_429: bool = True) -> None:
    """پر کردن کش CoinGecko با retry 429."""
    now = time.time()
    if _COINGECKO_CACHE["data"] is not None and (now - _COINGECKO_CACHE["fetched_at"]) < _COINGECKO_CACHE["ttl"]:
        return

    try:
        url = f"{COINGECKO_BASE}/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
            "sparkline": "false",
        }
        r = requests.get(url, params=params, timeout=TIMEOUT)

        if r.status_code == 429:
            logger.warning("CoinGecko 429 Too Many Requests")
            if retry_on_429:
                logger.info("Sleeping 60s and retrying CoinGecko once...")
                time.sleep(60)
                _refresh_coingecko_cache(retry_on_429=False)
                return
            logger.error("CoinGecko 429 after retry — giving up")
            return

        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            logger.error("CoinGecko returned non-list")
            return

        mapping = {}
        for coin in data:
            sym = (coin.get("symbol") or "").upper()
            if sym:
                mapping[sym] = {
                    "id": coin.get("id"),
                    "market_cap": float(coin.get("market_cap") or 0),
                    "volume_24h": float(coin.get("total_volume") or 0),
                    "price": float(coin.get("current_price") or 0),
                }
        _COINGECKO_CACHE["data"] = mapping
        _COINGECKO_CACHE["fetched_at"] = now
        logger.info(f"CoinGecko cache refreshed | {len(mapping)} coins")
    except Exception as e:
        logger.error(f"CoinGecko refresh failed | {e}")

def get_market_cap(symbol: str) -> Optional[dict]:
    """مارکت‌کپ CoinGecko + fallback از بایننس."""
    _refresh_coingecko_cache()
    cache = _COINGECKO_CACHE["data"] or {}

    base = _base_asset(symbol).upper()
    if base in cache:
        info = cache[base]
        logger.info(f"get_market_cap OK | {symbol} | mc={info['market_cap']:.0f}")
        return {
            "market_cap": info["market_cap"],
            "volume_24h": info["volume_24h"],
            "price": info["price"],
        }

    logger.warning(f"get_market_cap NOT FOUND | {symbol} | base={base} | trying binance fallback")
    try:
        vol_url = f"{BINANCE_SPOT_MIRROR}/api/v3/ticker/24hr?symbol={symbol}"
        vr = requests.get(vol_url, timeout=5)
        if vr.status_code == 200:
            vol_data = vr.json()
            volume_24h = float(vol_data.get("quoteVolume", 0))
            price = float(vol_data.get("lastPrice", 0))
            if volume_24h > 0:
                logger.info(f"get_market_cap OK (binance fallback) | {symbol} | vol24h={volume_24h:.0f}")
                return {
                    "market_cap": 0,
                    "volume_24h": volume_24h,
                    "price": price,
                }
    except Exception as e:
        logger.warning(f"get_market_cap binance fallback failed | {symbol} | {e}")

    logger.error(f"get_market_cap ALL FAILED | {symbol}")
    return None

# ============================================================
# ۶. اخبار RSS
# ============================================================
def get_news_rss(feed_urls: Optional[list] = None, max_per_feed: int = 20) -> list:
    """خواندن اخبار از فیدهای RSS."""
    if feed_urls is None:
        feed_urls = DEFAULT_RSS_FEEDS

    results = []
    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            entries = feed.entries[:max_per_feed]
            for e in entries:
                results.append({
                    "title": e.get("title", ""),
                    "link": e.get("link", ""),
                    "published": e.get("published", ""),
                    "source": url,
                })
            logger.info(f"get_news_rss OK | {url} | {len(entries)} entries")
        except Exception as e:
            logger.warning(f"get_news_rss failed | {url} | {e}")

    return results

# ============================================================
# ۷. اردربوک چندصرافی + هم‌پوشانی
# ============================================================
def get_order_book_all_exchanges(symbol: str, min_wall_usd: float = MIN_WHALE_WALL_USD) -> dict:
    """اردربوک از OKX + Kucoin + MEXC + هم‌پوشانی بین‌صرافی."""
    exchange_names = OVERLAP_EXCHANGES
    exchange_instances = {
        "okx": ccxt.okx({"enableRateLimit": True}),
        "kucoin": ccxt.kucoin({"enableRateLimit": True}),
        "mexc": ccxt.mexc({"enableRateLimit": True}),
        "gate": ccxt.gate({"enableRateLimit": True}),
        "bitget": ccxt.bitget({"enableRateLimit": True}),
    }

    symbol_ccxt = _to_ccxt_symbol(symbol)
    walls_by_exchange: dict = {}

    for name in exchange_names:
        ex = exchange_instances.get(name)
        if ex is None:
            continue
        try:
            ob = ex.fetch_order_book(symbol_ccxt, limit=100)
            raw_bids = ob.get("bids") or []
            raw_asks = ob.get("asks") or []

            bids = []
            for w in raw_bids:
                if len(w) >= 2 and w[0] * w[1] >= min_wall_usd:
                    bids.append([float(w[0]), float(w[1])])

            asks = []
            for w in raw_asks:
                if len(w) >= 2 and w[0] * w[1] >= min_wall_usd:
                    asks.append([float(w[0]), float(w[1])])

            walls_by_exchange[name] = {"bids": bids, "asks": asks}
            logger.info(f"get_order_book_all OK | {name} | bids={len(bids)} asks={len(asks)}")
        except Exception as e:
            logger.warning(f"get_order_book_all failed | {name} | {e}")
        time.sleep(1)

    # ---- هم‌پوشانی ----
    overlap = []
    for side_key, side_name in (("bids", "bid"), ("asks", "ask")):
        all_walls = []
        for ex_name, data in walls_by_exchange.items():
            for p, q in data.get(side_key, []):
                all_walls.append((p, p * q, ex_name))

        used_indices = set()
        for i, (p1, usd1, ex1) in enumerate(all_walls):
            if i in used_indices:
                continue
            cluster = [(p1, usd1, ex1)]
            cluster_indices = [i]
            for j, (p2, usd2, ex2) in enumerate(all_walls):
                if j <= i or j in used_indices:
                    continue
                if ex2 == ex1:
                    continue
                if abs(p2 - p1) / p1 <= OVERLAP_PRICE_TOLERANCE:
                    cluster.append((p2, usd2, ex2))
                    cluster_indices.append(j)

            ex_set = {c[2] for c in cluster}
            if len(ex_set) >= MIN_OVERLAP_EXCHANGES:
                prices = [c[0] for c in cluster]
                total_usd = sum(c[1] for c in cluster)
                overlap.append({
                    "price_range": [min(prices), max(prices)],
                    "exchanges": sorted(ex_set),
                    "side": side_name,
                    "total_usd": total_usd,
                })
                used_indices.update(cluster_indices)

    if overlap:
        logger.info(f"cross_exchange_overlap FOUND | {len(overlap)} zones")

    return {"walls_by_exchange": walls_by_exchange, "cross_exchange_overlap": overlap}

# ============================================================
# ۸. کندل ۱ دقیقه
# ============================================================
def get_klines_1m(symbol: str, limit: int = 30) -> Optional[pd.DataFrame]:
    """کندل ۱ دقیقه — از همون get_klines با interval='1m'."""
    return get_klines(symbol=symbol, interval="1m", limit=limit)
