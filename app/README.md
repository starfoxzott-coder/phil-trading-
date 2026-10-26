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

## Kerzenlänge: 1, 2, 5, 15 oder 30 Minuten

Oben in der Kopfzeile stellst du unter „Kerzen“ ein, in welchem Zeitrahmen die App rechnet. Alles
richtet sich danach: Eröffnungsspanne, Einstieg (Kerzenschluss über dem Spannenhoch),
Indikatoren (ATR, EMA, RSI pro Kerze), Simulation, Backtest und die gelernte Regel.

| Kerzen | Historie für Simulation und Lernen | Charakter |
|---|---|---|
| 1 Min | etwa 20 Handelstage (Yahoo hält 1-Minuten-Daten nur rund 30 Tage) | viele, schnelle Signale, enge Stops; Spread zählt mehr |
| 2 Min | etwa 30 Handelstage | schneller als 5 Min |
| 5 Min | 60 Tage | Standard, guter Kompromiss |
| 15 Min | 60 Tage | ruhiger, weniger Fehlausbrüche, weniger Signale |
| 30 Min | 60 Tage | sehr ruhig; die Spanne ist mindestens 30 Minuten lang |

- Die Eröffnungsspanne wird auf ganze Kerzen aufgerundet (15 Min bei 2-Min-Kerzen = 16 Min,
  bei 30-Min-Kerzen = 30 Min).
- Ein Ausbruch gilt zwei Kerzen lang als frisch (mindestens 5, höchstens 30 Minuten), danach
  heißt es LÄUFT.
- **Jede Kerzenlänge lernt getrennt**: eigene Regel, eigene Mindest-Erwartung, eigene gesperrte
  Ticker, eigenes Signal-Journal. Abends lernt die App für jede Kerzenlänge, die du benutzt hast.
- Deine eigenen Trades merken sich, mit welcher Kerzenlänge du gehandelt hast. Ein offen
  gelassener Trade wird mit 1-Minuten-Daten abgerechnet, solange Yahoo sie noch hat.

## Interaktives Chart-Fenster

Der Knopf **„⤢ Interaktives Chart“** oben rechts im Chart öffnet ein großes Fenster:

- **Chart-Kerzen**: 1, 2, 5, 15, 30 Minuten, 1 Stunde oder Tag. Das ist nur die Ansicht; sie
  wird im Browser gemerkt.
- **Vorschläge**: die Kerzenlänge, auf der die App ihre Signale, Stops und Ziele berechnet
  (dieselbe Einstellung wie oben in der Kopfzeile). Beide sind unabhängig: Du kannst dir
  Tageskerzen ansehen und trotzdem auf 5-Minuten-Kerzen handeln.
- **Zeitraum**: 1 / 5 / 20 Tage oder alles (bei Tageskerzen 3 Monate bis 2 Jahre). Weiter
  zurück: mit dem Mausrad zoomen (Handy: zwei Finger), ziehen zum Verschieben, Pfeiltasten,
  Doppelklick setzt zurück. Historie: 1 Min etwa 20 Tage, 2 Min etwa 30, 5 bis 30 Min 60 Tage,
  1 Stunde und Tag 2 Jahre.
- **Vergangene Signale**: jeder frühere Tag, an dem die aktuelle Regel (für die eingestellte
  Vorschlags-Kerzenlänge) gekauft hätte, als kleine grün-rote Box mit Ergebnis in R. Bei
  Tageskerzen als Dreieck unter der Kerze. Darüber fahren zeigt Kauf, Stop, Ziel und Ausgang.
  Diese Boxen nutzen den Stop und das Ziel der Regel; die Feinabstimmung pro Trade läuft nur für
  heute.
- Dazu der heutige Plan, deine eigenen Trades, VWAP und EMA 20 (einzeln abschaltbar), die
  laufende Kerze live und ein Fadenkreuz mit Kurs und Uhrzeit.

## Echtzeit und Zusatzdaten

- **Live-Kerzen**: Während der Börsenzeit verbindet sich der Browser per WebSocket mit
  Hyperliquid (dort handelt Liquid die Aktien als Perpetuals, z. B. `xyz:TSLA`) und zeichnet
  die gerade laufende Kerze hohl, bis sie schließt, plus eine blaue Live-Kurslinie.
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

1. Eine Kerze hat über dem Spannenhoch geschlossen, vor höchstens zwei Kerzen (5 bis 30 Minuten)
2. Erwartung nach Kosten mindestens +0,10R (die Lernschleife erhöht das, wenn die Vorhersagen zu
   optimistisch waren)
3. Deine Risikoregeln sind erfüllt, und der Ticker ist nicht gesperrt

und die **Qualität** mindestens B ist. Die Qualität (A/B/C) zählt Zusatzkriterien: Setup auf
dieser Aktie historisch positiv, Einstieg über VWAP, SPY über VWAP, mindestens 90 Minuten Zeit,
RSI unter 75, Liquid-Profis nicht dagegen. Jedes Kriterium wird zu jedem Signal mitgeschrieben;
im Tab „Lernen“ siehst du später, ob Trades mit erfülltem Kriterium wirklich besser waren.

„So setzt du die Order“ gibt dir die vier Schritte: Kauf-Limit (Signalkurs + 0,1R, nicht
nachjagen), Stop-Market-Verkauf, Take-Profit-Limit und spätestens 15:55 New York alles verkaufen.

## Deine eigenen Trades: eintragen, bewerten, daraus lernen

