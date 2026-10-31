"""Liquid / Hyperliquid data for the app. Standard library only.

Liquid trades US stocks as perpetuals on Hyperliquid's "xyz" market list
(xyz:AAPL, xyz:NVDA, xyz:SP500 ...). Hyperliquid's info API is public and
needs no key, so the app reads it directly:

  live candles    the browser subscribes to wss://api.hyperliquid.xyz/ws
                  (type "candle", 1m) for the forming candle; /api/live serves
                  the same 1m candles over HTTP as the fallback
  perp context    funding, open interest, premium of the perp over the oracle

Liquid's smart-money split (profitable vs losing traders) is not public: it is
only served through the Liquid connector in Claude. Claude writes it to
data/intel.json (see README) and this module only reads that file.
"""

import json
import os
import time
import urllib.request

import engine

API = "https://api.hyperliquid.xyz/info"
HERE = os.path.dirname(os.path.abspath(__file__))
INTEL = os.path.join(HERE, "data", "intel.json")

# Index ETFs have no 1:1 perp; their positioning comes from the index perp,
# but their candles must not (different price level).
POSITIONING_ALIAS = {"SPY": "SP500", "QQQ": "XYZ100"}


def _post(body, timeout=15):
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": engine.UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _universe():
    def load():
        meta, ctxs = _post({"type": "metaAndAssetCtxs", "dex": "xyz"})
        return {u["name"].split(":", 1)[1]: c for u, c in zip(meta["universe"], ctxs)}
    return engine._cached(("hl-universe",), 15, load)


def perp_coin(symbol):
    """The Hyperliquid coin whose candles match this stock 1:1, or None."""
    sym = symbol.upper()
    try:
        return "xyz:" + sym if sym in _universe() else None
    except Exception:
        return None


def perp_context(symbol):
    sym = symbol.upper()
    try:
        u = _universe()
    except Exception:
        return None
    key = sym if sym in u else POSITIONING_ALIAS.get(sym)
    c = u.get(key) if key else None
    if not c:
        return None
    mark, oracle = float(c["markPx"]), float(c.get("oraclePx") or c["markPx"])
    prev = float(c.get("prevDayPx") or mark)
    return {"coin": "xyz:" + key, "same_instrument": key == sym, "mark": mark, "oracle": oracle,
            "premium_pct": round((mark - oracle) / oracle * 100, 4),
            "funding_pct_hr": round(float(c["funding"]) * 100, 5),
            "open_interest_usd": round(float(c["openInterest"]) * mark),
            "day_volume_usd": round(float(c.get("dayNtlVlm") or 0)),
            "change_24h_pct": round((mark - prev) / prev * 100, 2)}


def live_candles(symbol, minutes=30):
    """Recent 1m candles of the matching perp as [t, o, h, l, c, v] (t = start, seconds)."""
    coin = perp_coin(symbol)
    if not coin:
        return None
    now = int(time.time() * 1000)

    def load():
        rows = _post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1m",
                                                        "startTime": now - minutes * 60000, "endTime": now}})
        return [[r["t"] // 1000, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])] for r in rows]
    return {"coin": coin, "candles": engine._cached(("hl-1m", coin), 4, load)}


def intel(symbol):
    """Liquid positioning that Claude saved, with its age. None when there is none."""
    try:
        with open(INTEL, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    sym = symbol.upper()
    row = doc.get("symbols", {}).get(sym)
    if not row:
        return None
    out = dict(row, updated_at=doc.get("updated_at"), source=doc.get("source"))
    sm, crowd = row.get("smart_long"), row.get("crowd_long")
    if sm is not None and crowd is not None:
        # Liquid's own "contrarian" reading: profitable traders lean the other way from the crowd
        if crowd - sm >= 0.25 and sm < 0.5:
            out["read"] = "against"   # smart money short into a crowded long: bad for a long
        elif sm - crowd >= 0.25 and sm > 0.5:
            out["read"] = "with"      # smart money long, crowd short
        else:
            out["read"] = "neutral"
    return out
