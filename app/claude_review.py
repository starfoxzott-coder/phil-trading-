"""Optional: Claude reads your trade screenshots and reviews the trade.

Needs the official SDK (`pip install anthropic`) and a key (`ANTHROPIC_API_KEY`,
or a profile from `ant auth login`). Without them the rest of the app works
unchanged; only the two buttons that call this module report what is missing.

  read_screenshot  pulls ticker, entry, stop, target, shares and times out of a
                   broker or chart screenshot, so the trade form fills itself
  review_trade     compares what you did with the app's plan and the result,
                   and writes a short lesson plus mistake tags the journal counts
"""

import base64
import json
import os

import trades

MODEL = "claude-opus-5-5"
MEDIA = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


class ReviewError(Exception):
    pass


NO_KEY = "Kein Anthropic-Schlüssel gefunden: ANTHROPIC_API_KEY setzen (oder `ant auth login`), dann die App neu starten"


def _client():
    try:
        import anthropic
    except ImportError:
        raise ReviewError("Für KI-Funktionen: pip install anthropic, dann die App neu starten")
    if not available():
        raise ReviewError(NO_KEY)
    return anthropic, anthropic.Anthropic()


def _image_blocks(names):
    blocks = []
    for name in names[:4]:
        p = trades.shot_path(name)
        if not p:
            continue
        with open(p, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("utf-8")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": MEDIA[name.rsplit(".", 1)[1]], "data": data}})
    return blocks


def _num_or_null():
    return {"anyOf": [{"type": "number"}, {"type": "null"}]}


def _str_or_null():
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _ask(content, schema, effort):
    anthropic, client = _client()
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        raise ReviewError("Kein gültiger Anthropic-Schlüssel: ANTHROPIC_API_KEY setzen oder `ant auth login`")
    except anthropic.RateLimitError:
        raise ReviewError("Anthropic-Limit erreicht, gleich noch einmal versuchen")
    except anthropic.APIStatusError as e:
        raise ReviewError("Anthropic-API antwortet mit Fehler %s" % e.status_code)
    except anthropic.APIConnectionError:
        raise ReviewError("Keine Verbindung zur Anthropic-API")
    except TypeError as e:
        if "authentication" in str(e):
            raise ReviewError(NO_KEY)
        raise
    if resp.stop_reason == "refusal":
        raise ReviewError("Claude hat die Auswertung abgelehnt")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise ReviewError("Leere Antwort von Claude")
    return json.loads(text)


READ_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": _str_or_null(), "side": {"anyOf": [{"type": "string", "enum": ["long", "short"]}, {"type": "null"}]},
        "entry": _num_or_null(), "shares": _num_or_null(), "stop": _num_or_null(), "target": _num_or_null(),
        "entry_time_et": _str_or_null(), "exit": _num_or_null(), "exit_time_et": _str_or_null(),
        "what_i_see": {"type": "string"},
    },
    "required": ["symbol", "side", "entry", "shares", "stop", "target", "entry_time_et", "exit", "exit_time_et", "what_i_see"],
    "additionalProperties": False,
}


def read_screenshot(image_names, symbol_hint=None):
    blocks = _image_blocks(image_names)
    if not blocks:
        raise ReviewError("Kein Bild gefunden")
    prompt = (
        "Das ist ein Screenshot eines eigenen Aktien-Daytrades (Broker-Order, Positionsansicht oder Chart). "
        "Lies daraus ab: Ticker, Richtung, Kaufkurs, Stückzahl, Stop-Loss, Take-Profit, Kaufzeit und, falls "
        "schon verkauft, Verkaufskurs und Verkaufszeit. Zeiten als HH:MM in New-York-Zeit; ist eine andere Zeitzone "
        "zu sehen, rechne um. Was nicht klar zu erkennen ist, bleibt null; rate nicht. "
        "In what_i_see beschreibst du auf Deutsch in einem Satz, was auf dem Bild zu sehen ist."
        + (" Vermuteter Ticker: %s." % symbol_hint if symbol_hint else "")
    )
    return _ask(blocks + [{"type": "text", "text": prompt}], READ_SCHEMA, "low")


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["good", "mixed", "bad"]},
        "followed_plan": {"type": "boolean"},
        "lesson": {"type": "string"},
        "keep_doing": {"type": "string"},
        "change_next_time": {"type": "string"},
        "mistakes": {"type": "array", "items": {"type": "string", "enum": sorted(trades.MISTAKES)}},
    },
    "required": ["verdict", "followed_plan", "lesson", "keep_doing", "change_next_time", "mistakes"],
    "additionalProperties": False,
}


