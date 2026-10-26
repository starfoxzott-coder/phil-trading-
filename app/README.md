# Phil Day-Trade

Eine lokale Web-App, die dir beim Daytrading von US-Aktien sagt, **ob, wann und wo** du
einsteigen sollst, wo **Stop-Loss** und **Take-Profit** liegen, und das als rote/grüne
Positionsbox mit Zeitleiste im Chart zeigt. Sie lernt nach jedem Handelstag aus ihren
eigenen Signalen, so wie Phil (der Rest dieses Repos) aus seinen Polymarket-Wetten lernt.

```bash
python3 app/server.py          # öffnet http://localhost:8787
python3 app/server.py scan     # nur die Watchlist im Terminal
python3 app/server.py learn    # einen Lernzyklus von Hand
```

Nur Python 3.9+ und die Standardbibliothek, keine Installation. Kursdaten: Yahoo Finance
(kostenlos, inoffiziell, kann etwas verzögert sein).

## Was du im Chart siehst

- **Grüne Box**: Einstieg bis Take-Profit. **Rote Box**: Einstieg bis Stop-Loss.
- **Länge der Box** = wie lange die Position maximal läuft: vom Einstieg bis „flat by“
  (Standard 15:55 New York). Die gestrichelte senkrechte Linie zeigt, wann die
  Simulation typischerweise aussteigt.
- **Blaues Band**: die Eröffnungsspanne (erste 15 oder 30 Minuten).
- **Blauer Fächer**: wohin der Kurs in 50 % der simulierten Tage läuft.
- **Zeitleiste unten**: oben New-York-Zeit, darunter deine Zeit; der farbige Balken ist die
  Positionsdauer.

## Was die App entscheidet

| Anzeige | Bedeutung |
|---|---|
| **KAUFEN** | Ausbruch gerade bestätigt (Kerze in den letzten 10 Min geschlossen), Statistik und deine Limits sind ok |
| **LÄUFT** | Gutes Signal, aber schon älter: halten, wenn du drin bist, nicht nachkaufen |
| **WARTEN** | Noch kein Ausbruch; wenn einer kommt, wäre er handelbar. Die Box zeigt den Plan |
| **BEOBACHTEN** | Noch kein Ausbruch, und selbst dann spricht die Statistik dagegen |
| **KEIN TRADE** | Signal da, aber Simulation, Historie oder dein Risiko-Check sagen nein |
| **VORBEI** | Der heutige Trade ist schon durch Stop, Ziel oder Glattstellen beendet |

Die Regeln kommen aus den trader-Skills: Einstieg wie **trader-entry** (erste 5-Min-Kerze, die
über dem Hoch der Eröffnungsspanne *schließt*), Stop/Ziel wie **trader-exit**, die 2.000
Replays wie **trader-timemachine**, Stückzahl wie **trader-budget**, Prüfung wie
**trader-risk**, Schlagzeilen wie **trader-news**. Gekauft wird nur long. Die App platziert
**keine** Orders. Willst du handeln, nimm die Bracket-Order aus „Größe & Risiko-Check“ zu
deinem Broker (oder nutze /trader-broker).

## Wie sie lernt (Phils Zyklus)

Läuft der Server, startet er an jedem Handelstag nach 16:05 New York von selbst einen
Lernzyklus (oder du drückst „Jetzt lernen“):

1. **Abrechnen**: jedes Signal, das die App gut fand, bekommt sein echtes Ergebnis in R.
   Signale werden immer mitgeschrieben, auch wenn du sie nicht handelst.
2. **Bewerten**: Trefferquote, Ø R, und ob die Wahrscheinlichkeiten der Time Machine
   gestimmt haben (Brier-Score, wie `brier_delta` bei Phil).
3. **Regel anpassen**: 48 Varianten (Spanne 15/30 Min, Stop Mitte/Tief, Ziel 1–3R,
   Volumenfilter) laufen über die letzten 60 Tage der Watchlist plus deine Live-Signale.
   Die Regel wechselt nur, wenn eine Variante über mindestens 30 Trades klar besser ist
   (untere Schranke: Ø R minus Standardfehler), damit eine Glückswoche nichts kippt.
   Ticker, auf denen die Regel dauerhaft verliert, werden gesperrt. War die Simulation zu
   optimistisch, verlangt die App mehr Vorteil.
4. **Retro**: jede Änderung landet mit Grund in `data/strategy-history.jsonl` und
   `data/retros.md` und im Tab „Lernen“.

Deine Limits (Kontogröße, max. Verlust pro Trade, Tageslimit, Einstiegsfenster …) stellst du
im Tab „Lernen“ ein. Die Lernschleife fasst sie nie an.

## Ehrlich gesagt

Opening-Range-Breakouts auf großen Aktien sind im Schnitt nahe Break-even. Die App sagt
deshalb oft „KEIN TRADE“; das ist der Sinn eines Filters. 60 Tage 5-Minuten-Daten sind
wenig Historie, und eine Simulation ist keine Garantie: ein Gap kann über den Stop springen.
Erst handeln, wenn das Journal über Wochen positiv ist, und dann klein.

## Dateien

- `engine.py`: Daten, Einstieg, Stop/Ziel, Time Machine, Positionsgröße, Risiko-Check
- `learn.py`: Abrechnen, Bewerten, Regel-Evolution, Retros
- `server.py`: Web-Server und Kommandozeile
- `web/index.html`: Oberfläche mit Chart
- `data/strategy.json`: aktuelle Regel und deine Limits. Journal und Lernhistorie entstehen
  dort beim Benutzen.

Die App liegt außerhalb von Phils geschütztem Kern (`core/`, `config/`) und ändert nichts an
Phils Polymarket-Loop.
