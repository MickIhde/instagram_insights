#!/usr/bin/env python3
"""Veroeffentlicht faellige Posts aus queue/ ueber die Instagram Graph API.

Aufbau eines Eintrags:  queue/<id>/post.json + JPEG-Bilder im selben Ordner
Status je Eintrag:      posted/<id>.json  (verhindert Doppelposts)

post.json:
  {
    "type": "carousel" | "image" | "story",
    "publish_at": "2026-11-18T09:00:00+01:00",
    "caption": "Text mit #hashtags",          (nicht bei story)
    "images": ["01.jpg", "02.jpg", ...],
    "topic": "Thema, endet mit Punkt.",       (optional, fuer die Auswertung)
    "audience": "website" | "core-package"    (optional, fuer die Auswertung)
  }

Sicherheitsnetze:
  - Doppelpost-Schutz: Statusfile + Abgleich der Caption mit den letzten Beitraegen
  - Verpasste Posts (mehr als MAX_LATE_HOURS ueberfaellig) werden NICHT mehr gepostet,
    sondern als "missed" markiert und der Lauf schlaegt fehl (Mail von GitHub)
  - Bilder werden vorab geprueft (JPEG, Groesse, Seitenverhaeltnis, oeffentlich erreichbar)
  - Fehlgeschlagene Posts werden bis MAX_ATTEMPTS Mal im naechsten Lauf wiederholt

Benoetigt: Umgebungsvariable IG_TOKEN (GitHub Secret). Nur Standardbibliothek.
"""
import argparse
import datetime as dt
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
QUEUE = os.path.join(ROOT, "queue")
POSTED = os.path.join(ROOT, "posted")

TOKEN = os.environ.get("IG_TOKEN", "").strip()
IG_USER_ID = (os.environ.get("IG_USER_ID") or "17841480902982254").strip()
VERSION = (os.environ.get("GRAPH_VERSION") or "v23.0").strip()
BASE = os.environ.get("GRAPH_BASE", f"https://graph.facebook.com/{VERSION}").rstrip("/")
REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
REF = os.environ.get("GITHUB_SHA") or os.environ.get("BRANCH", "main")
RAW_BASE = os.environ.get(
    "RAW_BASE", f"https://raw.githubusercontent.com/{REPO}/{REF}"
).rstrip("/")
MAX_LATE = dt.timedelta(hours=float(os.environ.get("MAX_LATE_HOURS", "6")))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
PRUNE_DAYS = int(os.environ.get("PRUNE_DAYS", "14"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "3"))

MAX_IMAGE_BYTES = 8 * 1024 * 1024
RATIO_MIN, RATIO_MAX = 0.8, 1.91  # Breite/Hoehe laut Instagram (4:5 bis 1.91:1)


