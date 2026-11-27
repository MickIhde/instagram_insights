#!/usr/bin/env python3
"""Smoke-Test gegen einen simulierten Graph-API-Server (kein echter Account noetig).

  python tests/smoke_test.py
Benoetigt Pillow. ffmpeg/ffprobe sind optional (zusaetzlicher Abgleich mit einem echten MP4).

Geprueft: zwei Accounts (agentur, safeslot), alle Beitragstypen, Reels (URL und Resumable-Upload),
Doppelpost-Schutz/Verpasst-Logik/Retries je Account, fehlendes Token bei einem Account,
Insights je Account, Aufraeumen, altes Layout, und dass kein Token in Dateien oder Logs landet.
"""
import csv
import datetime as dt
import http.server
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.parse

from PIL import Image

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOK = {"agentur": "tokAGENTUR-secret", "safeslot": "tokSAFESLOT-secret"}
UID = {"agentur": "123", "safeslot": "456"}
published = {u: [] for u in UID.values()}
calls, uploads, created = [], [], {}
state = {"fail_url_video": False, "polls": {}}


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
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path.startswith("/raw/"):
            p = os.path.join(self.root, urllib.parse.unquote(u.path[5:]))
            if os.path.isfile(p):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                self.wfile.write(open(p, "rb").read())
            else:
                self.send_response(404)
                self.end_headers()
            return
        parts = u.path.strip("/").split("/")
        if parts[0] != "v23.0":
            self.send_response(404)
            self.end_headers()
            return
        calls.append(("GET " + u.path, q))
        if len(parts) == 3 and parts[2] == "media":  # Beitragsliste eines Accounts
            self.send({"data": [{"id": m["id"], "caption": m["caption"], "timestamp": m["ts"],
                                 "media_type": "VIDEO" if m.get("reel") else "IMAGE",
                                 "media_product_type": "REELS" if m.get("reel") else "FEED"}
                                for m in reversed(published[parts[1]])]})
        elif len(parts) == 3 and parts[2] == "insights" and parts[1] in published:  # Account-Reichweite
            self.send({"data": [{"name": "reach", "total_value": {"value": 4242}}]})
        elif len(parts) == 3 and parts[2] == "insights":  # Beitrags-Insights
            metrics = q["metric"].split(",")
            if "ig_reels_video_view_total_time" in metrics:
                self.send({"error": {"message": "metric not supported", "code": 100}}, 400)
            else:
                self.send({"data": [{"name": m, "values": [{"value": 7}]} for m in metrics]})
        elif parts[1] in published:  # Account-Stammdaten
            self.send({"username": "acct" + parts[1], "followers_count": 10, "follows_count": 2,
                       "media_count": len(published[parts[1]])})
        else:  # Container-Status
            c = created.get(parts[1], {})
            if c.get("resumable"):
                code = "FINISHED" if c.get("uploaded") else "IN_PROGRESS"
            elif c.get("video") and state["fail_url_video"]:
                code = "ERROR"
            elif c.get("video"):  # Videos brauchen mehrere Abfragen
                n = state["polls"][parts[1]] = state["polls"].get(parts[1], 0) + 1
                code = "FINISHED" if n >= 3 else "IN_PROGRESS"
            else:
                code = "FINISHED"
            self.send({"status_code": code})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.startswith("/rupload/"):
            cid = self.path.split("/")[-1]
            uploads.append({"cid": cid, "auth": self.headers.get("Authorization"), "offset": self.headers.get("offset"),
                            "file_size": self.headers.get("file_size"), "len": len(body)})
            created[cid]["uploaded"] = True
            return self.send({"success": True})
        q = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
        calls.append((self.path, q))
        ig = self.path.split("/")[2]
        if self.path.endswith("/media_publish"):
            cid = q["creation_id"]
            m = {"id": f"media{sum(len(v) for v in published.values()) + 1}",
                 "caption": created.get(cid, {}).get("caption", ""), "reel": created.get(cid, {}).get("video", False),
                 "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")}
            published[ig].append(m)
            self.send({"id": m["id"]})
        else:
            cid = f"c{len(created) + 1}"
            created[cid] = {"caption": q.get("caption", ""), "ig": ig, "video": q.get("media_type") == "REELS",
                            "resumable": q.get("upload_type") == "resumable"}
            res = {"id": cid}
            if created[cid]["resumable"]:
                res["uri"] = f"http://127.0.0.1:{PORT}/rupload/{cid}"
            self.send(res)


