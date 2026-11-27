#!/usr/bin/env python3
"""Veroeffentlicht faellige Posts aus queue/ ueber die Instagram Graph API (mehrere Accounts).

Aufbau eines Eintrags:  queue/<account>/<id>/post.json + Medien im selben Ordner
Status je Eintrag:      posted/<account>/<id>.json  (verhindert Doppelposts)
Accounts:               accounts.json (name, ig_user_id, token_secret)

post.json:
  {
    "type": "carousel" | "image" | "story" | "reel",
    "publish_at": "2026-11-18T09:00:00+01:00",
    "caption": "Text mit #hashtags",          (nicht bei story)
    "images": ["01.jpg", "02.jpg", ...],      (carousel/image/story)
    "video": "video.mp4",                     (nur reel: H.264/AAC, 9:16, 3-90 s)
    "cover": "cover.jpg",                     (nur reel, optional)
    "share_to_feed": true,                    (nur reel, Default true)
    "topic": "Thema, endet mit Punkt.",       (optional, fuer die Auswertung)
    "audience": "website" | "core-package"    (optional, fuer die Auswertung)
  }

Sicherheitsnetze (je Account):
  - Doppelpost-Schutz: Statusfile + Abgleich der Caption mit den letzten Beitraegen
  - Verpasste Posts (mehr als MAX_LATE_HOURS ueberfaellig) werden NICHT mehr gepostet,
    sondern als "missed" markiert und der Lauf schlaegt fehl (Mail von GitHub)
  - Medien werden vorab geprueft (JPEG bzw. MP4: Format, Groesse, Dauer, oeffentlich erreichbar)
  - Fehlgeschlagene Posts werden bis MAX_ATTEMPTS Mal im naechsten Lauf wiederholt
  - Fehlt das Token eines Accounts, wird nur dieser uebersprungen; die anderen laufen weiter

Tokens kommen aus den Umgebungsvariablen, die accounts.json nennt (z.B. IG_TOKEN,
IG_TOKEN_SAFESLOT). Sie werden nie gespeichert oder geloggt. Nur Standardbibliothek.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from igcommon import (MAX_IMAGE_BYTES, ROOT, account_problem, jpeg_size,  # noqa: E402
                      load_accounts, log_skip, validate_reel)

QUEUE = os.path.join(ROOT, "queue")
POSTED = os.path.join(ROOT, "posted")

VERSION = (os.environ.get("GRAPH_VERSION") or "v23.0").strip()
BASE = os.environ.get("GRAPH_BASE", f"https://graph.facebook.com/{VERSION}").rstrip("/")
REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
REF = os.environ.get("GITHUB_SHA") or os.environ.get("BRANCH", "main")
RAW_BASE = os.environ.get(
    "RAW_BASE", f"https://raw.githubusercontent.com/{REPO}/{REF}"
).rstrip("/")
MAX_LATE = dt.timedelta(hours=float(os.environ.get("MAX_LATE_HOURS", "24")))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
PRUNE_DAYS = int(os.environ.get("PRUNE_DAYS", "14"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "3"))
VIDEO_POLL_SECONDS = float(os.environ.get("VIDEO_POLL_SECONDS", "") or max(POLL_SECONDS, 0) * 2)
IMAGE_WAIT = 180            # Sekunden Container-Wartezeit bei Bildern (unveraendert)
VIDEO_WAIT = int(os.environ.get("VIDEO_WAIT_SECONDS", "900"))  # Videos: Verarbeitung dauert
UPLOAD_MODES = ("auto", "url", "resumable")

RATIO_MIN, RATIO_MAX = 0.8, 1.91  # Breite/Hoehe laut Instagram (4:5 bis 1.91:1)
TYPES = ("carousel", "image", "story", "reel")


class PostError(Exception):
    pass


class ContainerError(PostError):
    """Container wurde von Instagram mit ERROR/EXPIRED abgelehnt (es wurde nichts veroeffentlicht)."""


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(s):
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError("publish_at braucht eine Zeitzone, z.B. +01:00")
    return t


def scrub(msg, acct):
    """Entfernt das Token aus Texten, bevor sie in Logs oder Statusfiles landen."""
    t = acct.token if acct else ""
    return msg.replace(t, "***") if t else msg


# ---------------------------------------------------------------- Bilder


def validate_images(d, typ, names):
    errs = []
    for name in names:
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            errs.append(f"{name}: Datei fehlt")
            continue
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > MAX_IMAGE_BYTES:
            errs.append(f"{name}: groesser als 8 MB")
        try:
            w, h = jpeg_size(data)
        except ValueError as ex:
            errs.append(f"{name}: {ex}")
            continue
        if typ != "story" and not (RATIO_MIN <= w / h <= RATIO_MAX):
            errs.append(f"{name}: Seitenverhaeltnis {w}x{h} ausserhalb 4:5 bis 1.91:1")
    return errs


# ---------------------------------------------------------------- Eintraege


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def queue_dir(acct, eid=""):
    return os.path.join(QUEUE, acct.name, eid) if eid else os.path.join(QUEUE, acct.name)


def state_path(acct, eid):
    return os.path.join(POSTED, acct.name, f"{eid}.json")


def read_state(acct, eid):
    try:
        return read_json(state_path(acct, eid))
    except FileNotFoundError:
        return {}


def load_entry(acct, eid):
    """Gibt (entry, fehlerliste) zurueck."""
    d = queue_dir(acct, eid)
    try:
        e = read_json(os.path.join(d, "post.json"))
    except Exception as ex:
        return None, [f"post.json nicht lesbar: {ex}"]
    errs = []
    typ = e.get("type")
    if typ not in TYPES:
        errs.append("type muss carousel, image, story oder reel sein")
    when = None
    try:
        when = parse_when(e["publish_at"])
    except Exception as ex:
        errs.append(f"publish_at ungueltig: {ex}")
    images = e.get("images") or []
    caption = e.get("caption") or ""
    video = e.get("video") or ""
    cover = e.get("cover") or ""
    share = e.get("share_to_feed", True)
    if typ == "carousel" and not 2 <= len(images) <= 10:
        errs.append("carousel braucht 2 bis 10 Bilder")
    if typ == "image" and len(images) != 1:
        errs.append("image braucht genau 1 Bild")
    if typ == "story" and not images:
        errs.append("story braucht mindestens 1 Bild")
    if typ == "reel":
        if images:
            errs.append("reel hat kein 'images' (Cover heisst 'cover')")
        if not isinstance(share, bool):
            errs.append("share_to_feed muss true oder false sein")
    if len(caption) > 2200:
        errs.append("Caption laenger als 2200 Zeichen")
    if sum(1 for w in caption.split() if w.startswith("#")) > 30:
        errs.append("mehr als 30 Hashtags")
    if typ in ("carousel", "image", "story"):
        errs += validate_images(d, typ, images)
    elif typ == "reel":
        if not video:
            errs.append("reel braucht 'video' (eine .mp4-Datei)")
        else:
            errs += validate_reel(d, video, cover or None)
    entry = {"id": eid, "type": typ, "when": when, "caption": caption, "images": images,
             "video": video, "cover": cover, "share_to_feed": share is not False}
    return entry, errs


# ---------------------------------------------------------------- Graph API


def api(acct, method, path, params=None):
    params = dict(params or {})
    params["access_token"] = acct.token
    url = f"{BASE}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    what = f"API {method} {path.split('/')[-1]}"
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e).get("error", {})
            msg = f"{e.code} {body.get('message', '')} (code {body.get('code')})"
        except Exception:
            msg = str(e.code)
        raise PostError(scrub(f"{what}: {msg}", acct))
    except Exception as e:
        raise PostError(scrub(f"{what}: {e}", acct))


def wait_ready(acct, cid, timeout=IMAGE_WAIT, poll=None):
    poll = POLL_SECONDS if poll is None else poll
    t0 = time.time()
    while True:
        d = api(acct, "GET", cid, {"fields": "status_code,status"})
        code = d.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise ContainerError(f"Container {cid}: {code} {d.get('status', '')}")
        if time.time() - t0 > timeout:
            raise PostError(f"Container {cid}: Timeout nach {timeout} s (Status {code})")
        time.sleep(poll)


def check_urls(urls, kind="image"):
    """Pruefen, dass Instagram die Medien oeffentlich abrufen kann."""
    for u in urls:
        try:
            with urllib.request.urlopen(u, timeout=30) as r:
                head = r.read(12)
        except Exception as ex:
            raise PostError(
                f"Datei nicht oeffentlich erreichbar ({ex}). Repo muss public sein: {u}"
            )
        if kind == "video":
            if head[4:8] != b"ftyp":
                raise PostError(f"URL liefert kein MP4: {u}")
        elif head[:2] != b"\xff\xd8":
            raise PostError(f"URL liefert kein JPEG: {u}")


def find_duplicate(acct, caption, when):
    """Sucht unter den letzten Beitraegen einen mit gleicher Caption seit dem Plan-Zeitpunkt."""
    if not caption.strip():
        return None
    d = api(acct, "GET", f"{acct.ig_user_id}/media", {"fields": "id,caption,timestamp", "limit": "15"})
    for m in d.get("data", []):
        if (m.get("caption") or "").strip() != caption.strip():
            continue
        try:
            ts = dt.datetime.fromisoformat(m["timestamp"])
        except Exception:
            return m["id"]
        if ts >= when - dt.timedelta(hours=1):
            return m["id"]
    return None


# ---------------------------------------------------------------- Reels


def rupload_allowed(uri):
    """Das Token darf nur an Facebook (bzw. im Test an den Test-Server) gesendet werden."""
    u = urllib.parse.urlparse(uri)
    if "GRAPH_BASE" in os.environ:
        return u.netloc == urllib.parse.urlparse(BASE).netloc
    return u.scheme == "https" and (u.hostname or "").endswith(".facebook.com")


def reel_params(e, urls):
    p = {"media_type": "REELS", "caption": e["caption"],
         "share_to_feed": "true" if e["share_to_feed"] else "false"}
    if e["cover"]:
        p["cover_url"] = urls["cover"]
    return p


def create_reel_container(acct, e, urls, resumable):
    p = reel_params(e, urls)
    if not resumable:
        p["video_url"] = urls["video"]
        return api(acct, "POST", f"{acct.ig_user_id}/media", p)["id"]
    p["upload_type"] = "resumable"
    d = api(acct, "POST", f"{acct.ig_user_id}/media", p)
    cid, uri = d["id"], d.get("uri")
    if not uri or not rupload_allowed(uri):
        raise PostError("Resumable-Upload: Upload-URI fehlt oder gehoert nicht zu Facebook")
    path = os.path.join(queue_dir(acct, e["id"]), e["video"])
    with open(path, "rb") as f:
        body = f.read()
    req = urllib.request.Request(uri, data=body, method="POST", headers={
        "Authorization": f"OAuth {acct.token}", "offset": "0", "file_size": str(len(body)),
        "Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            res = json.load(r)
    except urllib.error.HTTPError as ex:
        raise PostError(scrub(f"Resumable-Upload: HTTP {ex.code}", acct))
    except Exception as ex:
        raise PostError(scrub(f"Resumable-Upload: {ex}", acct))
    if res.get("success") is not True:
        raise PostError(f"Resumable-Upload: unerwartete Antwort {json.dumps(res)[:200]}")
    return cid


def reusable_container(acct, cid):
    """Container aus einem frueheren Versuch weiterverwenden, wenn er noch lebt."""
    try:
        code = api(acct, "GET", cid, {"fields": "status_code"}).get("status_code")
    except PostError:
        return False
    return code in ("FINISHED", "IN_PROGRESS")


def publish_reel(acct, e, st, save, mode):
    d = urllib.parse.quote(e["id"])
    base = f"{RAW_BASE}/queue/{urllib.parse.quote(acct.name)}/{d}"
    urls = {"video": f"{base}/{urllib.parse.quote(e['video'])}"}
    if e["cover"]:
        urls["cover"] = f"{base}/{urllib.parse.quote(e['cover'])}"
        check_urls([urls["cover"]])

    dup = find_duplicate(acct, e["caption"], e["when"])
    if dup:
        st["media_ids"] = [dup]
        st["note"] = "auf Instagram bereits vorhanden, nicht erneut gepostet"
        return

    cid = st.get("creation_id")
    if cid and not reusable_container(acct, cid):
        cid = None
    resumable = mode == "resumable"
    if not cid:
        if not resumable:
            try:
                check_urls([urls["video"]], kind="video")
            except PostError as ex:
                if mode != "auto":
                    raise
                print(f"[{acct.name}/{e['id']}] URL-Abruf nicht moeglich ({ex}) -> Resumable-Upload")
                resumable = True
        cid = create_reel_container(acct, e, urls, resumable)
        st["creation_id"] = cid
        st["upload"] = "resumable" if resumable else "url"
        save()  # bei Timeout im naechsten Lauf weiterverwenden statt neu hochladen
    try:
        wait_ready(acct, cid, VIDEO_WAIT, VIDEO_POLL_SECONDS)
    except ContainerError as ex:
        if mode != "auto" or st.get("upload") == "resumable":
            raise
        # Instagram konnte das Video nicht per URL holen -> einmal per Resumable-Upload
        print(f"[{acct.name}/{e['id']}] {ex} -> neuer Versuch per Resumable-Upload")
        cid = create_reel_container(acct, e, urls, True)
        st["creation_id"], st["upload"] = cid, "resumable"
        save()
        wait_ready(acct, cid, VIDEO_WAIT, VIDEO_POLL_SECONDS)
    st["media_ids"] = [api(acct, "POST", f"{acct.ig_user_id}/media_publish", {"creation_id": cid})["id"]]


def publish_entry(acct, e, st, save, mode="auto"):
    if e["type"] == "reel":
        return publish_reel(acct, e, st, save, mode)
    d = urllib.parse.quote(e["id"])
    urls = [f"{RAW_BASE}/queue/{urllib.parse.quote(acct.name)}/{d}/{urllib.parse.quote(n)}" for n in e["images"]]
    check_urls(urls)
    typ = e["type"]
    uid = acct.ig_user_id
    if typ in ("carousel", "image"):
        dup = find_duplicate(acct, e["caption"], e["when"])
        if dup:
            st["media_ids"] = [dup]
            st["note"] = "auf Instagram bereits vorhanden, nicht erneut gepostet"
            return
    if typ == "image":
        c = api(acct, "POST", f"{uid}/media", {"image_url": urls[0], "caption": e["caption"]})["id"]
        wait_ready(acct, c)
        st["media_ids"] = [api(acct, "POST", f"{uid}/media_publish", {"creation_id": c})["id"]]
    elif typ == "carousel":
        kids = []
        for u in urls:
            kids.append(api(acct, "POST", f"{uid}/media", {"image_url": u, "is_carousel_item": "true"})["id"])
        for k in kids:
            wait_ready(acct, k)
        parent = api(acct, "POST", f"{uid}/media", {
            "media_type": "CAROUSEL", "children": ",".join(kids), "caption": e["caption"]})["id"]
        wait_ready(acct, parent)
        st["media_ids"] = [api(acct, "POST", f"{uid}/media_publish", {"creation_id": parent})["id"]]
    else:  # story: jedes Bild als eigene Story, Fortschritt wird gespeichert
        st.setdefault("media_ids", [])
        for u in urls[len(st["media_ids"]):]:
            c = api(acct, "POST", f"{uid}/media", {"image_url": u, "media_type": "STORIES"})["id"]
            wait_ready(acct, c)
            st["media_ids"].append(api(acct, "POST", f"{uid}/media_publish", {"creation_id": c})["id"])
            st["status"] = "partial"
            save()


# ---------------------------------------------------------------- Lauf

PRUNE_EXT = (".jpg", ".jpeg", ".mp4")


def prune(accounts):
    """Raeumt Medien bereits gepostete Eintraege nach PRUNE_DAYS auf (Status bleibt)."""
    for acct in accounts:
        pdir = os.path.join(POSTED, acct.name)
        if not os.path.isdir(pdir):
            continue
        for fn in os.listdir(pdir):
            if not fn.endswith(".json"):
                continue
            eid = fn[:-5]
            try:
                st = read_json(os.path.join(pdir, fn))
                t = dt.datetime.fromisoformat(st["updated"].replace("Z", "+00:00"))
            except Exception:
                continue
            d = queue_dir(acct, eid)
            if st.get("status") != "posted" or now() - t <= dt.timedelta(days=PRUNE_DAYS) or not os.path.isdir(d):
                continue
            # nur die Medien loeschen; post.json (Caption, Thema, Zeit) bleibt fuer die Auswertung
            gone = [f for f in os.listdir(d) if f.lower().endswith(PRUNE_EXT)]
            for f in gone:
                os.remove(os.path.join(d, f))
            if gone:
                print(f"[{acct.name}] Medien aufgeraeumt: queue/{acct.name}/{eid} ({len(gone)})")


def legacy_entries():
    """Eintraege im alten Layout (queue/<id>/, posted/<id>.json), die noch nicht migriert sind."""
    found = []
    if os.path.isdir(QUEUE):
        found += [f"queue/{x}" for x in sorted(os.listdir(QUEUE))
                  if not x.startswith((".", "_")) and os.path.isfile(os.path.join(QUEUE, x, "post.json"))]
    if os.path.isdir(POSTED):
        found += [f"posted/{x}" for x in sorted(os.listdir(POSTED))
                  if x.endswith(".json") and os.path.isfile(os.path.join(POSTED, x))]
    return found


def list_ids(acct):
    d = queue_dir(acct)
    if not os.path.isdir(d):
        return []
    return sorted(x for x in os.listdir(d)
                  if not x.startswith((".", "_")) and os.path.isfile(os.path.join(d, x, "post.json")))


def is_open(acct, eid):
    """Noch nicht erledigt (weder gepostet noch verpasst noch endgueltig fehlgeschlagen)."""
    st = read_state(acct, eid)
    if st.get("status") in ("posted", "missed"):
        return False
    return not (st.get("status") == "failed" and st.get("attempts", 0) >= MAX_ATTEMPTS)


def run_account(acct, args):
    """Bearbeitet einen Account. Gibt (anzahl_gepostet_oder_geprueft, fehler, id_gefunden) zurueck."""
    ids = list_ids(acct)
    if args.id:
        ids = [args.id] if args.id in ids else []
        if not ids:
            return 0, 0, False
    problem = account_problem(acct, need_token=not args.dry_run)
    if problem:
        waiting = [i for i in ids if is_open(acct, i)]
        if waiting:
            log_skip(acct, f"{problem}; {len(waiting)} Eintrag/Eintraege warten")
            return 0, 1, True  # roter Lauf, damit wartende Posts nicht unbemerkt liegen bleiben
        print(f"[{acct.name}] uebersprungen: {problem} (Queue leer)")
        return 0, 0, False

    todo, failures = [], 0
    for eid in ids:
        st = read_state(acct, eid)
        if st.get("status") in ("posted", "missed"):
            continue
        if st.get("status") == "failed" and st.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        e, errs = load_entry(acct, eid)
        due = bool(e and e["when"] and (e["when"] <= now() or args.ignore_time))
        if errs:
            print(f"[{acct.name}/{eid}] FEHLER: " + "; ".join(errs))
            if args.dry_run:
                failures += 1
            elif due:
                # Validierungsfehler aendern sich durch Wiederholen nicht: einmal Alarm, dann Ruhe
                write_json(state_path(acct, eid), {"status": "failed", "attempts": MAX_ATTEMPTS,
                                                   "error": "; ".join(errs), "updated": iso(now())})
                failures += 1
            continue
        if args.dry_run:
            what = f"{len(e['images'])} Bild(er)" if e["type"] != "reel" else f"Video {e['video']}"
            print(f"[{acct.name}/{eid}] ok: {e['type']}, {what}, geplant {e['when'].isoformat()}"
                  f"{' (faellig)' if due else ''}")
            continue
        if not due:
            continue
        if not args.ignore_time and now() - e["when"] > MAX_LATE:
            write_json(state_path(acct, eid), {"status": "missed", "error": "zu spaet, nicht mehr gepostet",
                                               "updated": iso(now())})
            print(f"[{acct.name}/{eid}] VERPASST: mehr als {MAX_LATE} ueberfaellig, nicht gepostet")
            failures += 1
            continue
        todo.append((e, st))

    for e, st in todo:
        st.setdefault("attempts", 0)
        st["attempts"] += 1
        path = state_path(acct, e["id"])

        def save(st=st, path=path):
            st["updated"] = iso(now())
            write_json(path, st)

        try:
            publish_entry(acct, e, st, save, args.video_upload)
            st.update(status="posted", type=e["type"], error=None,
                      publish_at=e["when"].isoformat())
            save()
            print(f"[{acct.name}/{e['id']}] gepostet: {', '.join(st['media_ids'])}")
        except PostError as ex:
            st.update(status="failed" if st.get("status") != "partial" else "partial", error=scrub(str(ex), acct))
            if st["status"] == "partial" and st["attempts"] >= MAX_ATTEMPTS:
                st["status"] = "failed"
            save()
            print(f"[{acct.name}/{e['id']}] FEHLGESCHLAGEN (Versuch {st['attempts']}/{MAX_ATTEMPTS}): "
                  f"{scrub(str(ex), acct)}")
            failures += 1
    return len(todo), failures, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="nur pruefen, nichts posten")
    ap.add_argument("--account", help="nur diesen Account bearbeiten (Standard: alle)")
    ap.add_argument("--id", help="nur diesen Eintrag bearbeiten")
    ap.add_argument("--ignore-time", action="store_true",
                    help="Eintrag sofort posten, egal was publish_at sagt (nur mit --id)")
    ap.add_argument("--video-upload", choices=UPLOAD_MODES,
                    default=(os.environ.get("VIDEO_UPLOAD") or "auto"),
                    help="Reels: url = Instagram holt das Video, resumable = wir laden es hoch, "
                         "auto = url, bei Fehler resumable (Standard)")
    args = ap.parse_args()
    if args.ignore_time and not args.id:
        sys.exit("--ignore-time geht nur zusammen mit --id")

    try:
        accounts = load_accounts()
    except (OSError, ValueError) as ex:
        sys.exit(f"accounts.json: {ex}")
    if args.account:
        accounts = [a for a in accounts if a.name == args.account]
        if not accounts:
            sys.exit(f"Account {args.account} nicht in accounts.json")

    failures, active, found = 0, 0, False
    legacy = legacy_entries()
    if legacy:
        print("FEHLER: Eintraege im alten Layout gefunden, bitte 'python scripts/migrate_to_accounts.py' "
              "ausfuehren und committen: " + ", ".join(legacy))
        failures += 1

    for acct in accounts:
        try:
            n, f, hit = run_account(acct, args)
        except Exception as ex:  # ein kaputter Account darf die anderen nicht stoppen
            print(f"[{acct.name}] FEHLER: {scrub(str(ex), acct)}")
            n, f, hit = 0, 1, True
        active += n
        failures += f
        found = found or hit

    if args.id and not found:
        sys.exit(f"Eintrag {args.id} nicht gefunden")
    if not args.dry_run:
        prune(accounts)
    if not active and not failures:
        print("Nichts faellig.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
