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

## Echtzeit und Zusatzdaten

- **Live-Kerzen**: Während der Börsenzeit verbindet sich der Browser per WebSocket mit
  Hyperliquid (dort handelt Liquid die Aktien als Perpetuals, z. B. `xyz:TSLA`) und zeichnet
  die gerade laufende 5-Minuten-Kerze hohl, bis sie schließt, plus eine blaue Live-Kurslinie.
  Für SPY und QQQ gibt es keinen 1:1-Perp; dort kommen die Live-Kerzen alle 5 Sekunden aus
  Yahoos 1-Minuten-Daten. Schließt eine Kerze, prüft die App die Entscheidung neu. Der
  Perp-Kurs kann ein paar Cent vom Börsenkurs abweichen; Einstieg, Stop und Ziel rechnet die
  App immer auf den geschlossenen Börsenkerzen.
- **Indikatoren im Chart**: VWAP (gelb) und EMA 20 (grau gestrichelt). Dazu RSI 14 und ATR 14
  im Kasten „Markt-Kontext“. VWAP und EMA sind auch Filter, die die Lernschleife testet
  („nur Ausbrüche über VWAP“).
- **Perp-Daten** (live, öffentlich, ohne Schlüssel): Funding pro Stunde, Open Interest, Prämie
  des Perps gegenüber dem Oracle-Kurs.
- **Liquid-Positionierung**: wie profitable Trader („Smart Money“) und die verlierende Masse
  positioniert sind. Diese Daten gibt es nur über den Liquid-Connector in Claude, nicht
  öffentlich. Claude speichert sie in `data/intel.json` (mit Zeitstempel). Zum Aktualisieren
  sag Claude: „Aktualisiere die Liquid-Positionierung für meine Watchlist“. Die App zeigt
  sie als Hinweis und schreibt sie zu jedem Signal ins Journal. Ob sie wirklich hilft, zeigt
  der Tab „Lernen“ (Ø R für „Profis dafür“, „neutral“ und „Profis dagegen“). Zur harten Regel
  wird sie erst, wenn diese Zahlen das belegen.
- **Quiver Quant** (Insider, Kongress, Dark Pool) ist noch nicht eingebaut: Dein Quiver-Konto
  ist verbunden, hat aber kein aktives Abo, daher liefert es keine Daten.

## Wann ist es wirklich sinnvoll? Stop und Ziel pro Trade

Für jedes Signal rechnet die App **30 Kombinationen** aus Stop und Ziel durch:

- **Stops**: Mitte oder Tief der Eröffnungsspanne, 1 / 1,5 / 2 × ATR unter dem Einstieg, knapp unter VWAP
- **Ziele**: 1R, 1,5R, 2R, 2,5R, 3R (R = Abstand Einstieg–Stop)

Jede Kombination wird auf denselben 2.000 simulierten Verläufen des restlichen Tages **und** auf
allen echten Ausbrüchen dieser Aktie der letzten 60 Tage durchgespielt. Davon gehen die
Handelskosten ab (Standard 3 Basispunkte pro Seite). Es gewinnt die Kombination mit der höchsten
Erwartung, aber nur mit einem Ziel, das in mindestens 15 % der Fälle zuerst erreicht wird. Die
Heatmap „Warum genau dieser Stop und dieses Ziel?“ zeigt alle 30 Zellen.

Gehandelt wird nur, wenn alle **Pflichtpunkte** stimmen:

1. Eine 5-Minuten-Kerze hat über dem Spannenhoch geschlossen, vor höchstens 10 Minuten
2. Erwartung nach Kosten mindestens +0,10R (die Lernschleife erhöht das, wenn die Vorhersagen zu
   optimistisch waren)
3. Deine Risikoregeln sind erfüllt, und der Ticker ist nicht gesperrt

und die **Qualität** mindestens B ist. Die Qualität (A/B/C) zählt Zusatzkriterien: Setup auf
dieser Aktie historisch positiv, Einstieg über VWAP, SPY über VWAP, mindestens 90 Minuten Zeit,
RSI unter 75, Liquid-Profis nicht dagegen. Jedes Kriterium wird zu jedem Signal mitgeschrieben;
im Tab „Lernen“ siehst du später, ob Trades mit erfülltem Kriterium wirklich besser waren.

„So setzt du die Order“ gibt dir die vier Schritte: Kauf-Limit (Signalkurs + 0,1R, nicht
nachjagen), Stop-Market-Verkauf, Take-Profit-Limit und spätestens 15:55 New York alles verkaufen.

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
2. **Bewerten**: Trefferquote, Ø R, ob die vorhergesagte Erwartung gestimmt hat, Ergebnis nach
   Qualität A/B/C und nach jedem Zusatzkriterium.
3. **Regel anpassen**: 96 Varianten (Spanne 15/30 Min, Stop Mitte/Tief, Ziel 1–3R,
   Volumenfilter, Trendfilter VWAP/EMA) laufen über die letzten 60 Tage der Watchlist plus deine Live-Signale.
   Die Regel wechselt nur, wenn eine Variante über mindestens 30 Trades klar besser ist
   (untere Schranke: Ø R minus Standardfehler), damit eine Glückswoche nichts kippt.
   Ticker, auf denen die Regel dauerhaft verliert, werden gesperrt. War die vorhergesagte
   Erwartung zu optimistisch, verlangt die App mehr (Mindest-Erwartung steigt um 0,05R).
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

- `engine.py`: Daten, Einstieg, Checkliste, Positionsgröße, Risiko-Check
- `optimize.py`: Stop und Ziel pro Signal (Simulation + echte Ausbrüche, nach Kosten)
- `learn.py`: Abrechnen, Bewerten, Regel-Evolution, Retros
- `liquid.py`: Hyperliquid-Live-Daten (Kerzen, Funding, Open Interest) und Liquid-Positionierung
- `server.py`: Web-Server und Kommandozeile
- `web/index.html`: Oberfläche mit Chart
- `data/intel.json`: Liquid-Positionierung, von Claude geschrieben
- `data/strategy.json`: aktuelle Regel und deine Limits. Journal und Lernhistorie entstehen
  dort beim Benutzen.

Die App liegt außerhalb von Phils geschütztem Kern (`core/`, `config/`) und ändert nichts an
Phils Polymarket-Loop.