class PostError(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(s):
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError("publish_at braucht eine Zeitzone, z.B. +01:00")
    return t


# ---------------------------------------------------------------- Bilder


def jpeg_size(data):
    """Liest (Breite, Hoehe) aus einem JPEG. Wirft ValueError bei Nicht-JPEG."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("kein JPEG")
    i, n = 2, len(data)
    try:
        while i < n:
            if data[i] != 0xFF:
                i += 1
                continue
            while data[i] == 0xFF:
                i += 1
            marker = data[i]
            i += 1
            if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                continue
            if marker == 0xD9:
                break
            seg = struct.unpack(">H", data[i:i + 2])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 3:i + 7])
                return w, h
            i += seg
    except (IndexError, struct.error):
        pass
    raise ValueError("Bildgroesse nicht lesbar")


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


def state_path(eid):
    return os.path.join(POSTED, f"{eid}.json")


def read_state(eid):
    try:
        return read_json(state_path(eid))
    except FileNotFoundError:
        return {}


def load_entry(eid):
    """Gibt (entry, fehlerliste) zurueck."""
    d = os.path.join(QUEUE, eid)
    try:
        e = read_json(os.path.join(d, "post.json"))
    except Exception as ex:
        return None, [f"post.json nicht lesbar: {ex}"]
    errs = []
    typ = e.get("type")
    if typ not in ("carousel", "image", "story"):
        errs.append("type muss carousel, image oder story sein")
    when = None
    try:
        when = parse_when(e["publish_at"])
    except Exception as ex:
        errs.append(f"publish_at ungueltig: {ex}")
    images = e.get("images") or []
    caption = e.get("caption") or ""
    if typ == "carousel" and not 2 <= len(images) <= 10:
        errs.append("carousel braucht 2 bis 10 Bilder")
    if typ == "image" and len(images) != 1:
        errs.append("image braucht genau 1 Bild")
    if typ == "story" and not images:
        errs.append("story braucht mindestens 1 Bild")
    if len(caption) > 2200:
        errs.append("Caption laenger als 2200 Zeichen")
    if sum(1 for w in caption.split() if w.startswith("#")) > 30:
        errs.append("mehr als 30 Hashtags")
    if typ in ("carousel", "image", "story"):
        errs += validate_images(d, typ, images)
    entry = {"id": eid, "type": typ, "when": when, "caption": caption, "images": images}
    return entry, errs


# ---------------------------------------------------------------- Graph API


def api(method, path, params=None):
    params = dict(params or {})
    params["access_token"] = TOKEN
    url = f"{BASE}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e).get("error", {})
            msg = f"{e.code} {body.get('message', '')} (code {body.get('code')})"
        except Exception:
            msg = str(e.code)
        raise PostError(f"API {method} {path.split('/')[-1]}: {msg}")
    except Exception as e:
        raise PostError(f"API {method} {path.split('/')[-1]}: {e}")


def wait_ready(cid, timeout=180):
    t0 = time.time()
    while True:
        d = api("GET", cid, {"fields": "status_code,status"})
        code = d.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise PostError(f"Container {cid}: {code} {d.get('status', '')}")
        if time.time() - t0 > timeout:
            raise PostError(f"Container {cid}: Timeout (Status {code})")
        time.sleep(POLL_SECONDS)


def check_urls(urls):
    """Pruefen, dass Instagram die Bilder oeffentlich abrufen kann."""
    for u in urls:
        try:
            with urllib.request.urlopen(u, timeout=30) as r:
                head = r.read(4)
        except Exception as ex:
            raise PostError(
                f"Bild nicht oeffentlich erreichbar ({ex}). Repo muss public sein: {u}"
            )
        if head[:2] != b"\xff\xd8":
            raise PostError(f"URL liefert kein JPEG: {u}")


def find_duplicate(caption, when):
    """Sucht unter den letzten Beitraegen einen mit gleicher Caption seit dem Plan-Zeitpunkt."""
    if not caption.strip():
        return None
    d = api("GET", f"{IG_USER_ID}/media", {"fields": "id,caption,timestamp", "limit": "15"})
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


def publish_entry(e, st, save):
    d = urllib.parse.quote(e["id"])
    urls = [f"{RAW_BASE}/queue/{d}/{urllib.parse.quote(n)}" for n in e["images"]]
    check_urls(urls)
    typ = e["type"]
    if typ in ("carousel", "image"):
        dup = find_duplicate(e["caption"], e["when"])
        if dup:
            st["media_ids"] = [dup]
            st["note"] = "auf Instagram bereits vorhanden, nicht erneut gepostet"
            return
    if typ == "image":
        c = api("POST", f"{IG_USER_ID}/media", {"image_url": urls[0], "caption": e["caption"]})["id"]
        wait_ready(c)
        st["media_ids"] = [api("POST", f"{IG_USER_ID}/media_publish", {"creation_id": c})["id"]]
    elif typ == "carousel":
        kids = []
        for u in urls:
            kids.append(api("POST", f"{IG_USER_ID}/media", {"image_url": u, "is_carousel_item": "true"})["id"])
        for k in kids:
            wait_ready(k)
        parent = api("POST", f"{IG_USER_ID}/media", {
            "media_type": "CAROUSEL", "children": ",".join(kids), "caption": e["caption"]})["id"]
        wait_ready(parent)
        st["media_ids"] = [api("POST", f"{IG_USER_ID}/media_publish", {"creation_id": parent})["id"]]
    else:  # story: jedes Bild als eigene Story, Fortschritt wird gespeichert
        st.setdefault("media_ids", [])
        for u in urls[len(st["media_ids"]):]:
            c = api("POST", f"{IG_USER_ID}/media", {"image_url": u, "media_type": "STORIES"})["id"]
            wait_ready(c)
            st["media_ids"].append(api("POST", f"{IG_USER_ID}/media_publish", {"creation_id": c})["id"])
            st["status"] = "partial"
            save()


# ---------------------------------------------------------------- Lauf


def prune():
    """Raeumt Bilder bereits gepostete Eintraege nach PRUNE_DAYS auf (Status bleibt)."""
    if not os.path.isdir(POSTED):
        return
    for fn in os.listdir(POSTED):
        if not fn.endswith(".json"):
            continue
        eid = fn[:-5]
        try:
            st = read_json(os.path.join(POSTED, fn))
            t = dt.datetime.fromisoformat(st["updated"].replace("Z", "+00:00"))
        except Exception:
            continue
        d = os.path.join(QUEUE, eid)
        if st.get("status") != "posted" or now() - t <= dt.timedelta(days=PRUNE_DAYS) or not os.path.isdir(d):
            continue
        # nur die Bilder loeschen; post.json (Caption, Thema, Zeit) bleibt fuer die Auswertung
        gone = [f for f in os.listdir(d) if f.lower().endswith((".jpg", ".jpeg"))]
        for f in gone:
            os.remove(os.path.join(d, f))
        if gone:
            print(f"Bilder aufgeraeumt: queue/{eid} ({len(gone)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="nur pruefen, nichts posten")
    ap.add_argument("--id", help="nur diesen Eintrag bearbeiten")
    ap.add_argument("--ignore-time", action="store_true",
                    help="Eintrag sofort posten, egal was publish_at sagt (nur mit --id)")
    args = ap.parse_args()
    if args.ignore_time and not args.id:
        sys.exit("--ignore-time geht nur zusammen mit --id")

    ids = []
    if os.path.isdir(QUEUE):
        ids = sorted(x for x in os.listdir(QUEUE)
                     if not x.startswith((".", "_")) and os.path.isfile(os.path.join(QUEUE, x, "post.json")))
    if args.id:
        ids = [args.id] if args.id in ids else []
        if not ids:
            sys.exit(f"Eintrag {args.id} nicht gefunden")

    if not args.dry_run and not TOKEN and ids:
        sys.exit("IG_TOKEN fehlt")

    todo, failures = [], 0
    for eid in ids:
        st = read_state(eid)
        if st.get("status") in ("posted", "missed"):
            continue
        if st.get("status") == "failed" and st.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        e, errs = load_entry(eid)
        due = bool(e and e["when"] and (e["when"] <= now() or args.ignore_time))
        if errs:
            print(f"[{eid}] FEHLER: " + "; ".join(errs))
            if args.dry_run:
                failures += 1
            elif due:
                # Validierungsfehler aendern sich durch Wiederholen nicht: einmal Alarm, dann Ruhe
                write_json(state_path(eid), {"status": "failed", "attempts": MAX_ATTEMPTS,
                                             "error": "; ".join(errs), "updated": iso(now())})
                failures += 1
            continue
        if args.dry_run:
            print(f"[{eid}] ok: {e['type']}, {len(e['images'])} Bild(er), geplant {e['when'].isoformat()}"
                  f"{' (faellig)' if due else ''}")
            continue
        if not due:
            continue
        if not args.ignore_time and now() - e["when"] > MAX_LATE:
            write_json(state_path(eid), {"status": "missed", "error": "zu spaet, nicht mehr gepostet",
                                         "updated": iso(now())})
            print(f"[{eid}] VERPASST: mehr als {MAX_LATE} ueberfaellig, nicht gepostet")
            failures += 1
            continue
        todo.append((e, st))

    for e, st in todo:
        st.setdefault("attempts", 0)
        st["attempts"] += 1
        path = state_path(e["id"])

        def save(st=st, path=path):
            st["updated"] = iso(now())
            write_json(path, st)

        try:
            publish_entry(e, st, save)
            st.update(status="posted", type=e["type"], error=None,
                      publish_at=e["when"].isoformat())
            save()
            print(f"[{e['id']}] gepostet: {', '.join(st['media_ids'])}")
        except PostError as ex:
            st.update(status="failed" if st.get("status") != "partial" else "partial", error=str(ex))
            if st["status"] == "partial" and st["attempts"] >= MAX_ATTEMPTS:
                st["status"] = "failed"
            save()
            print(f"[{e['id']}] FEHLGESCHLAGEN (Versuch {st['attempts']}/{MAX_ATTEMPTS}): {ex}")
            failures += 1

    if not args.dry_run:
        prune()
    if not todo and not failures:
        print("Nichts faellig.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
