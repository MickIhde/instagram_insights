#!/usr/bin/env python3
"""Holt Instagram-Insights ueber die Graph API (je Account) und schreibt sie ins Repo.

Ausgabe je Account (accounts.json):
  data/<account>/account_daily.csv - pro Lauf eine Zeile (Follower, Beitragszahl, Reichweite 28 Tage)
  data/<account>/media.json        - Snapshot aller Beitraege mit den neuesten Kennzahlen
  data/<account>/last_run.json     - Status und Fehler des letzten Laufs
Accounts mit "mirror_data_to_root": true (agentur) bekommen dieselben Dateien zusaetzlich unter
data/ (Altpfad, den der Monatsreport liest).

Reels bekommen zusaetzlich Aufrufe (views) und Wiedergabezeit (ig_reels_avg_watch_time,
ig_reels_video_view_total_time), soweit die Graph-API-Version (GRAPH_VERSION) sie liefert.

Das Token jedes Accounts kommt aus der Umgebungsvariable, die accounts.json nennt.
Fehlt es, wird nur dieser Account uebersprungen. Nur Standardbibliothek.
"""
import csv
import datetime as dt
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from igcommon import ROOT, account_problem, load_accounts, log_skip  # noqa: E402

VERSION = (os.environ.get("GRAPH_VERSION") or "v23.0").strip()
BASE = os.environ.get("GRAPH_BASE", f"https://graph.facebook.com/{VERSION}").rstrip("/")
DATA = os.path.join(ROOT, "data")
FILES = ("account_daily.csv", "media.json", "last_run.json")


class Run:
    """Zustand eines Accounts: Token, ID und gesammelte Warnungen."""

    def __init__(self, acct):
        self.acct = acct
        self.errors = []

    def get(self, path, params=None):
        """GET gegen die Graph API. Gibt (json, None) oder (None, Fehlertext) zurueck."""
        q = dict(params or {})
        q["access_token"] = self.acct.token
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
            return None, self.scrub(msg)
        except Exception as e:  # Netzwerk etc.
            return None, self.scrub(str(e))

    def scrub(self, msg):
        t = self.acct.token
        return msg.replace(t, "***") if t else msg

    def note(self, msg):
        print(f"WARN [{self.acct.name}]:", msg, file=sys.stderr)
        self.errors.append(msg)

    def account(self):
        d, err = self.get(self.acct.ig_user_id, {"fields": "username,followers_count,follows_count,media_count"})
        if err:
            self.note(f"account: {err}")
            return {}
        return d

    def reach_28d(self):
        since = int((dt.datetime.utcnow() - dt.timedelta(days=28)).timestamp())
        until = int(dt.datetime.utcnow().timestamp())
        d, err = self.get(f"{self.acct.ig_user_id}/insights",
                          {"metric": "reach", "period": "day", "metric_type": "total_value",
                           "since": since, "until": until})
        if err:
            self.note(f"reach_28d: {err}")
            return None
        try:
            return d["data"][0]["total_value"]["value"]
        except Exception:
            self.note("reach_28d: unerwartete Antwort")
            return None

    def list_media(self):
        items, url_params = [], {
            "fields": "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
            "limit": 50,
        }
        path = f"{self.acct.ig_user_id}/media"
        for _ in range(10):  # max. 500 Beitraege
            d, err = self.get(path, url_params)
            if err:
                self.note(f"media: {err}")
                break
            items += d.get("data", [])
            nxt = d.get("paging", {}).get("cursors", {}).get("after")
            if not d.get("paging", {}).get("next") or not nxt:
                break
            url_params = dict(url_params, after=nxt)
        return items

    def media_insights(self, media_id, product_type=None):
        sets = METRIC_SETS
        if product_type == "REELS":
            sets = REEL_METRIC_SETS + METRIC_SETS
        last_err = None
        for metrics in sets:
            d, err = self.get(f"{media_id}/insights", {"metric": metrics})
            if err is None:
                return {m["name"]: (m.get("values") or [{}])[0].get("value") for m in d.get("data", [])}, None
            last_err = err
        return {}, last_err


# Metriken, die je nach Beitragstyp verfuegbar sind. Wir probieren absteigend.
METRIC_SETS = [
    "views,reach,saved,shares,likes,comments,total_interactions",
    "reach,saved,shares,likes,comments,total_interactions",
    "reach,saved,total_interactions",
    "reach",
]
# Reels: Wiedergabezeit gibt es erst in neueren API-Versionen; sonst faellt die Abfrage zurueck.
REEL_METRIC_SETS = [
    "views,reach,saved,shares,likes,comments,total_interactions,"
    "ig_reels_avg_watch_time,ig_reels_video_view_total_time",
    "views,reach,saved,shares,likes,comments,total_interactions,ig_reels_avg_watch_time",
]


def out_dirs(acct):
    dirs = [os.path.join(DATA, acct.name)]
    if acct.mirror_data_to_root:
        dirs.append(DATA)
    return dirs


def fetch_account(acct):
    """Holt und schreibt alle Daten eines Accounts. Gibt (status_dict, exit_code) zurueck."""
    run = Run(acct)
    primary = os.path.join(DATA, acct.name)
    os.makedirs(primary, exist_ok=True)
    today = dt.date.today().isoformat()

    acc = run.account()
    reach = run.reach_28d()

    # Verlauf: eine Zeile pro Lauf, bestehende Zeile desselben Tages wird ersetzt.
    path = os.path.join(primary, "account_daily.csv")
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

    out = []
    for m in run.list_media():
        ins, err = run.media_insights(m["id"], m.get("media_product_type"))
        if err and not ins:
            run.note(f"insights {m['id']}: {err}")
        m = dict(m)
        m["insights"] = ins
        out.append(m)
    with open(os.path.join(primary, "media.json"), "w", encoding="utf-8") as f:
        json.dump({"fetched": today, "media": out}, f, ensure_ascii=False, indent=2)

    status = {"fetched": today, "ok": not run.errors, "media_count_fetched": len(out),
              "errors": run.errors[:20]}
    with open(os.path.join(primary, "last_run.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    for d in out_dirs(acct)[1:]:  # Altpfad spiegeln
        for fn in FILES:
            shutil.copyfile(os.path.join(primary, fn), os.path.join(d, fn))
    print(f"[{acct.name}] " + json.dumps(status, ensure_ascii=False))
    # Nur hart fehlschlagen, wenn gar nichts abgerufen werden konnte.
    return status, (0 if (acc or out) else 2)


def main():
    try:
        accounts = load_accounts()
    except (OSError, ValueError) as ex:
        print(f"accounts.json: {ex}", file=sys.stderr)
        return 1
    only = os.environ.get("INSIGHTS_ACCOUNT", "").strip()
    if only:
        accounts = [a for a in accounts if a.name == only]
        if not accounts:
            print(f"Account {only} nicht in accounts.json", file=sys.stderr)
            return 1
    os.makedirs(DATA, exist_ok=True)

    ran, worst = 0, 0
    for acct in accounts:
        problem = account_problem(acct)
        if problem:
            log_skip(acct, problem)
            continue
        try:
            _, code = fetch_account(acct)
        except Exception as ex:  # ein Account darf die anderen nicht stoppen
            print(f"[{acct.name}] FEHLER: {ex}".replace(acct.token, "***"), file=sys.stderr)
            code = 2
        ran += 1
        worst = max(worst, code)
    if not ran:
        print("Kein Account lief (alle uebersprungen).", file=sys.stderr)
        return 1
    return worst


if __name__ == "__main__":
    sys.exit(main())
