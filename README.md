# instagram_insights

Holt woechentlich die Instagram-Kennzahlen von @mickihde.de ueber die Graph API
und legt sie als Dateien im Repo ab. Der Monatsreport liest diese Dateien.

- `data/account_daily.csv` - Verlauf: Follower, Beitragszahl, Reichweite (28 Tage)
- `data/media.json` - Beitraege mit Reichweite, Likes, Kommentare, Saves, Shares
- `data/last_run.json` - Status und Fehler des letzten Laufs

## Einrichtung
1. GitHub Secret `IG_TOKEN` anlegen (Systemnutzer-Token, laeuft nie ab).
2. Actions -> "Instagram Insights" -> Run workflow (Test).
3. `data/last_run.json` pruefen: `ok: true` oder die Fehlermeldungen.

Das Token niemals in Dateien, Issues oder Chats ablegen.


## Auto-Posting (Warteschlange)

Dasselbe Repo veroeffentlicht Instagram-Posts automatisch zur geplanten Uhrzeit.
Eine GitHub Action (`publish.yml`) laeuft alle 15 Minuten und postet, was in `queue/` faellig ist.

- `queue/<id>/post.json` + `01.jpg ...` - Post mit Typ, Uhrzeit, Caption, Thema, Zielgruppe
- `posted/<id>.json` - Status und Instagram-Media-ID (verknuepfbar mit `data/media.json`)
- `scripts/enqueue.py` - legt einen Post ab (PNG -> JPEG, schreibt post.json)
- `scripts/publish.py` - veroeffentlicht Faelliges, mit Doppelpost-Schutz
- `tests/smoke_test.py` - Test gegen einen simulierten Server, ohne echten Account

```
python scripts/enqueue.py --id 2026-11-18_website --type carousel \
    --at "2026-11-18 09:00" --topic "Thema." --audience website \
    --caption-file caption.txt slide1.png slide2.png ...
```

Zeiten sind Berliner Zeit. Typen: `carousel` (2-10 Bilder), `image`, `story` (jedes Bild eine Story).
Das Repo muss public bleiben (Instagram holt die Bilder per raw.githubusercontent.com).

Einrichtung: Das Secret `IG_TOKEN` braucht zusaetzlich die Berechtigung `instagram_content_publish`.
Test: Actions -> "Publish Instagram" -> Run workflow (zuerst mit `dry_run`).

Sicherheitsnetze: Statusfile plus Caption-Abgleich gegen Doppelposts; Posts, die mehr als 6 Stunden
ueberfaellig sind, werden nicht mehr gepostet (roter Lauf = Mail von GitHub); Bilder werden vorab
geprueft; technische Fehler werden bis zu 3 Mal wiederholt. Nach 14 Tagen werden nur die Bilder
geloescht, `post.json` und Status bleiben fuer die Auswertung.