def box(t, payload):
    return struct.pack(">I4s", 8 + len(payload), t) + payload


def make_mp4(dur=10, w=1080, h=1920, vcodec=b"avc1", acodec=b"mp4a", rotate=False):
    """Baut ein winziges, aber gueltig aufgebautes MP4-Geruest (ohne echte Bilddaten)."""
    mat = [0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000] if rotate else \
        [0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000]

    def trak(handler, codec, tw, th):
        tkhd = struct.pack(">I5I", 0, 0, 0, 1, 0, 0) + b"\0" * 8 + b"\0" * 8 + struct.pack(">9i", *mat) \
            + struct.pack(">II", tw << 16, th << 16)
        hdlr = struct.pack(">II4s", 0, 0, handler) + b"\0" * 12 + b"x\0"
        stsd = struct.pack(">II", 0, 1) + box(codec, b"\0" * 78)
        stbl = box(b"stbl", box(b"stsd", stsd))
        return box(b"trak", box(b"tkhd", tkhd) + box(b"mdia", box(b"hdlr", hdlr) + box(b"minf", stbl)))

    mvhd = box(b"mvhd", struct.pack(">IIIII", 0, 0, 0, 1000, int(dur * 1000)) + b"\0" * 80)
    tr = trak(b"vide", vcodec, w, h)
    if acodec:
        tr += trak(b"soun", acodec, 0, 0)
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    return ftyp + box(b"moov", mvhd + tr) + box(b"mdat", b"\0" * 2000)


