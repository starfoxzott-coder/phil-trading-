#!/usr/bin/env python3
"""Phil Day-Trade: local web app. Standard library only.

    python3 app/server.py               # http://localhost:8787
    python3 app/server.py --port 9000
    python3 app/server.py learn         # one learning cycle, then exit
    python3 app/server.py scan          # print the watchlist's calls, then exit

While the server runs it learns by itself once per trading day after the
close (16:05 New York time): settle, score, evolve, retro (see learn.py).
"""

import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402
import learn  # noqa: E402
import liquid  # noqa: E402

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ORDER = {"KAUFEN": 0, "LÄUFT": 1, "WARTEN": 1, "BEOBACHTEN": 2, "VORBEI": 3, "KEIN TRADE": 4}
_lock = threading.Lock()
HEAD = (b'<!doctype html><html lang="de"><head><meta charset="utf-8">'
        b'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"></head><body>')


def analyze(symbol, replays=2000, news=True):
    strategy = learn.load_strategy()
    date = engine.et_now().date().isoformat()
    intel = liquid.intel(symbol)
    a = engine.analyze(symbol, strategy, learn.today_trades(date), mc_replays=replays, intel=intel)
    a["perp"] = liquid.perp_context(symbol)
    a["live_coin"] = liquid.perp_coin(symbol)
    a["intel"] = intel
    with _lock:
        sig = learn.record_signal(a)
    a["signal"] = sig
    a["generation"] = strategy.get("generation", 0)
    a["min_ev_r"] = strategy.get("min_ev_r", 0.10)
    if news:
        a["news"] = engine.fetch_news(symbol)
    return a


def scan(replays=600):
    strategy = learn.load_strategy()

    def one(sym):
        try:
            a = analyze(sym, replays=replays, news=False)
            sim = a.get("sim") or {}
            return {"symbol": sym, "action": a["decision"]["action"], "reason": (a["decision"]["reasons"] or [""])[0],
                    "price": a["price"], "entry": (a.get("plan") or {}).get("entry"),
                    "stop": (a.get("plan") or {}).get("stop"), "target": (a.get("plan") or {}).get("target"),
                    "edge_pp": sim.get("edge_pp"), "ev_net": sim.get("ev_net"),
                    "grade": (a.get("quality") or {}).get("grade"), "hist_avg_r": a["history"]["avg_r"], "hist_n": a["history"]["n"]}
        except Exception as e:  # one bad ticker must not sink the scan
            return {"symbol": sym, "action": "FEHLER", "reason": str(e)[:120]}

    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        rows = list(ex.map(one, strategy["watchlist"]))
    rows.sort(key=lambda r: (ORDER.get(r["action"], 9), -(r.get("ev_net") if r.get("ev_net") is not None else -99)))
    return rows


def yahoo_live(symbol):
    """1m bars from Yahoo for tickers without a matching perp (SPY, QQQ ...)."""
    d = engine.fetch_bars(symbol, "1d", "1m", ttl=4)
    rows = [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in d["bars"] if b["t"] % 60 == 0][-30:]
    px = d["meta"].get("regularMarketPrice")
    if rows and px:  # Yahoo's live price moves the newest bar before it closes
        r = rows[-1]
        r[2], r[3], r[4] = max(r[2], px), min(r[3], px), px
    return {"coin": None, "source": "Yahoo 1m", "candles": rows}


def state():
    strategy = learn.load_strategy()
    rows = learn.read_journal()
    return {"strategy": strategy, "score": learn.score(rows), "journal": rows[-60:][::-1],
            "history": learn.history()[-40:][::-1], "describe": learn.describe(strategy["params"]),
            "now_et": engine.et_now().strftime("%Y-%m-%d %H:%M")}


def save_settings(body):
    s = learn.load_strategy()
    for k, v in (body.get("account") or {}).items():
        if k in s["account"]:
            s["account"][k] = float(v)
    for k, v in (body.get("rules") or {}).items():
        if k in s["rules"]:
            if k in ("min_reward_risk", "cost_bps"):
                s["rules"][k] = float(v)
            elif k == "min_quality":
                if str(v).strip().upper() not in ("A", "B", "C"):
                    raise ValueError("Mindest-Qualität: A, B oder C")
                s["rules"][k] = str(v).strip().upper()
            else:
                engine.hhmm_to_min(str(v))  # validates HH:MM
                s["rules"][k] = str(v)
    if "watchlist" in body:
        wl = [str(x).strip().upper() for x in body["watchlist"] if str(x).strip()]
        if not wl or any(not x.replace("-", "").replace(".", "").isalnum() or len(x) > 10 for x in wl):
            raise ValueError("Watchlist: nur Ticker wie AAPL, BRK-B")
        s["watchlist"] = wl[:40]
    learn.save_strategy(s)
    return s


def auto_learner():
    """Learn once per trading day after the close."""
    while True:
        try:
            now = engine.et_now()
            s = learn.load_strategy()
            done = (s.get("learned_at") or "")[:10]
            if now.weekday() < 5 and engine.minute_of_day(now) >= 16 * 60 + 5 and done != now.date().isoformat():
                with _lock:
                    learn.cycle()
                print("[phil] Lernzyklus fertig", now.strftime("%H:%M"), flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(600)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, fn):
        try:
            self._send(200, fn())
        except (engine.EngineError, ValueError, KeyError) as e:
            self._send(400, {"error": str(e)})
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "interner Fehler – siehe Terminal"})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), "rb") as f:
                page = HEAD + f.read() + b"</body></html>"
            return self._send(200, page, "text/html; charset=utf-8")
        if u.path == "/api/analyze":
            return self._guard(lambda: analyze(q.get("symbol", "SPY")))
        if u.path == "/api/live":
            return self._guard(lambda: liquid.live_candles(q.get("symbol", "SPY")) or yahoo_live(q.get("symbol", "SPY")))
        if u.path == "/api/scan":
            return self._guard(scan)
        if u.path == "/api/state":
            return self._guard(state)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        p = urlparse(self.path).path
        if p == "/api/learn":
            def run():
                with _lock:
                    return learn.cycle()
            return self._guard(run)
        if p == "/api/taken":
            return self._guard(lambda: (learn.mark_taken(body["id"], body.get("taken", True)), state())[1])
        if p == "/api/settings":
            return self._guard(lambda: save_settings(body))
        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "learn", "scan"])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.cmd == "learn":
        r = learn.cycle()
        print(json.dumps({k: r[k] for k in ("changes", "current", "score")}, indent=2, ensure_ascii=False))
        return
    if a.cmd == "scan":
        for r in scan():
            print("%-6s %-11s %s" % (r["symbol"], r["action"], r.get("reason", "")))
        return
    threading.Thread(target=auto_learner, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = "http://%s:%d" % ("localhost" if a.host in ("127.0.0.1", "0.0.0.0") else a.host, a.port)
    print("Phil Day-Trade läuft auf", url, "(Strg+C beendet)", flush=True)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
