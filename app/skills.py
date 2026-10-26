"""Bridge to the eleven trader-* skills: find them on disk, run them, read their JSON.

The skills are standalone Python programs (standard library only). The app runs
them as child processes with their own command-line flags, in a work folder of
its own (data/skillwork/), so their trader/*.json files never mix with yours:

  trader-scanner      whole-market daily setups, opening-range breakouts
  trader-entry        is the breakout confirmed?            \\
  trader-exit         the plan with the app's stop/target    |  the per-signal
  trader-timemachine  10,000-style replays of that plan      |  "skill check":
  trader-budget       shares and the bracket order           |  entry -> exit ->
  trader-risk         PASS or BLOCK against your limits      |  replay -> size ->
  trader-broker       the order ticket (never places)       /   risk -> ticket
  trader-alerts       stop / target / breakout / flat-by alarms and the NYSE clock
  trader-news         Yahoo headlines and SEC 8-K filings, only what is new
  trader-analyst      reported quarters from SEC EDGAR
  trader-pelosi       House members' periodic transaction reports

Where the skills are: PHIL_SKILLS_DIR (one or more folders, separated by the
OS path separator), the "skills_dir" setting, then ~/.claude/skills (Claude
Code keeps synced skills there), ~/.claude/plugins and app/skills.

Arguments always go as a list (no shell), tickers are validated first.
"""

import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "data", "skillwork")
SCRIPTS = {"scanner": "scan.py", "entry": "entry.py", "exit": "exit.py", "timemachine": "timemachine.py",
           "budget": "size.py", "risk": "check.py", "broker": "ticket.py", "alerts": "alerts.py",
           "news": "news.py", "analyst": "facts.py", "pelosi": "ptr.py"}
LABELS = {"scanner": "Markt-Scanner", "entry": "Einstieg", "exit": "Stop & Ziel", "timemachine": "Time Machine",
          "budget": "Stückzahl", "risk": "Risiko-Check", "broker": "Order-Ticket", "alerts": "Alarme",
          "news": "News & SEC-Meldungen", "analyst": "Quartalszahlen", "pelosi": "Kongress-Trades"}
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_found = {"at": 0, "map": {}}
_lock = threading.Lock()
# The skills hand files to each other in one folder (entry.json -> plan.json -> orders.json ...):
# one signal check at a time, and one alerts command at a time.
_check_lock = threading.Lock()
_alerts_lock = threading.Lock()


class SkillError(Exception):
    pass


def roots(extra=None):
    out = []
    for p in (os.environ.get("PHIL_SKILLS_DIR") or "").split(os.pathsep) + [extra or ""]:
        if p.strip():
            out.append(os.path.expanduser(p.strip()))
    home = os.path.expanduser("~")
    out += [os.path.join(home, ".claude", "skills"), os.path.join(home, ".claude", "plugins"), os.path.join(HERE, "skills")]
    return out


def discover(extra=None, fresh=False):
    """{skill: path to its script} for every trader-* skill found."""
    with _lock:
        if not fresh and time.time() - _found["at"] < 60 and _found.get("extra") == extra:
            return dict(_found["map"])
        found = {}
        for root in roots(extra):
            if not os.path.isdir(root):
                continue
            for name, script in SCRIPTS.items():
                if name in found:
                    continue
                hits = sorted(glob.glob(os.path.join(root, "**", "trader-" + name, script), recursive=True))
                if hits:
                    found[name] = hits[0]
        _found.update(at=time.time(), map=found, extra=extra)
        return dict(found)


def status(strategy):
    found = discover(strategy.get("skills_dir"))
    return [{"skill": "trader-" + k, "key": k, "label": LABELS[k], "found": k in found, "path": found.get(k)}
            for k in SCRIPTS]


def symbol(s):
    s = str(s or "").strip().upper()
    if not SYMBOL.match(s):
        raise SkillError("Ungültiger Ticker: %r" % s)
    return s


