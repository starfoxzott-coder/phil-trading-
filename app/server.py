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
import re
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
import trades  # noqa: E402
import chartdata  # noqa: E402
import skills  # noqa: E402

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ORDER = {"KAUFEN": 0, "LÄUFT": 1, "WARTEN": 1, "BEOBACHTEN": 2, "VORBEI": 3, "KEIN TRADE": 4}
_lock = threading.Lock()
HEAD = (b'<!doctype html><html lang="de"><head><meta charset="utf-8">'
        b'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        # "add to home screen" on a phone: own icon, full screen, no browser bar
        b'<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/icon-180.png">'
        b'<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
        b'<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        b'<meta name="apple-mobile-web-app-title" content="Phil"><meta name="theme-color" content="#0d1211">'
        b'</head><body>')
MANIFEST = {"name": "Phil Day-Trade", "short_name": "Phil", "start_url": "/", "display": "standalone",
            "background_color": "#0d1211", "theme_color": "#0d1211", "lang": "de",
            "icons": [{"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}


def analyze(symbol, replays=2000, news=True, now_ts=None):
    """The app's call for one ticker; with now_ts, the call as it stood at that moment."""
    strategy = learn.load_strategy()
    date = engine.et_now(now_ts).date().isoformat()
    intel = liquid.intel(symbol)
    taken = learn.today_trades(date)
    counted = {r["id"] for r in taken}
    taken += [t for t in trades.today_risk(date) if t.get("signal_id") not in counted]
    a = engine.analyze(symbol, strategy, taken, now_ts=now_ts, mc_replays=replays, intel=intel)
    a["perp"] = liquid.perp_context(symbol)
    a["live_coin"] = liquid.perp_coin(symbol)
    a["intel"] = intel
    a["my_trades"] = trades.for_day(symbol, a["date"])
    with _lock:
        sig = learn.record_signal(a)
    a["signal"] = sig
    a["generation"] = strategy.get("generation", 0)
    a["min_ev_r"] = strategy.get("min_ev_r", 0.10)
    if now_ts is None:
        apply_skill_check(a, strategy, run_now=a["decision"]["action"] == "KAUFEN")
    if news:
        a["news"], a["news_source"] = skill_news(strategy, symbol)
    return a


_skill_cache = {}


def _skill_key(a):
    p = a.get("plan") or {}
    return (a["symbol"], a["date"], p.get("entry_t"), p.get("stop"), p.get("target"), a.get("bar_minutes"))


def apply_skill_check(a, strategy, run_now=False):
    """The skills' own verdict on this plan: run it for a fresh buy (or on request), reuse it for 15 minutes.

    trader-risk is the gate: a BLOCK from the skill turns a KAUFEN into KEIN TRADE.
    """
    if not a.get("plan"):
        return None
    key = _skill_key(a)
    hit = _skill_cache.get(key)
    if hit and time.time() - hit[0] < 900:
        res = hit[1]
    elif run_now:
        res = skills.signal_check(a, strategy)
        _skill_cache[key] = (time.time(), res)
    else:
        return None
    res = {k: v for k, v in (res or {}).items() if k != "raw"} or None  # the page needs the summary, not every band
    a["skills"] = res
    if res and res.get("risk_verdict"):
        ok = res["risk_verdict"] == "PASS"
        a.setdefault("checks", []).insert(3, {"key": "skill_risk", "hard": True, "ok": ok,
                                              "label": "trader-risk (Skill) gibt die Order frei",
                                              "detail": "PASS" if ok else "; ".join(res.get("risk_reasons") or [])[:160]})
        if not ok and a["decision"]["action"] == "KAUFEN":
            a["decision"]["action"] = "KEIN TRADE"
            a["decision"]["reasons"].insert(0, "trader-risk (Skill) blockiert: " + "; ".join(res.get("risk_reasons") or ["ohne Grund"]))
    return res


_news_cache = {}


def skill_news(strategy, symbol):
    """Headlines and SEC filings from trader-news; Yahoo search as the fallback."""
    hit = _news_cache.get(symbol)
    if hit and time.time() - hit[0] < 600:
        return hit[1], hit[2]
    r = skills.news(strategy, symbol)
    if r.get("ok") and r.get("data"):
        items = [{"title": it.get("title"), "publisher": it.get("source"), "link": it.get("link"), "kind": it.get("kind"),
                  "tags": it.get("tags") or [], "new": it.get("new"),
                  "time": engine.parse_iso_ts(it.get("time"))} for it in (r["data"].get("items") or [])[:12]]
        _news_cache[symbol] = (time.time(), items, "trader-news")
        return items, "trader-news"
    return engine.fetch_news(symbol), "Yahoo"


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


def trades_state():
    rows = trades.all_trades()
    rows.sort(key=lambda t: (t["date"], t["time"]), reverse=True)
    return {"trades": rows, "summary": trades.summary(), "mistakes": trades.MISTAKES,
            "claude": claude_ok()}


def claude_ok():
    try:
        import claude_review
        return claude_review.available()
    except Exception:
        return False


def open_trade(body):
    analysis = None
    try:  # the app's view at the moment you bought, from the bars closed by then
        date = body.get("date") or engine.et_now().date().isoformat()
        at = trades.et_ts(date, body.get("time") or engine.et_now().strftime("%H:%M")) + 1
        analysis = analyze(body.get("symbol", ""), replays=600, news=False, now_ts=min(at, time.time()))
    except (engine.EngineError, ValueError):
        pass  # a ticker without data can still be logged, just without the app's plan
    t = trades.open_trade(body, analysis)
    if t.get("signal_id"):
        learn.mark_taken(t["signal_id"], True)
    try:  # trader-alerts watches your own stop and target from now on
        skills.alerts_for_plan(learn.load_strategy(), t["symbol"], t["date"], stop=t.get("stop"), target=t.get("target"), tag="trade")
    except skills.SkillError:
        pass
    return t


def review(trade_id):
    import claude_review
    t = trades.get(trade_id)
    if not t:
        raise ValueError("Trade nicht gefunden")
    if t["status"] != "closed":
        raise ValueError("Erst den Trade abschließen, dann auswerten")
    try:
        r = claude_review.review_trade(t)
    except claude_review.ReviewError as e:
        raise ValueError(str(e))
    return trades.set_review(trade_id, r)


def ask(body):
    import claude_review
    try:
        return claude_review.ask(body.get("turns") or [], body.get("images") or [])
    except claude_review.ReviewError as e:
        raise ValueError(str(e))


def read_shot(body):
    import claude_review
    name = trades.save_image("draft", body.get("image"), "read")
    try:
        fields = claude_review.read_screenshot([name], body.get("symbol"))
    except claude_review.ReviewError as e:
        return {"image_name": name, "error": str(e)}
    return {"image_name": name, "fields": fields}


def state():
    strategy = learn.load_strategy()
    m = strategy["active_minutes"]
    rows = [r for r in learn.read_journal() if r.get("bar_minutes", 5) == m]
    return {"strategy": strategy, "score": learn.score(rows), "journal": rows[-60:][::-1], "bar_minutes": m,
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
            elif k == "bar_minutes":
                if int(float(v)) not in engine.BAR_MINUTES:
                    raise ValueError("Kerzenlänge: 1, 2, 5, 15 oder 30 Minuten")
                s["rules"][k] = int(float(v))
            elif k == "min_quality":
                if str(v).strip().upper() not in ("A", "B", "C"):
                    raise ValueError("Mindest-Qualität: A, B oder C")
                s["rules"][k] = str(v).strip().upper()
            else:
                engine.hhmm_to_min(str(v))  # validates HH:MM
                s["rules"][k] = str(v)
    for k in ("sec_user_agent", "skills_dir"):
        if k in body:
            v = str(body[k] or "").strip()
            if len(v) > 300 or any(c in v for c in "\r\n"):
                raise ValueError("%s: höchstens eine Zeile" % k)
            s[k] = v
    if "watchlist" in body:
        wl = [str(x).strip().upper() for x in body["watchlist"] if str(x).strip()]
        if not wl or any(not x.replace("-", "").replace(".", "").isalnum() or len(x) > 10 for x in wl):
            raise ValueError("Watchlist: nur Ticker wie AAPL, BRK-B")
        s["watchlist"] = wl[:40]
    learn.save_strategy(s)
    return s


def skill_daemon():
    """trader-alerts every minute in market hours; the whole-market scan once before the open."""
    last_scan = None
    while True:
        try:
            now = engine.et_now()
            m = engine.minute_of_day(now)
            s = learn.load_strategy()
            if now.weekday() < 5 and skills.discover(s.get("skills_dir")):
                if 9 * 60 <= m < 9 * 60 + 25 and last_scan != now.date():
                    last_scan = now.date()
                    skills.start_job("scan", lambda: skills.market_scan(s))
                if engine.OPEN_MIN <= m <= engine.CLOSE_MIN + 5:
                    skills.check_alerts(s)
        except Exception:
            traceback.print_exc()
        time.sleep(60)


def skills_state():
    s = learn.load_strategy()
    return {"skills": skills.status(s), "sec_user_agent": s.get("sec_user_agent") or "", "skills_dir": s.get("skills_dir") or "",
            "jobs": {k: skills.job(k) for k in ("scan", "orb", "congress")}, "watchlist": s["watchlist"],
            "rings": skills.RINGS[-30:], "clock": skills.clock(s) if skills.discover(s.get("skills_dir")).get("alerts") else None}


def skill_post(op, body):
    s = learn.load_strategy()
    if op == "check":
        a = analyze(body.get("symbol", "SPY"), news=False)
        res = apply_skill_check(a, s, run_now=True)
        if res is None:
            raise ValueError("Für diesen Ticker gibt es heute keinen Plan, den die Skills prüfen könnten")
        return res
    if op == "scan":
        return skills.start_job("scan", lambda: skills.market_scan(s))
    if op == "orb":
        syms = [x for x in (body.get("symbols") or []) if x] or s["watchlist"]
        return skills.start_job("orb", lambda: skills.orb_scan(s, syms))
    if op == "congress":
        import datetime
        since = (engine.et_now().date() - datetime.timedelta(days=int(body.get("days") or 45))).isoformat()
        return skills.start_job("congress", lambda: skills.congress(s, since, set(s["watchlist"])))
    if op == "analyst":
        return skills.analyst(s, body.get("symbol", ""))
    if op == "alerts":
        return {"log": skills.alerts_for_plan(s, body["symbol"], engine.et_now().date().isoformat(),
                                              entry=body.get("entry"), stop=body.get("stop"), target=body.get("target"))}
    if op == "alert-list":
        return {"text": skills.alert_rules(s)}
    if op == "check-alerts":
        return skills.check_alerts(s)
    raise ValueError("unbekannte Skill-Aktion")


def auto_learner():
    """Learn once per trading day after the close, for every bar length in use."""
    while True:
        try:
            now = engine.et_now()
            if now.weekday() < 5 and engine.minute_of_day(now) >= 16 * 60 + 5:
                for m in learn.intervals_in_use():
                    done = (learn.load_strategy(m).get("learned_at") or "")[:10]
                    if done != now.date().isoformat():
                        with _lock:
                            learn.cycle(minutes=m)
                        print("[phil] Lernzyklus fertig (%d-Min-Kerzen)" % m, now.strftime("%H:%M"), flush=True)
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
        if u.path == "/manifest.webmanifest":
            return self._send(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
        if u.path in ("/icon-180.png", "/icon-512.png"):
            with open(os.path.join(WEB, u.path[1:]), "rb") as f:
                return self._send(200, f.read(), "image/png")
        if u.path == "/api/analyze":
            return self._guard(lambda: analyze(q.get("symbol", "SPY")))
        if u.path == "/api/live":
            return self._guard(lambda: liquid.live_candles(q.get("symbol", "SPY")) or yahoo_live(q.get("symbol", "SPY")))
        if u.path == "/api/scan":
            return self._guard(scan)
        if u.path == "/api/state":
            return self._guard(state)
        if u.path == "/api/trades":
            return self._guard(trades_state)
        if u.path == "/api/skills":
            return self._guard(skills_state)
        if u.path == "/api/skills/job":
            return self._guard(lambda: skills.job(q.get("name", "")))
        if u.path == "/api/chart":
            return self._guard(lambda: chartdata.chart(q.get("symbol", "SPY"), q.get("iv", "5")))
        if u.path == "/api/past-signals":
            return self._guard(lambda: chartdata.past_signals(q.get("symbol", "SPY")))
        if u.path.startswith("/shots/"):
            p = trades.shot_path(u.path[len("/shots/"):])
            if not p:
                return self._send(404, {"error": "not found"})
            ext = p.rsplit(".", 1)[1]
            with open(p, "rb") as f:
                return self._send(200, f.read(), {"jpg": "image/jpeg"}.get(ext, "image/" + ext))
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
        if p == "/api/trades":
            return self._guard(lambda: open_trade(body))
        if p == "/api/read-shot":
            return self._guard(lambda: read_shot(body))
        if p.startswith("/api/skills/"):
            return self._guard(lambda: skill_post(p[len("/api/skills/"):], body))
        if p == "/api/ask":
            return self._guard(lambda: ask(body))
        m = re.match(r"^/api/trades/([A-Za-z0-9_.-]+)/(close|image|rate|review|delete)$", p)
        if m:
            tid, op = m.groups()
            if op == "close":
                return self._guard(lambda: trades.close_trade(tid, body, learn.load_strategy()))
            if op == "image":
                return self._guard(lambda: trades.add_image(tid, body.get("image"), body.get("when", "extra")))
            if op == "rate":
                return self._guard(lambda: trades.rate(tid, body.get("rating"), body.get("note")))
            if op == "review":
                return self._guard(lambda: review(tid))
            return self._guard(lambda: (trades.delete(tid), {"ok": True})[1])
        self._send(404, {"error": "not found"})


def main():
    for stream in (sys.stdout, sys.stderr):  # a Windows service log may not take every character
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
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
    threading.Thread(target=skill_daemon, daemon=True).start()
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