def run_script(root, script, *args, env_extra=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("IG_", "GITHUB_", "GRAPH_", "RAW_", "VIDEO_", "MAX_", "INSIGHTS_"))}
    env.update(IG_TOKEN=TOK["agentur"], IG_TOKEN_SAFESLOT=TOK["safeslot"], POLL_SECONDS="0",
               GRAPH_BASE=f"http://127.0.0.1:{PORT}/v23.0", RAW_BASE=f"http://127.0.0.1:{PORT}/raw")
    for k, v in (env_extra or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    r = subprocess.run([sys.executable, os.path.join(root, "scripts", script), *args],
                       env=env, capture_output=True, text=True)
    LOG.append(r.stdout + r.stderr)
    return r


def run(root, *args, env_extra=None):
    return run_script(root, "publish.py", *args, env_extra=env_extra)


def entry(root, acct, eid, typ, when, n=1, size=(1080, 1080), caption="Test #a #b", **video):
    d = os.path.join(root, "queue", acct, eid)
    os.makedirs(d)
    post = {"type": typ, "publish_at": when.isoformat()}
    if typ == "reel":
        open(os.path.join(d, "video.mp4"), "wb").write(make_mp4(**video))
        post["video"] = "video.mp4"
    else:
        names = []
        for i in range(n):
            Image.new("RGB", size, "white").save(os.path.join(d, f"{i + 1:02d}.jpg"), "JPEG")
            names.append(f"{i + 1:02d}.jpg")
        post["images"] = names
    if typ != "story":
        post["caption"] = caption
    json.dump(post, open(os.path.join(d, "post.json"), "w"))


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        print("\n".join(LOG[-3:]))
        sys.exit(1)


LOG = []
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
root = tempfile.mkdtemp()
shutil.copytree(os.path.join(HERE, "scripts"), os.path.join(root, "scripts"),
                ignore=shutil.ignore_patterns("__pycache__"))
json.dump({"accounts": [
    {"name": "agentur", "ig_user_id": "123", "token_secret": "IG_TOKEN", "mirror_data_to_root": True},
    {"name": "safeslot", "ig_user_id": "456", "token_secret": "IG_TOKEN_SAFESLOT"}]},
    open(os.path.join(root, "accounts.json"), "w"))
H.root = root
now = dt.datetime.now(dt.timezone.utc)
hour = dt.timedelta(hours=1)
minutes = lambda n: dt.timedelta(minutes=n)
st = lambda a, i: json.load(open(os.path.join(root, "posted", a, f"{i}.json")))
has_state = lambda a, i: os.path.exists(os.path.join(root, "posted", a, f"{i}.json"))
posts_with = lambda a, **kv: [q for p, q in calls if p.startswith(f"/v23.0/{UID[a]}/media") and
                              all(q.get(k) == v for k, v in kv.items())]

# ------------------------------------------------ MP4-Parser und Reel-Validierung
sys.path.insert(0, os.path.join(root, "scripts"))
import igcommon  # noqa: E402

tmp = tempfile.mkdtemp()
for name, kw, frag in [
    ("ok", {}, None),
    ("ok_noaudio", {"acodec": None}, None),
    ("ok_rotated", {"w": 1920, "h": 1080, "rotate": True}, None),
    ("short", {"dur": 2}, "Dauer"),
    ("long", {"dur": 95}, "Dauer"),
    ("landscape", {"w": 1920, "h": 1080}, "9:16"),
    ("hevc", {"vcodec": b"hvc1"}, "H.264"),
    ("mp3", {"acodec": b".mp3"}, "AAC"),
]:
    open(os.path.join(tmp, f"{name}.mp4"), "wb").write(make_mp4(**kw))
    errs = igcommon.validate_reel(tmp, f"{name}.mp4")
    check((not errs) if frag is None else any(frag in e for e in errs), f"Reel-Pruefung: {name}")
open(os.path.join(tmp, "junk.mp4"), "wb").write(b"not a video at all")
check(igcommon.validate_reel(tmp, "junk.mp4"), "Reel-Pruefung: kein MP4 wird abgelehnt")
check(igcommon.validate_reel(tmp, "ok.mp4", "missing.jpg"), "Reel-Pruefung: fehlendes Cover wird gemeldet")
if shutil.which("ffmpeg") and shutil.which("ffprobe"):
    real = os.path.join(tmp, "real.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=4:r=15",
                    "-f", "lavfi", "-i", "sine=d=4", "-c:v", "libx264", "-c:a", "aac", "-shortest", "-y", real],
                   check=True)
    i = igcommon.mp4_info(real)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "json", real], capture_output=True, text=True).stdout)
    check(abs(i["duration"] - float(probe["format"]["duration"])) < 0.2 and (i["width"], i["height"]) == (360, 640)
          and i["video_codec"] == "avc1" and i["audio_codec"] == "mp4a" and not igcommon.validate_reel(tmp, "real.mp4"),
          "Parser stimmt mit echtem MP4 (ffmpeg/ffprobe) ueberein")

# ------------------------------------------------ Szenario: zwei Accounts
entry(root, "agentur", "due_carousel", "carousel", now - minutes(5), 4)
entry(root, "agentur", "future", "carousel", now + 3 * hour, 3, caption="Zukunft")
entry(root, "agentur", "old_missed", "carousel", now - 30 * hour, 3, caption="Zu alt")
entry(root, "agentur", "bad_ratio", "carousel", now - minutes(5), 3, size=(1080, 1920), caption="Falsch")
entry(root, "agentur", "due_story", "story", now - minutes(1), 2)
entry(root, "safeslot", "reel_url", "reel", now - minutes(3), caption="SafeSlot Reel #verein")
entry(root, "safeslot", "reel_too_long", "reel", now - minutes(3), caption="Zu lang", dur=120)
entry(root, "safeslot", "ss_image", "image", now - minutes(2), 1, caption="Test #a #b")  # gleiche Caption wie agentur
entry(root, "safeslot", "reel_future", "reel", now + 5 * hour, caption="Spaeter")

r = run(root, "--dry-run")
check("bad_ratio" in r.stdout and "Seitenverhaeltnis" in r.stdout and r.returncode == 1,
      "Dry-Run meldet falsches Seitenverhaeltnis")