def sync_config(strategy):
    """Write the skills' trader/config.json from the app's own limits."""
    acct, rules = strategy["account"], strategy["rules"]
    cfg = {
        "budget": acct["account_usd"], "cash_buffer_pct": acct["cash_buffer_pct"],
        "max_position_pct": acct["position_cap_pct"], "max_order_usd": acct["max_order_usd"], "min_order_usd": 5,
        "max_loss_per_trade_usd": acct["max_loss_per_trade_usd"], "max_daily_loss_usd": acct["daily_loss_limit_usd"],
        "max_trades_per_day": int(acct["max_trades_per_day"]), "max_orders_per_day": 10,
        "min_reward_risk": rules["min_reward_risk"], "require_stop": True,
        "no_entries_before": rules["no_entries_before"], "no_entries_after": rules["no_entries_after"],
        "flat_by": rules["flat_by"], "fractional": False, "broker": "alpaca", "paper": True, "blocked": [],
        "allow_options": False, "allow_margin": False, "allow_short": False, "pdt_guard": False,
    }
    ua = (strategy.get("sec_user_agent") or "").strip()
    if ua:
        cfg["sec_user_agent"] = ua
    os.makedirs(os.path.join(WORK, "trader", "state"), exist_ok=True)
    path = os.path.join(WORK, "trader", "config.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    return cfg


def run(strategy, name, args, timeout=180, as_json=True):
    """Run one skill. Returns {"ok", "data" (parsed JSON) or "text", "error", "seconds"}."""
    path = discover(strategy.get("skills_dir")).get(name)
    if not path:
        return {"ok": False, "missing": True, "error": "Skill trader-%s nicht gefunden (siehe Tab Markt → Skills)" % name}
    sync_config(strategy)
    env = dict(os.environ)
    ua = (strategy.get("sec_user_agent") or "").strip()
    if ua:
        env["SEC_USER_AGENT"] = ua
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, path] + [str(a) for a in args], cwd=WORK, env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "trader-%s brauchte länger als %d s" % (name, timeout)}
    out = {"ok": p.returncode == 0, "code": p.returncode, "seconds": round(time.time() - t0, 1),
           "stderr": (p.stderr or "")[-1500:]}
    text = (p.stdout or "").strip()
    if as_json:
        try:
            out["data"] = json.loads(text) if text else None
            out["ok"] = out["data"] is not None  # many skills exit 1 for "nothing triggered" but still answer
        except ValueError:
            out["text"] = text[-6000:]
    else:
        out["text"] = text[-12000:]
        out["ok"] = bool(text) or p.returncode == 0
    if not out["ok"] and not out.get("error"):
        out["error"] = (p.stderr or text or "trader-%s ist fehlgeschlagen" % name).strip().splitlines()[-1][:300]
    return out


