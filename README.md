# instagram_insights

Holt woechentlich die Instagram-Kennzahlen der Accounts ueber die Graph API und legt sie als
Dateien im Repo ab, und veroeffentlicht geplante Posts (Bilder, Karussells, Stories, Reels).
Beides laeuft fuer mehrere Accounts, die in `accounts.json` stehen. Der Monatsreport liest die Dateien.

## Accounts

`accounts.json` listet pro Account `name`, `ig_user_id` und `token_secret` (der NAME der Umgebungsvariable /
des GitHub Secrets, nie das Token selbst):

| name       | Instagram            | Secret              |
|------------|----------------------|---------------------|
| `agentur`  | @mickihde.de         | `IG_TOKEN`          |
| `safeslot` | SafeSlot (SaaS)      | `IG_TOKEN_SAFESLOT` |

`agentur` hat `"mirror_data_to_root": true`: seine Insights liegen zusaetzlich am alten Pfad `data/`
(den der Monatsreport liest).

### Zweiten Account einrichten (SafeSlot)
1. In Meta (Business-Portfolio) einen Systemnutzer-Token fuer das SafeSlot-Instagram erzeugen. Er braucht
   `instagram_basic`, `instagram_manage_insights` und fuer das Posten zusaetzlich `instagram_content_publish`
   (plus `pages_show_list`/`pages_read_engagement`, wie beim ersten Account).
2. GitHub: Settings -> Secrets and variables -> Actions -> New repository secret: `IG_TOKEN_SAFESLOT`.
3. Die Instagram-User-ID von SafeSlot (Graph API Explorer: `me/accounts?fields=instagram_business_account`)
   in `accounts.json` bei `safeslot` unter `ig_user_id` eintragen und committen.
4. Test: Actions -> "Publish Instagram" -> Run workflow, `account` = `safeslot`, `dry_run` = an.
   Danach "Instagram Insights" einmal manuell starten und `data/safeslot/last_run.json` pruefen.

Fehlt das Token (oder die `ig_user_id`) eines Accounts, wird nur dieser Account uebersprungen und als
gelbe Warnung geloggt (`UEBERSPRUNGEN`); die anderen laufen weiter. Warten beim uebersprungenen Account
offene Posts in der Queue, wird der Lauf rot (Mail von GitHub), damit nichts unbemerkt liegen bleibt.
Ein weiterer Account: Eintrag in `accounts.json` plus eine Zeile `NAME: ${{ secrets.NAME }}` in
`publish.yml` und `insights.yml` (GitHub Secrets lassen sich nicht dynamisch auslesen).

Das Token niemals in Dateien, Issues oder Chats ablegen.

## Insights (insights.yml)

Je Account in `data/<account>/`:
- `account_daily.csv` - Verlauf: Follower, Beitragszahl, Reichweite (28 Tage)
- `media.json` - Beitraege mit Reichweite, Likes, Kommentare, Saves, Shares; bei Reels zusaetzlich
  `views` (Aufrufe), `ig_reels_avg_watch_time` und `ig_reels_video_view_total_time` (Wiedergabezeit),
  soweit die `GRAPH_VERSION` sie liefert (sonst Rueckfall auf weniger Metriken, kein Abbruch)
- `last_run.json` - Status und Fehler des letzten Laufs

Einrichtung: Secrets wie oben, dann Actions -> "Instagram Insights" -> Run workflow (Test) und
`data/<account>/last_run.json` pruefen: `ok: true` oder die Fehlermeldungen.


## Auto-Posting (Warteschlange)

Eine GitHub Action (`publish.yml`) laeuft alle 15 Minuten und postet, was in `queue/` faellig ist.

- `queue/<account>/<id>/post.json` + Medien - Post mit Typ, Uhrzeit, Caption, Thema, Zielgruppe
- `posted/<account>/<id>.json` - Status und Instagram-Media-ID (verknuepfbar mit `data/<account>/media.json`)
- `scripts/enqueue.py` - legt einen Post ab (`--account` ist Pflicht; PNG -> JPEG, schreibt post.json)
- `scripts/publish.py` - veroeffentlicht Faelliges, mit Doppelpost-Schutz, je Account
- `scripts/migrate_to_accounts.py` - verschiebt Eintraege im alten Layout (`queue/<id>`) nach `queue/agentur/`
- `tests/smoke_test.py` - Test gegen einen simulierten Server, ohne echten Account