- **„Ich habe diesen Trade gemacht“** (im Kasten mit der Entscheidung): öffnet ein Formular,
  vorausgefüllt mit dem Plan. Trag ein, was du wirklich gemacht hast: Kaufkurs, Stück, Stop,
  Ziel, Uhrzeit, warum. Dazu ein **Foto oder Screenshot** (Datei wählen, am Handy die Kamera,
  am PC mit Strg+V einfügen). Die App speichert ihren Plan **zum Zeitpunkt deines Kaufs** und
  berechnet ihn nur aus den Kerzen, die da schon fertig waren.
- **Tab „Meine Trades“**: Nach dem Verkauf „Trade abschließen“ (Verkaufskurs, Uhrzeit, wie raus)
  und sagen, ob er **gut oder schlecht** war. Vergessen? „Automatisch aus Kursdaten“ rechnet
  ihn mit deinem Stop und Ziel ab. Trades vom Vortag, die noch offen sind, rechnet die
  Lernschleife abends so ab (als „geschätzt“ markiert).
- **Was die App selbst erkennt**, mit Beleg aus den Zahlen: gekauft, obwohl die App abgeraten
  hat; über dem Kauf-Limit gekauft (nachgejagt); vor dem Kerzenschluss gekauft; Stop weiter als
  im Plan; zu früh verkauft (der Plan hätte mehr gebracht); über die Glattstellzeit gehalten;
  Position größer als dein Verlustlimit. Dazu kommen deine eigenen Angaben (Stop verschoben,
  aus Angst etwas zu verpassen, Verlust zurückholen wollen).
- **Deine Bilanz**: du gegen den Plan der App auf denselben Trades, Ergebnis mit befolgtem gegen
  gebrochenen Plan, Fehler nach Häufigkeit und Kosten.
- **Persönliche Regeln**: Ein Fehler, der mindestens 3-mal vorkam und dich Geld gekostet hat,
  wird zur Regel. Sie steht dann bei jeder Order unter „So setzt du die Order“ und im
  abendlichen Retro.
- **„Bild von Claude auslesen“ und „Von Claude auswerten“** (optional): Claude liest Kurs, Stop,
  Ziel und Zeiten aus deinem Screenshot ins Formular und schreibt nach dem Abschluss eine kurze
  Lektion (gut / gemischt / schlecht, was beibehalten, was ändern). Dafür
  `pip install anthropic` und einen Anthropic-API-Schlüssel (`ANTHROPIC_API_KEY`) setzen. Das
  kostet pro Auswertung ein paar Cent (Modell Claude Opus 5.5). Ohne Schlüssel bleiben die
  zwei Knöpfe grau, alles andere funktioniert.
- Deine Trades (`data/my-trades.jsonl`) und Bilder (`data/shots/`) bleiben auf deinem Rechner
  und werden nicht in git eingecheckt.
- **Vom Handy aus** (Foto direkt vom Broker-Bildschirm): `python3 app/server.py --host 0.0.0.0`
  und am Handy im selben WLAN `http://<IP deines PCs>:8787` öffnen. Dann ist die App für alle
  im WLAN erreichbar, also nur im eigenen Netz so starten.

## Was die App entscheidet

| Anzeige | Bedeutung |
|---|---|
| **KAUFEN** | Ausbruch gerade bestätigt (Kerze in den letzten 10 Min geschlossen), Statistik und deine Limits sind ok |
| **LÄUFT** | Gutes Signal, aber schon älter: halten, wenn du drin bist, nicht nachkaufen |
| **WARTEN** | Noch kein Ausbruch; wenn einer kommt, wäre er handelbar. Die Box zeigt den Plan |
| **BEOBACHTEN** | Noch kein Ausbruch, und selbst dann spricht die Statistik dagegen |
| **KEIN TRADE** | Signal da, aber Simulation, Historie oder dein Risiko-Check sagen nein |
| **VORBEI** | Der heutige Trade ist schon durch Stop, Ziel oder Glattstellen beendet |

Die Regeln kommen aus den trader-Skills: Einstieg wie **trader-entry** (erste Kerze, die
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
deshalb oft „KEIN TRADE“; das ist der Sinn eines Filters. 60 Tage Kursdaten (bei 1-Minuten-Kerzen nur etwa 20) sind
wenig Historie, und eine Simulation ist keine Garantie: ein Gap kann über den Stop springen.
Erst handeln, wenn das Journal über Wochen positiv ist, und dann klein.

## Dateien

- `engine.py`: Daten, Einstieg, Checkliste, Positionsgröße, Risiko-Check
- `optimize.py`: Stop und Ziel pro Signal (Simulation + echte Ausbrüche, nach Kosten)
- `learn.py`: Abrechnen, Bewerten, Regel-Evolution, Retros
- `liquid.py`: Hyperliquid-Live-Daten (Kerzen, Funding, Open Interest) und Liquid-Positionierung
- `trades.py`: deine eigenen Trades, Bilder, erkannte Fehler, persönliche Regeln
- `claude_review.py`: optionale Bildauswertung und Trade-Bewertung durch Claude
- `chartdata.py`: Daten für das Chart-Fenster (lange Historie, vergangene Signale)
- `server.py`: Web-Server und Kommandozeile
- `web/index.html`: Oberfläche mit Chart
- `data/intel.json`: Liquid-Positionierung, von Claude geschrieben
- `data/strategy.json`: aktuelle Regel und deine Limits. Journal und Lernhistorie entstehen
  dort beim Benutzen.

Die App liegt außerhalb von Phils geschütztem Kern (`core/`, `config/`) und ändert nichts an
Phils Polymarket-Loop.