check("reel_too_long" in r.stdout and "Dauer" in r.stdout, "Dry-Run meldet ungueltiges Reel (agentur und safeslot)")
check("[safeslot/reel_url] ok: reel" in r.stdout, "Dry-Run zeigt gueltiges Reel")
check(not any(published.values()) and not created, "Dry-Run postet nichts")

r = run(root)
print(r.stdout)
check(st("agentur", "due_carousel")["status"] == "posted", "agentur: faelliges Karussell gepostet")
check(len(posts_with("agentur", is_carousel_item="true")) == 4, "agentur: 4 Karussell-Kinder angelegt")
check(any(q.get("media_type") == "CAROUSEL" and q["children"] == "c1,c2,c3,c4" for q in posts_with("agentur")),
      "agentur: Kinder in richtiger Reihenfolge")
check(st("agentur", "due_story")["status"] == "posted" and len(st("agentur", "due_story")["media_ids"]) == 2,
      "agentur: 2 Stories gepostet")
check(st("agentur", "old_missed")["status"] == "missed", "agentur: zu spaetes Post wird nicht mehr gepostet")
check(st("agentur", "bad_ratio")["status"] == "failed", "agentur: ungueltiges Bild wird als Fehler markiert")
check(not has_state("agentur", "future") and not has_state("safeslot", "reel_future"),
      "zukuenftige Posts bleiben unberuehrt")
check(st("safeslot", "reel_url")["status"] == "posted", "safeslot: Reel gepostet")
rq = posts_with("safeslot", media_type="REELS")
check(len(rq) == 1 and rq[0]["video_url"].endswith("/queue/safeslot/reel_url/video.mp4")
      and rq[0]["share_to_feed"] == "true" and rq[0]["caption"] == "SafeSlot Reel #verein",
      "Reel: media_type=REELS, video_url, caption, share_to_feed=true")
check(st("safeslot", "reel_url")["upload"] == "url" and state["polls"], "Reel: URL-Weg, Container-Wartezeit mit mehreren Abfragen")
check(st("safeslot", "reel_too_long")["status"] == "failed" and "Dauer" in st("safeslot", "reel_too_long")["error"],
      "safeslot: ungueltiges Reel wird als Fehler markiert")
check(st("safeslot", "ss_image")["status"] == "posted" and "bereits" not in json.dumps(st("safeslot", "ss_image")),
      "Doppelpost-Schutz ist je Account: gleiche Caption bei anderem Account zaehlt nicht")
check(all(q["access_token"] == TOK["safeslot"] for p, q in calls if p.startswith("/v23.0/456/")) and
      all(q["access_token"] == TOK["agentur"] for p, q in calls if p.startswith("/v23.0/123/")),
      "jeder Account nutzt sein eigenes Token")
check(len(published["123"]) == 3 and len(published["456"]) == 2, "Beitraege landen beim richtigen Account")
check(r.returncode == 1, "Lauf mit verpasstem/fehlerhaftem Post schlaegt fehl (Alarm)")

n = sum(len(v) for v in published.values())
r = run(root)
check(sum(len(v) for v in published.values()) == n and r.returncode == 0, "zweiter Lauf: kein Doppelpost, Exit 0")

# Duplikat-Schutz: Statusfile verloren, Post existiert aber schon auf Instagram
for a, i in (("agentur", "due_carousel"), ("safeslot", "reel_url")):
    os.remove(os.path.join(root, "posted", a, f"{i}.json"))
r = run(root)
check(sum(len(v) for v in published.values()) == n and "bereits" in json.dumps(st("agentur", "due_carousel"))
      and "bereits" in json.dumps(st("safeslot", "reel_url")),
      "Duplikat-Schutz: Post/Reel wird nicht doppelt veroeffentlicht")

# Bild nicht oeffentlich erreichbar
entry(root, "agentur", "unreachable", "image", now - minutes(2), 1, caption="Unreachable")
r = run(root, env_extra={"RAW_BASE": f"http://127.0.0.1:{PORT}/nope"})
check(st("agentur", "unreachable")["status"] == "failed" and "oeffentlich" in st("agentur", "unreachable")["error"],
      "nicht erreichbare Bild-URL wird sauber gemeldet")