def read_work(rel):
    try:
        with open(os.path.join(WORK, rel)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- per-signal check

def _ampm(ts):
    import engine
    t = engine.to_et(ts)
    return t.strftime("%I:%M %p").lstrip("0")


def signal_check(a, strategy):
    """Run entry -> exit -> time machine -> budget -> risk -> broker on the app's plan for one ticker."""
    with _check_lock:
        return _signal_check(a, strategy)


def _signal_check(a, strategy):
    import engine
    p = a.get("plan")
    if not p or not a.get("opening_range"):
        return None
    sym = symbol(a["symbol"])
    orr = a["opening_range"]
    iv = "1m" if a.get("bar_minutes") == 1 else "5m"
    om = str(max(5, int(a["params"]["or_minutes"])))
    steps = {}
    steps["entry"] = run(strategy, "entry", ["check", sym, "--or-minutes", om, "--interval", iv, "--json", "--write"])
    exit_args = ["plan", "--symbol", sym, "--entry", "%.2f" % p["entry"], "--or-high", "%.2f" % orr["high"],
                 "--or-low", "%.2f" % orr["low"], "--or-minutes", om, "--date", a["date"], "--time", _ampm(p["entry_t"]),
                 "--interval", iv, "--stop", "price:%.2f" % p["stop"], "--target", "price:%.2f" % p["target"],
                 "--flat-by", strategy["rules"]["flat_by"], "--json", "--write"]
    steps["exit"] = run(strategy, "exit", exit_args)
    if steps["exit"].get("ok"):
        steps["timemachine"] = run(strategy, "timemachine", ["run", "--plan", "trader/plan.json", "--until",
                                                             strategy["rules"]["flat_by"], "--json"], timeout=240)
        buy_limit = (a.get("order") or {}).get("buy_limit") or p["entry"]
        steps["budget"] = run(strategy, "budget", ["--plan", "trader/plan.json", "--limit", "%.2f" % buy_limit, "--write", "--json"])
        if steps["budget"].get("ok"):
            steps["risk"] = run(strategy, "risk", ["--json"])
            steps["broker"] = run(strategy, "broker", ["show", "--json"])
    return summarize_check(steps, engine)


def summarize_check(steps, engine):
    """Keep the parts the page shows; the full JSON stays in `raw`."""
    s = {"at": engine.et_now().strftime("%H:%M"), "raw": {k: v.get("data") for k, v in steps.items()}, "steps": []}
    def add(key, ok, line, detail=None):
        s["steps"].append({"key": key, "label": LABELS[key], "ok": ok, "line": line, "detail": detail,
                           "error": (steps.get(key) or {}).get("error")})
    e = (steps.get("entry") or {}).get("data")
    if e:
        add("entry", e.get("status") == "triggered", {"triggered": "Ausbruch bestätigt", "waiting": "wartet", "expired": "abgelaufen"}.get(e.get("status"), e.get("status")),
            e.get("reason"))
    elif "entry" in steps:
        add("entry", None, "keine Antwort")
    x = (steps.get("exit") or {}).get("data")
    if x:
        add("exit", not x.get("warnings"), "Stop %.2f · Ziel %.2f · %sR" % (x["stop"], x["target"], x.get("target_r")), "; ".join(x.get("warnings") or []) or x.get("note"))
    elif "exit" in steps:
        add("exit", False, "Plan abgelehnt", (steps["exit"].get("error") or "")[:300])
    t = (steps.get("timemachine") or {}).get("data")
    if t:
        mc, h = t.get("mc") or {}, t.get("history") or {}
        s["timemachine"] = {"replays": mc.get("replays"), "p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"),
                            "p_neither": mc.get("p_neither"), "avg_r": mc.get("avg_R"), "hist_triggered": h.get("triggered"),
                            "hist_avg_r": h.get("avg_R"), "verdict": t.get("verdict")}
        add("timemachine", (mc.get("avg_R") or 0) > 0,
            "Ziel zuerst %.0f %% · Stop zuerst %.0f %% · Ø %+.2fR (%s Replays)" % ((mc.get("p_target") or 0) * 100, (mc.get("p_stop") or 0) * 100,
                                                                         mc.get("avg_R") or 0, mc.get("replays")), t.get("verdict"))
    b = (steps.get("budget") or {}).get("data")
    if b:
        z = b.get("sizing") or {}
        add("budget", (z.get("qty") or 0) > 0 and not z.get("refusal"),
            "%s Stück · max. Verlust $%s · begrenzt durch %s" % (z.get("qty"), z.get("max_loss"), z.get("bound_by")), z.get("refusal"))
    r = (steps.get("risk") or {}).get("data")
    if r and r.get("results"):
        v = r["results"][0]
        s["risk_verdict"] = v.get("verdict")
        s["risk_reasons"] = v.get("reasons") or []
        add("risk", v.get("verdict") == "PASS", v.get("verdict"), "; ".join(v.get("reasons") or v.get("info") or []))
    k = (steps.get("broker") or {}).get("data")
    if k and isinstance(k, list) and k:
        s["ticket"] = {kk: k[0].get(kk) for kk in ("ready", "status", "plain", "tool", "tool_input", "max_loss", "paper")}
        add("broker", bool(k[0].get("ready")), k[0].get("plain", "")[:220], k[0].get("status"))
    return s


# ---------------------------------------------------------------- alerts

def alerts_for_plan(strategy, sym, date, entry=None, stop=None, target=None, tag="plan"):
    with _alerts_lock:
        return _alerts_for_plan(strategy, sym, date, entry, stop, target, tag)


def _alerts_for_plan(strategy, sym, date, entry, stop, target, tag):
    sym = symbol(sym)
    done = []
    base = "%s-%s-%s" % (sym.lower(), tag, date)
    if entry:
        done.append(run(strategy, "alerts", ["add", "--symbol", sym, "--above", "%.2f" % entry, "--date", date,
                                             "--note", "Ausbruch: Kurs über Einstieg", "--id", base + "-entry"], as_json=False))
    if stop:
        done.append(run(strategy, "alerts", ["add", "--symbol", sym, "--stop", "%.2f" % stop, "--date", date,
                                             "--note", "Stop erreicht", "--id", base + "-stop"], as_json=False))
    if target:
        done.append(run(strategy, "alerts", ["add", "--symbol", sym, "--target", "%.2f" % target, "--date", date,
                                             "--note", "Ziel erreicht", "--id", base + "-target"], as_json=False))
    done.append(run(strategy, "alerts", ["add", "--at", strategy["rules"]["flat_by"], "--label", "Glattstellen: alles verkaufen",
                                         "--before-close", "--date", date, "--id", "flat-" + date], as_json=False))
    return [d.get("text") or d.get("error") for d in done]


# ---------------------------------------------------------------- market-wide

def market_scan(strategy):
    """trader-scanner daily over every US common stock (the universe is rebuilt once a day)."""
    uni = os.path.join(WORK, "trader", "universe.txt")
    if not os.path.exists(uni) or time.time() - os.path.getmtime(uni) > 20 * 3600:
        u = run(strategy, "scanner", ["universe"], timeout=120, as_json=False)
        if not os.path.exists(uni):
            raise SkillError(u.get("error") or "Universum konnte nicht geladen werden")
    r = run(strategy, "scanner", ["daily", "--json", "--quiet"], timeout=900)
    if not r.get("ok"):
        raise SkillError(r.get("error") or "Scan fehlgeschlagen")
    return r["data"]