```
python scripts/enqueue.py --account agentur --id 2026-11-18_website --type carousel \
    --at "2026-11-18 09:00" --topic "Thema." --audience website \
    --caption-file caption.txt slide1.png slide2.png ...

python scripts/enqueue.py --account safeslot --id 2026-12-02_reel --type reel \
    --at "2026-12-02 12:00" --caption-file caption.txt --cover cover.png reel.mp4
```

Zeiten sind Berliner Zeit. Typen: `carousel` (2-10 Bilder), `image`, `story` (jedes Bild eine Story), `reel`.
Das Repo muss public bleiben (Instagram holt die Medien per raw.githubusercontent.com).

Einrichtung: Jedes Token braucht zusaetzlich die Berechtigung `instagram_content_publish`.
Test: Actions -> "Publish Instagram" -> Run workflow (zuerst mit `dry_run`; optional `account` und `id`).

### Reels

Typ `reel`: genau 1 MP4 (H.264-Video, AAC-Ton, Ton optional), Format 9:16 (z.B. 1080x1920),
3 bis 90 Sekunden, maximal 100 MB (GitHub-Limit). Optional ein Cover (`--cover`, wird JPEG) und
`share_to_feed` (Standard: an; `--no-share-to-feed` zum Abschalten).
Dauer, Format und Codec werden ohne Zusatzprogramm direkt aus der MP4-Datei gelesen (kein ffprobe noetig).

Instagram braucht fuer Videos deutlich laenger als fuer Bilder; `publish.py` wartet bis zu 15 Minuten
auf den Container (`VIDEO_WAIT_SECONDS`). Wird der Lauf dabei abgebrochen, nutzt der naechste Versuch
denselben Container weiter, statt das Video neu zu senden.

Upload-Weg (`--video-upload` bzw. Workflow-Eingabe `video_upload`):
- `auto` (Standard): Instagram holt das Video per URL von raw.githubusercontent.com. Ist die URL nicht
  erreichbar oder lehnt Instagram den Container mit `ERROR` ab, wird das Video einmal per Resumable-Upload
  (`upload_type=resumable`, `rupload.facebook.com`) selbst hochgeladen.
- `url`: nur URL-Abruf. `resumable`: immer selbst hochladen. Das Token geht dabei nur an Facebook-Hosts.

Hinweis: Videos in Git bleiben auch nach dem Aufraeumen in der Historie und vergroessern das Repo.

### Sicherheitsnetze (je Account)

Statusfile plus Caption-Abgleich gegen Doppelposts; Posts, die mehr als 24 Stunden
ueberfaellig sind, werden nicht mehr gepostet (roter Lauf = Mail von GitHub); Medien werden vorab
geprueft; technische Fehler werden bis zu 3 Mal wiederholt. Nach 14 Tagen werden nur die Medien
(`.jpg`, `.mp4`) geloescht, `post.json` und Status bleiben fuer die Auswertung. Eintraege im alten
Layout (`queue/<id>`) werden nicht still ignoriert, sondern melden einen Fehler (siehe Migration).

### Migration vom alten Layout

Eintraege, die noch als `queue/<id>/` und `posted/<id>.json` vorliegen (z.B. weil sie nach dem Anlegen dieses
Branches auf `main` dazukamen), verschiebt `python scripts/migrate_to_accounts.py` nach `queue/agentur/` und
`posted/agentur/` (und kopiert `data/*` nach `data/agentur/`). Danach committen. Vor dem Merge pruefen,
ob auf `main` solche Eintraege liegen.

### Puenktlich posten (externer Cron)

GitHub startet geplante Workflows bei wenig genutzten Repos oft nur alle paar Stunden.
Damit Posts um 09:00 rausgehen, startet ein externer Cron-Dienst (z.B. cron-job.org) den
Workflow taeglich um 09:01 Berliner Zeit per API:

- URL: `https://api.github.com/repos/MickIhde/instagram_insights/actions/workflows/publish.yml/dispatches`
- Methode: `POST`
- Header: `Authorization: Bearer <TOKEN>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`
- Body: `{"ref":"main","inputs":{"dry_run":"false"}}` (wichtig: `dry_run` ist sonst `true`; `account`/`id` leer = alle Accounts)
- Token: Fine-grained Personal Access Token, nur dieses Repo, Berechtigung "Actions: Read and write"

Der 15-Minuten-Cron bleibt als Reserve aktiv.