r = run(root)
check(st("agentur", "unreachable")["status"] == "posted" and st("agentur", "unreachable")["attempts"] == 2,
      "Retry im naechsten Lauf klappt")

# Sofort-Test per --account --id --ignore-time
r = run(root, "--account", "agentur", "--id", "future", "--ignore-time")
check(st("agentur", "future")["status"] == "posted", "--account --id --ignore-time postet sofort")
r = run(root, "--id", "reel_future", "--ignore-time")
check(st("safeslot", "reel_future")["status"] == "posted", "--id findet den Eintrag auch ohne --account")
r = run(root, "--id", "gibt_es_nicht")
check(r.returncode != 0 and "nicht gefunden" in (r.stdout + r.stderr), "unbekannte --id wird gemeldet")

# ------------------------------------------------ Reels: Resumable-Upload
entry(root, "safeslot", "reel_resumable", "reel", now - minutes(2), caption="Resumable gewuenscht")
r = run(root, "--account", "safeslot", "--video-upload", "resumable")
up = uploads[-1] if uploads else {}
size = os.path.getsize(os.path.join(root, "queue", "safeslot", "reel_resumable", "video.mp4"))
check(st("safeslot", "reel_resumable")["status"] == "posted" and st("safeslot", "reel_resumable")["upload"] == "resumable"
      and not any("video_url" in q for q in posts_with("safeslot", caption="Resumable gewuenscht"))
      and up.get("len") == size and up.get("file_size") == str(size) and up.get("offset") == "0",
      "Reel per Resumable-Upload (upload_type=resumable, Datei komplett hochgeladen)")
check(up.get("auth") == f"OAuth {TOK['safeslot']}", "Resumable-Upload: Token nur im Authorization-Header")

# auto: Instagram kann das Video nicht per URL holen (Container ERROR) -> Fallback
state["fail_url_video"] = True
entry(root, "safeslot", "reel_fallback", "reel", now - minutes(2), caption="Fallback noetig")
n_up = len(uploads)
r = run(root, "--account", "safeslot")
check(st("safeslot", "reel_fallback")["status"] == "posted" and st("safeslot", "reel_fallback")["upload"] == "resumable"
      and len(uploads) == n_up + 1, "auto: Container-ERROR bei URL -> Resumable-Upload als Fallback")
# url erzwungen + ERROR -> Fehler, kein Fallback
entry(root, "safeslot", "reel_urlonly", "reel", now - minutes(2), caption="Nur URL")
r = run(root, "--account", "safeslot", "--video-upload", "url")
check(st("safeslot", "reel_urlonly")["status"] == "failed" and "ERROR" in st("safeslot", "reel_urlonly")["error"]
      and len(uploads) == n_up + 1, "url erzwungen: kein Fallback, Fehler wird gemeldet")
state["fail_url_video"] = False
# auto: Video-URL nicht erreichbar -> direkt Resumable
entry(root, "safeslot", "reel_nourl", "reel", now - minutes(2), caption="URL tot")
r = run(root, "--account", "safeslot", "--id", "reel_nourl", env_extra={"RAW_BASE": f"http://127.0.0.1:{PORT}/nope"})
check(st("safeslot", "reel_nourl")["status"] == "posted" and st("safeslot", "reel_nourl")["upload"] == "resumable",
      "auto: nicht erreichbare Video-URL -> Resumable-Upload")

# ------------------------------------------------ fehlendes Token bei einem Account
entry(root, "agentur", "a_after", "image", now - minutes(2), 1, caption="Agentur laeuft weiter")
entry(root, "safeslot", "s_blocked", "image", now - minutes(2), 1, caption="SafeSlot ohne Token")
r = run(root, env_extra={"IG_TOKEN_SAFESLOT": None})
out = r.stdout + r.stderr
check(st("agentur", "a_after")["status"] == "posted", "Token von safeslot fehlt: agentur wird trotzdem gepostet")
check(not has_state("safeslot", "s_blocked") and "UEBERSPRUNGEN" in out and "IG_TOKEN_SAFESLOT" in out
      and "[safeslot]" in out, "Token fehlt: nur safeslot uebersprungen, klar geloggt (Variablenname, nie der Wert)")
