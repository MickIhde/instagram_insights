#!/usr/bin/env python3
"""Holt Instagram-Insights ueber die Graph API und schreibt sie ins Repo.

Ausgabe:
  data/account_daily.csv  - pro Lauf eine Zeile (Follower, Beitragszahl, Reichweite 28 Tage)
  data/media.json         - Snapshot aller Beitraege mit den neuesten Kennzahlen
  data/last_run.json      - Status und Fehler des letzten Laufs

Benoetigt: Umgebungsvariable IG_TOKEN (GitHub Secret). Nur Standardbibliothek.
"""
import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("IG_TOKEN", "").strip()
IG_USER_ID = os.environ.get("IG_USER_ID", "17841480902982254").strip()
VERSION = os.environ.get("GRAPH_VERSION", "v23.0").strip()
BASE = f"https://graph.facebook.com/{VERSION}"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

errors = []


def get(path, params=None):
    """GET gegen die Graph API. Gibt (json, None) oder (None, Fehlertext) zurueck."""
    q = dict(params or {})
    q["access_token"] = TOKEN
    url = f"{BASE}/{path}?{urllib.parse.urlencode(q)}"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e).get("error", {})
            msg = f"{e.code} {body.get('message', '')} (code {body.get('code')})"
        except Exception:
            msg = f"{e.code}"
        return None, msg
    except Exception as e:  # Netzwerk etc.
        return None, str(e)


def note(msg):
    print("WARN:", msg, file=sys.stderr)
    errors.append(msg)


def account():
    d, err = get(IG_USER_ID, {"fields": "username,followers_count,follows_count,media_count"})
    if err:
        note(f"account: {err}")
        return {}
    return d


def reach_28d():
    since = int((dt.datetime.utcnow() - dt.timedelta(days=28)).timestamp())
    until = int(dt.datetime.utcnow().timestamp())
    d, err = get(f"{IG_USER_ID}/insights",
                 {"metric": "reach", "period": "day", "metric_type": "total_value",
                  "since": since, "until": until})
    if err:
        note(f"reach_28d: {err}")
        return None
    try:
        return d["data"][0]["total_value"]["value"]
    except Exception:
        note("reach_28d: unerwartete Antwort")
        return None


def list_media():
    items, url_params = [], {
        "fields": "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
        "limit": 50,
    }
    path = f"{IG_USER_ID}/media"
    for _ in range(10):  # max. 500 Beitraege
        d, err = get(path, url_params)
        if err:
            note(f"media: {err}")
            break
        items += d.get("data", [])
        nxt = d.get("paging", {}).get("cursors", {}).get("after")
        if not d.get("paging", {}).get("next") or not nxt:
            break
        url_params = dict(url_params, after=nxt)
    return items


# Metriken, die je nach Beitragstyp verfuegbar sind. Wir probieren absteigend.
METRIC_SETS = [
    "views,reach,saved,shares,likes,comments,total_interactions",
    "reach,saved,shares,likes,comments,total_interactions",
    "reach,saved,total_interactions",
    "reach",
]


def media_insights(media_id):
    last_err = None
    for metrics in METRIC_SETS:
        d, err = get(f"{media_id}/insights", {"metric": metrics})
        if err is None:
            return {m["name"]: (m.get("values") or [{}])[0].get("value") for m in d.get("data", [])}, None
        last_err = err
    return {}, last_err


def main():
    if not TOKEN:
        print("IG_TOKEN fehlt (GitHub Secret).", file=sys.stderr)
        return 1
    os.makedirs(DATA, exist_ok=True)
    today = dt.date.today().isoformat()

    acc = account()
    reach = reach_28d()

    # Verlauf: eine Zeile pro Lauf, bestehende Zeile desselben Tages wird ersetzt.
    path = os.path.join(DATA, "account_daily.csv")
    header = ["date", "username", "followers_count", "follows_count", "media_count", "reach_28d"]
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != today]
    rows.append({
        "date": today,
        "username": acc.get("username", ""),
        "followers_count": acc.get("followers_count", ""),
        "follows_count": acc.get("follows_count", ""),
        "media_count": acc.get("media_count", ""),
        "reach_28d": "" if reach is None else reach,
    })
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)

    media = list_media()
    out = []
    for m in media:
        ins, err = media_insights(m["id"])
        if err and not ins:
            note(f"insights {m['id']}: {err}")
        m = dict(m)
        m["insights"] = ins
        out.append(m)
    with open(os.path.join(DATA, "media.json"), "w", encoding="utf-8") as f:
        json.dump({"fetched": today, "media": out}, f, ensure_ascii=False, indent=2)

    status = {"fetched": today, "ok": not errors, "media_count_fetched": len(out), "errors": errors[:20]}
    with open(os.path.join(DATA, "last_run.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    print(json.dumps(status, ensure_ascii=False))
    # Nur hart fehlschlagen, wenn gar nichts abgerufen werden konnte.
    return 0 if (acc or out) else 2


if __name__ == "__main__":
    sys.exit(main())
