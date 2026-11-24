#!/usr/bin/env python3
"""Smoke-Test gegen einen simulierten Graph-API-Server (kein echter Account noetig).

  python tests/smoke_test.py
Benoetigt Pillow.
"""
import datetime as dt
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse

from PIL import Image

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
published, calls = [], []


class H(http.server.BaseHTTPRequestHandler):
    root = ""

    def log_message(self, *a):
        pass

    def send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/raw/"):
            p = os.path.join(self.root, urllib.parse.unquote(u.path[5:]))
            if os.path.isfile(p):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(open(p, "rb").read())
            else:
                self.send_response(404)
                self.end_headers()
            return
        if not u.path.startswith("/v23.0"):
            self.send_response(404)
            self.end_headers()
            return
        if u.path.endswith("/media"):
            self.send({"data": [{"id": m["id"], "caption": m["caption"], "timestamp": m["ts"]}
                                for m in reversed(published)]})
        else:
            self.send({"status_code": "FINISHED"})

    def do_POST(self):
        q = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        q = {k: v[0] for k, v in q.items()}
        calls.append((self.path, q))
        if self.path.endswith("/media_publish"):
            cid = q["creation_id"]
            m = {"id": f"media{len(published) + 1}", "caption": created.get(cid, ""),
                 "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")}
            published.append(m)
            self.send({"id": m["id"]})
        else:
            cid = f"c{len(created) + 1}"
            created[cid] = q.get("caption", "")
            self.send({"id": cid})


created = {}


def run(root, *args, env_extra=None):
    env = dict(os.environ, IG_TOKEN="dummy", IG_USER_ID="123", POLL_SECONDS="0",
               GRAPH_BASE=f"http://127.0.0.1:{PORT}/v23.0", RAW_BASE=f"http://127.0.0.1:{PORT}/raw")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, os.path.join(root, "scripts", "publish.py"), *args],
                          env=env, capture_output=True, text=True)


def entry(root, eid, typ, when, n, size=(1080, 1080), caption="Test #a #b"):
    d = os.path.join(root, "queue", eid)
    os.makedirs(d)
    names = []
    for i in range(n):
        Image.new("RGB", size, "white").save(os.path.join(d, f"{i + 1:02d}.jpg"), "JPEG")
        names.append(f"{i + 1:02d}.jpg")
    post = {"type": typ, "publish_at": when.isoformat(), "images": names}
    if typ != "story":
        post["caption"] = caption
    json.dump(post, open(os.path.join(d, "post.json"), "w"))


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
root = tempfile.mkdtemp()
shutil.copytree(os.path.join(HERE, "scripts"), os.path.join(root, "scripts"))
H.root = root
now = dt.datetime.now(dt.timezone.utc)
hour = dt.timedelta(hours=1)

entry(root, "due_carousel", "carousel", now - dt.timedelta(minutes=5), 4)
entry(root, "future", "carousel", now + 3 * hour, 3, caption="Zukunft")
entry(root, "old_missed", "carousel", now - 30 * hour, 3, caption="Zu alt")
entry(root, "bad_ratio", "carousel", now - dt.timedelta(minutes=5), 3, size=(1080, 1920), caption="Falsch")
entry(root, "due_story", "story", now - dt.timedelta(minutes=1), 2)

r = run(root, "--dry-run")
check("bad_ratio" in r.stdout and "Seitenverhaeltnis" in r.stdout and r.returncode == 1,
      "Dry-Run meldet falsches Seitenverhaeltnis")
check(not published, "Dry-Run postet nichts")

r = run(root)
print(r.stdout)
st = lambda i: json.load(open(os.path.join(root, "posted", f"{i}.json")))
check(st("due_carousel")["status"] == "posted", "faelliges Karussell gepostet")
check(sum(1 for p, q in calls if q.get("is_carousel_item")) == 4, "4 Karussell-Kinder angelegt")
check(any(q.get("media_type") == "CAROUSEL" and q["children"] == "c1,c2,c3,c4" for p, q in calls),
      "Kinder in richtiger Reihenfolge")
check(st("due_story")["status"] == "posted" and len(st("due_story")["media_ids"]) == 2, "2 Stories gepostet")
check(st("old_missed")["status"] == "missed", "zu spaetes Post wird nicht mehr gepostet")
check(st("bad_ratio")["status"] == "failed", "ungueltiges Bild wird als Fehler markiert")
check(not os.path.exists(os.path.join(root, "posted", "future.json")), "zukuenftiger Post bleibt unberuehrt")
check(r.returncode == 1, "Lauf mit verpasstem/fehlerhaftem Post schlaegt fehl (Alarm)")

n = len(published)
r = run(root)
check(len(published) == n and r.returncode == 0, "zweiter Lauf: kein Doppelpost, Exit 0")

# Duplikat-Schutz: Statusfile verloren, Post existiert aber schon auf Instagram
os.remove(os.path.join(root, "posted", "due_carousel.json"))
r = run(root)
check(len(published) == n and "bereits" in json.dumps(st("due_carousel")),
      "Duplikat-Schutz: Post wird nicht doppelt veroeffentlicht")

# Bild nicht oeffentlich erreichbar
entry(root, "unreachable", "image", now - dt.timedelta(minutes=2), 1, caption="Unreachable")
r = run(root, env_extra={"RAW_BASE": f"http://127.0.0.1:{PORT}/nope"})
check(st("unreachable")["status"] == "failed" and "oeffentlich" in st("unreachable")["error"],
      "nicht erreichbare Bild-URL wird sauber gemeldet")
r = run(root)
check(st("unreachable")["status"] == "posted" and st("unreachable")["attempts"] == 2,
      "Retry im naechsten Lauf klappt")

# Sofort-Test per --id --ignore-time
r = run(root, "--id", "future", "--ignore-time")
check(st("future")["status"] == "posted", "--id --ignore-time postet sofort")
# Aufraeumen: nach PRUNE_DAYS nur Bilder weg, post.json bleibt
old = st("due_carousel")
old["updated"] = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump(old, open(os.path.join(root, "posted", "due_carousel.json"), "w"))
r = run(root)
left = os.listdir(os.path.join(root, "queue", "due_carousel"))
check(left == ["post.json"], "Aufraeumen loescht nur Bilder, post.json bleibt")
print("\nAlle Tests bestanden.")