def review_trade(t):
    facts = {k: t.get(k) for k in ("symbol", "date", "time", "entry", "shares", "stop", "target", "exit", "exit_time",
                                    "exit_reason", "r", "pnl", "rating", "note", "note_after", "auto_mistakes", "mistakes")}
    facts["app_plan"] = t.get("plan")
    facts["app_plan_result"] = t.get("plan_result")
    prompt = (
        "Du bist der Trading-Coach in einer Daytrading-App (US-Aktien, nur Long, Ausbruch über die Eröffnungsspanne, "
        "Stop und Ziel aus dem Plan der App, spätestens 15:55 New York glattstellen). Bewerte diesen abgeschlossenen "
        "Trade des Nutzers. Fakten als JSON (Preise in $, r = Ergebnis in Vielfachen des Risikos, app_plan = was die App "
        "vorgeschlagen hatte, app_plan_result = was dieser Plan am selben Tag gebracht hätte, auto_mistakes = aus den "
        "Zahlen belegte Abweichungen):\n" + json.dumps(facts, ensure_ascii=False) + "\n\n"
        "Beurteile die Entscheidungen, nicht nur das Ergebnis: ein Verlust mit sauber befolgtem Plan ist ein guter "
        "Trade, ein Gewinn trotz Regelbruch nicht. Nutze die Bilder, falls vorhanden. Schreibe lesson in 2 bis 4 kurzen "
        "deutschen Sätzen, keep_doing und change_next_time in je einem Satz. mistakes nur aus der erlaubten Liste und "
        "nur, wenn Fakten oder Bilder sie belegen."
    )
    blocks = _image_blocks([im["name"] for im in t.get("images", [])])
    out = _ask(blocks + [{"type": "text", "text": prompt}], REVIEW_SCHEMA, "medium")
    out["model"] = MODEL
    return out


def ask(turns, images=()):
    """Chat about trades: `turns` alternate user/assistant and end on a user turn.

    The first user turn carries the coaching rules and the page's data (the page
    builds it); images (data URLs, e.g. the chart or a broker screenshot) go with
    the last user turn.
    """
    anthropic, client = _client()
    msgs = []
    src = turns if len(turns) <= 24 else [turns[0]] + turns[-22:]  # keep the rules turn, drop the oldest chat
    for t in src:
        role = "assistant" if t.get("role") == "assistant" else "user"
        text = str(t.get("content") or "").strip()
        if not text:
            continue
        if msgs and msgs[-1]["role"] == role:  # the API wants alternating turns
            msgs[-1]["content"][0]["text"] += "\n\n" + text
        else:
            msgs.append({"role": role, "content": [{"type": "text", "text": text}]})
    if not msgs or msgs[-1]["role"] != "user":
        raise ReviewError("Keine Frage")
    for url in list(images)[:3]:
        m = trades.re.match(r"^data:(image/(?:png|jpeg|webp|gif));base64,(.+)$", url or "", trades.re.S)
        if m:
            msgs[-1]["content"].insert(0, {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            messages=msgs,
        )
    except anthropic.AuthenticationError:
        raise ReviewError("Kein gültiger Anthropic-Schlüssel: ANTHROPIC_API_KEY setzen oder `ant auth login`")
    except anthropic.RateLimitError:
        raise ReviewError("Anthropic-Limit erreicht, gleich noch einmal versuchen")
    except anthropic.APIStatusError as e:
        raise ReviewError("Anthropic-API antwortet mit Fehler %s" % e.status_code)
    except anthropic.APIConnectionError:
        raise ReviewError("Keine Verbindung zur Anthropic-API")
    except TypeError as e:
        if "authentication" in str(e):
            raise ReviewError(NO_KEY)
        raise
    if resp.stop_reason == "refusal":
        raise ReviewError("Claude hat die Frage abgelehnt")
    text = "\n\n".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise ReviewError("Leere Antwort von Claude")
    return {"text": text, "truncated": resp.stop_reason == "max_tokens"}


def available():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.path.isdir(os.path.expanduser("~/.config/anthropic")))
