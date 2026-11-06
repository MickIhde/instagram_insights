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
