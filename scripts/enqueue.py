#!/usr/bin/env python3
"""Legt einen Post in queue/<account>/<id>/ ab: Bilder -> JPEG (bzw. Reel-Video), dazu post.json.

Beispiele:
  python scripts/enqueue.py --account agentur --id 2026-11-18_website --type carousel \\
      --at "2026-11-18 09:00" --caption-file caption.txt slide1.png slide2.png ...

  python scripts/enqueue.py --account safeslot --id 2026-12-02_reel --type reel \\
      --at "2026-12-02 12:00" --caption-file caption.txt --cover cover.png reel.mp4

--at ist Berliner Zeit (Sommer-/Winterzeit wird automatisch beruecksichtigt).
Bilder brauchen Pillow (pip install pillow); ein Reel ohne Cover braucht es nicht.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from igcommon import MAX_IMAGE_BYTES, ROOT, load_accounts, validate_reel  # noqa: E402


def to_jpeg(src, dst):
    from PIL import Image  # nur bei Bildern noetig

    Image.open(src).convert("RGB").save(dst, "JPEG", quality=92, optimize=True)
    if os.path.getsize(dst) > MAX_IMAGE_BYTES:
        sys.exit(f"{os.path.basename(dst)} ist groesser als 8 MB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True, help="Name aus accounts.json, z.B. agentur oder safeslot")
    ap.add_argument("--id", required=True, help="z.B. 2026-11-18_website")
    ap.add_argument("--type", required=True, choices=["carousel", "image", "story", "reel"])
    ap.add_argument("--at", required=True, help='Berliner Zeit, z.B. "2026-11-18 09:00"')
    ap.add_argument("--caption-file", help="Textdatei mit der Caption (nicht bei story)")
    ap.add_argument("--topic", help="Thema des Posts (fuer die Auswertung)")
    ap.add_argument("--audience", choices=["website", "core-package"],
                    help="Zielgruppe (fuer die Auswertung)")
    ap.add_argument("--cover", help="Reel: Titelbild (PNG/JPG), optional")
    ap.add_argument("--no-share-to-feed", action="store_true",
                    help="Reel: nicht zusaetzlich im Feed zeigen (Standard: wird geteilt)")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("media", nargs="+", help="Bilddateien in Reihenfolge (PNG/JPG), bei reel: genau eine .mp4")
    a = ap.parse_args()

    try:
        names_ok = [x.name for x in load_accounts()]
    except (OSError, ValueError) as ex:
        sys.exit(f"accounts.json: {ex}")
    if a.account not in names_ok:
        sys.exit(f"--account {a.account!r} unbekannt. In accounts.json: {', '.join(names_ok)}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", a.id):
        sys.exit("--id darf nur Buchstaben, Ziffern, _ und - enthalten")
    when = dt.datetime.fromisoformat(a.at.replace(" ", "T")).replace(tzinfo=ZoneInfo("Europe/Berlin"))
    if when < dt.datetime.now(dt.timezone.utc):
        print("Hinweis: Zeitpunkt liegt in der Vergangenheit.", file=sys.stderr)

    caption = ""
    if a.caption_file:
        with open(a.caption_file, encoding="utf-8") as f:
            caption = f.read().strip()
    if a.type == "story" and caption:
        sys.exit("Stories haben keine Caption")
    if a.type != "reel" and (a.cover or a.no_share_to_feed):
        sys.exit("--cover und --no-share-to-feed gehen nur bei --type reel")
    if a.type == "reel":
        if len(a.media) != 1 or not a.media[0].lower().endswith(".mp4"):
            sys.exit("reel braucht genau eine .mp4-Datei")
        if not os.path.isfile(a.media[0]):
            sys.exit(f"{a.media[0]} nicht gefunden")

    out = os.path.join(ROOT, "queue", a.account, a.id)
    if os.path.exists(out):
        if not a.overwrite:
            sys.exit(f"queue/{a.account}/{a.id} existiert bereits (--overwrite zum Ersetzen)")
        shutil.rmtree(out)
    os.makedirs(out)

    try:
        post = {"type": a.type, "publish_at": when.isoformat()}
        if a.type == "reel":
            shutil.copyfile(a.media[0], os.path.join(out, "video.mp4"))
            post["video"] = "video.mp4"
            if a.cover:
                to_jpeg(a.cover, os.path.join(out, "cover.jpg"))
                post["cover"] = "cover.jpg"
            post["share_to_feed"] = not a.no_share_to_feed
            errs = validate_reel(out, "video.mp4", post.get("cover"))
            if errs:
                sys.exit("Reel ungueltig:\n  " + "\n  ".join(errs))
            summary = "1 Video"
        else:
            names = []
            for i, src in enumerate(a.media, 1):
                name = f"{i:02d}.jpg"
                to_jpeg(src, os.path.join(out, name))
                names.append(name)
            post["images"] = names
            summary = f"{len(names)} Bild(er)"
        if a.type != "story":
            post["caption"] = caption
        if a.topic:
            post["topic"] = a.topic
        if a.audience:
            post["audience"] = a.audience
        with open(os.path.join(out, "post.json"), "w", encoding="utf-8") as f:
            json.dump(post, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)  # keine halben Eintraege in der Queue hinterlassen
        raise
    print(f"queue/{a.account}/{a.id}: {summary}, {a.type}, {when.isoformat()}")


if __name__ == "__main__":
    main()