check(r.returncode == 1, "wartender Eintrag ohne Token -> roter Lauf")
r = run(root, "--dry-run", env_extra={"IG_TOKEN_SAFESLOT": None})
check("[safeslot/s_blocked] ok" in r.stdout, "Dry-Run braucht kein Token")
r = run(root, env_extra={"IG_TOKEN": None})
check(not has_state("safeslot", "s_blocked") or st("safeslot", "s_blocked")["status"] == "posted",
      "Token von agentur fehlt: safeslot laeuft weiter")
check(st("safeslot", "s_blocked")["status"] == "posted", "  ... und postet seinen Eintrag")
shutil.rmtree(os.path.join(root, "queue", "safeslot", "s_blocked"))  # Aufraeumen fuer den naechsten Test
r = run(root, env_extra={"IG_TOKEN_SAFESLOT": None})
check(r.returncode == 0 and "uebersprungen" in r.stdout, "Token fehlt bei leerer Queue: Hinweis, kein Alarm")

# ------------------------------------------------ Aufraeumen (jpg und mp4)
entry(root, "safeslot", "reel_old", "reel", now - minutes(1), caption="Altes Reel")
run(root, "--account", "safeslot", "--id", "reel_old")
Image.new("RGB", (100, 100)).save(os.path.join(root, "queue", "safeslot", "reel_old", "cover.jpg"), "JPEG")
old = st("safeslot", "reel_old")
old["updated"] = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump(old, open(os.path.join(root, "posted", "safeslot", "reel_old.json"), "w"))
old = st("agentur", "due_carousel")
old["updated"] = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump(old, open(os.path.join(root, "posted", "agentur", "due_carousel.json"), "w"))
r = run(root)
check(os.listdir(os.path.join(root, "queue", "agentur", "due_carousel")) == ["post.json"],
      "Aufraeumen loescht nur Bilder, post.json bleibt")
check(os.listdir(os.path.join(root, "queue", "safeslot", "reel_old")) == ["post.json"],
      "Aufraeumen loescht auch .mp4 und Cover")

# ------------------------------------------------ altes Layout wird nicht still ignoriert
legacy = os.path.join(root, "queue", "old_style")
os.makedirs(legacy)
json.dump({"type": "image", "publish_at": now.isoformat()}, open(os.path.join(legacy, "post.json"), "w"))
r = run(root)
check(r.returncode == 1 and "alten Layout" in r.stdout and "migrate_to_accounts" in r.stdout,
      "Eintrag im alten Layout wird laut gemeldet")
r = run_script(root, "migrate_to_accounts.py")
check(os.path.isfile(os.path.join(root, "queue", "agentur", "old_style", "post.json"))
      and not os.path.exists(legacy), "Migration verschiebt alten Eintrag nach queue/agentur/")
shutil.rmtree(os.path.join(root, "queue", "agentur", "old_style"))

# ------------------------------------------------ enqueue.py
src = tempfile.mkdtemp()
open(os.path.join(src, "r.mp4"), "wb").write(make_mp4())
open(os.path.join(src, "bad.mp4"), "wb").write(make_mp4(w=1920, h=1080))
Image.new("RGB", (1080, 1920), "red").save(os.path.join(src, "cover.png"))
Image.new("RGB", (1080, 1080), "red").save(os.path.join(src, "a.png"))
enq = lambda *a: subprocess.run([sys.executable, os.path.join(root, "scripts", "enqueue.py"), *a],
                                capture_output=True, text=True)
common = ["--id", "enq", "--type", "reel", "--at", "2030-01-01 09:00"]
r = enq(*common, os.path.join(src, "r.mp4"))
check(r.returncode != 0 and "--account" in r.stderr, "enqueue.py: --account ist Pflicht")
r = enq("--account", "nope", *common, os.path.join(src, "r.mp4"))
check(r.returncode != 0 and "unbekannt" in r.stderr, "enqueue.py: unbekannter Account wird abgelehnt")
r = enq("--account", "safeslot", *common, os.path.join(src, "bad.mp4"))
check(r.returncode != 0 and "9:16" in r.stderr and not os.path.exists(os.path.join(root, "queue", "safeslot", "enq")),
      "enqueue.py: ungueltiges Reel wird abgelehnt, nichts bleibt liegen")