def orb_scan(strategy, symbols):
    syms = ",".join(symbol(x) for x in symbols)[:4000]
    r = run(strategy, "scanner", ["orb", "--symbols", syms, "--json", "--quiet"], timeout=900)
    if not r.get("ok"):
        raise SkillError(r.get("error") or "Ausbruch-Scan fehlgeschlagen")
    return r["data"]


def congress(strategy, since, watch=(), limit=40):
    """Recent House PTRs, each read into rows (needs pdftotext for the rows)."""
    r = run(strategy, "pelosi", ["index", "--since", since, "--json"], timeout=120)
    if not r.get("ok"):
        raise SkillError(r.get("error") or "Kongress-Index nicht lesbar")
    filings = r["data"] or []
    rows, unread = [], 0
    for f in filings[:limit]:
        x = run(strategy, "pelosi", ["fetch", f["doc_id"], "--json"] + (["--filed", f["filed"]] if f.get("filed") else []), timeout=90)
        d = x.get("data") if x.get("ok") else None
        if not d or not d.get("rows"):
            unread += 1
            continue
        for row in d["rows"]:
            rows.append(dict(row, member=d.get("name") or f.get("name"), filed=f.get("filed"), url=f.get("url"),
                             watched=bool(row.get("ticker") and row["ticker"].upper() in watch)))
    return {"since": since, "filings": len(filings), "read": min(len(filings), limit) - unread, "unread": unread,
            "rows": rows, "filing_list": filings[:80]}


def news(strategy, sym, hours=72):
    args = ["--tickers", symbol(sym), "--json", "--hours", str(hours), "--all", "--dry-run"]
    if not (strategy.get("sec_user_agent") or "").strip():
        args.append("--no-sec")
    return run(strategy, "news", args, timeout=60)


def analyst(strategy, sym):
    if not (strategy.get("sec_user_agent") or "").strip():
        raise SkillError("Für Quartalszahlen verlangt die SEC deinen Namen und deine E-Mail (Tab Markt → Einstellungen)")
    r = run(strategy, "analyst", [symbol(sym), "--quarters", "6"], timeout=120, as_json=False)
    if not r.get("ok"):
        raise SkillError(r.get("error") or "Quartalszahlen nicht lesbar")
    return {"symbol": sym, "text": r["text"]}


def clock(strategy):
    r = run(strategy, "alerts", ["clock", "--json"], timeout=30)
    return r.get("data")


RINGS = []


def check_alerts(strategy):
    """One trader-alerts check; new rings are kept for the page (last 200)."""
    with _alerts_lock:
        r = run(strategy, "alerts", ["check", "--json"], timeout=90)
    d = r.get("data") or {}
    now = time.time()
    for ring in d.get("rings") or []:
        RINGS.append(dict(ring, at=now))
    del RINGS[:-200]
    return d


def alert_rules(strategy):
    r = run(strategy, "alerts", ["list"], timeout=30, as_json=False)
    return r.get("text") or r.get("error") or ""


# ---------------------------------------------------------------- background jobs

JOBS = {}


def start_job(name, fn):
    """Run fn() in a thread once at a time; its result lands in JOBS[name]."""
    j = JOBS.get(name)
    if j and j["state"] == "running":
        return j
    JOBS[name] = j = {"state": "running", "started": time.time(), "result": None, "error": None}

    def go():
        try:
            j["result"] = fn()
            j["state"] = "done"
        except Exception as e:  # surfaced to the page as the job's error
            j["error"], j["state"] = str(e)[:300], "failed"
        j["finished"] = time.time()
        if j["state"] == "done":  # keep the last good result across restarts
            os.makedirs(os.path.join(WORK, "jobs"), exist_ok=True)
            tmp = os.path.join(WORK, "jobs", name + ".json.tmp")
            with open(tmp, "w") as f:
                json.dump(j, f)
            os.replace(tmp, os.path.join(WORK, "jobs", name + ".json"))

    threading.Thread(target=go, daemon=True).start()
    return j


def job(name):
    j = JOBS.get(name)
    if not j and re.match(r"^[a-z]+$", name or ""):
        try:
            with open(os.path.join(WORK, "jobs", name + ".json")) as f:
                j = JOBS[name] = json.load(f)
        except (OSError, ValueError):
            j = None
    if not j:
        return {"state": "idle"}
    return dict(j, elapsed=round((j.get("finished") or time.time()) - j["started"]))