r = enq("--account", "safeslot", *common, "--cover", os.path.join(src, "cover.png"), "--no-share-to-feed",
        os.path.join(src, "r.mp4"))
post = json.load(open(os.path.join(root, "queue", "safeslot", "enq", "post.json")))
check(r.returncode == 0 and post["video"] == "video.mp4" and post["cover"] == "cover.jpg"
      and post["share_to_feed"] is False and post["type"] == "reel", "enqueue.py: Reel mit Cover, share_to_feed=false")
r = run(root, "--dry-run", "--account", "safeslot", "--id", "enq")
check("[safeslot/enq] ok: reel" in r.stdout, "enqueue-Ergebnis besteht die Pruefung in publish.py")
r = enq("--account", "agentur", "--id", "enq_img", "--type", "image", "--at", "2030-01-01 09:00",
        "--caption-file", os.devnull, os.path.join(src, "a.png"))
check(r.returncode == 0 and os.path.isfile(os.path.join(root, "queue", "agentur", "enq_img", "01.jpg")),
      "enqueue.py: Bild-Post landet in queue/agentur/")

# ------------------------------------------------ Insights je Account
# Ein Reel und ein Bild gelten als Beitraege der Accounts (siehe published)
r = run_script(root, "fetch_insights.py")
d = lambda *p: os.path.join(root, "data", *p)
check(r.returncode == 0 and os.path.isfile(d("agentur", "media.json")) and os.path.isfile(d("safeslot", "media.json")),
      "Insights: Ausgabe in data/agentur/ und data/safeslot/")
rows = list(csv.DictReader(open(d("safeslot", "account_daily.csv"))))
check(rows[-1]["username"] == "acct456" and rows[-1]["reach_28d"] == "4242", "Insights: Kontodaten je Account")
check(all(open(d(f)).read() == open(d("agentur", f)).read() for f in
          ("media.json", "account_daily.csv", "last_run.json")), "Insights: agentur wird zusaetzlich nach data/ gespiegelt")
check(list(csv.DictReader(open(d("account_daily.csv"))))[-1]["username"] == "acct123",
      "Insights: safeslot wird nicht nach data/ gespiegelt")
media = json.load(open(d("safeslot", "media.json")))["media"]
reel = next(m for m in media if m["media_product_type"] == "REELS")
check(reel["insights"].get("views") == 7 and reel["insights"].get("ig_reels_avg_watch_time") == 7
      and "ig_reels_video_view_total_time" not in reel["insights"],
      "Insights: Reel bekommt views + Wiedergabezeit (Rueckfall, wenn Gesamtzeit nicht unterstuetzt)")
feed = next(m for m in media if m["media_product_type"] == "FEED")
check("ig_reels_avg_watch_time" not in feed["insights"], "Insights: Bilder bekommen keine Reel-Metriken")
r = run_script(root, "fetch_insights.py", env_extra={"IG_TOKEN_SAFESLOT": None})
check(r.returncode == 0 and "UEBERSPRUNGEN" in r.stdout and "[agentur]" in r.stdout,
      "Insights: Token fehlt bei safeslot -> nur dieser uebersprungen, agentur laeuft")
r = run_script(root, "fetch_insights.py", env_extra={"IG_TOKEN_SAFESLOT": None, "IG_TOKEN": None})
check(r.returncode == 1, "Insights: kein Account lauffaehig -> Fehler")

# ------------------------------------------------ keine Tokens in Dateien oder Logs
leaks = []
for dp, _, fs in os.walk(root):
    for f in fs:
        data = open(os.path.join(dp, f), "rb").read()
        leaks += [f for t in TOK.values() if t.encode() in data]
check(not leaks and not any(t in "".join(LOG) for t in TOK.values()),
      "kein Token in Dateien, Statusfiles oder Logs")
print("\nAlle Tests bestanden.")
