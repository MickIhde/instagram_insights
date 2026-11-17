#!/usr/bin/env python3
"""Legt einen Post in queue/ ab: Bilder -> JPEG, dazu post.json.

Beispiel:
  python scripts/enqueue.py --id 2026-11-18_website --type carousel \\
      --at "2026-11-18 09:00" --caption-file caption.txt slide1.png slide2.png ...

--at ist Berliner Zeit (Sommer-/Winterzeit wird automatisch beruecksichtigt).
Benoetigt Pillow (pip install pillow).
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from zoneinfo import ZoneInfo

from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="z.B. 2026-11-18_website")
    ap.add_argument("--type", required=True, choices=["carousel", "image", "story"])
    ap.add_argument("--at", required=True, help='Berliner Zeit, z.B. "2026-11-18 09:00"')
    ap.add_argument("--caption-file", help="Textdatei mit der Caption (nicht bei story)")
    ap.add_argument("--topic", help="Thema des Posts (fuer die Auswertung)")
    ap.add_argument("--audience", choices=["website", "core-package"],
                    help="Zielgruppe (fuer die Auswertung)")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("images", nargs="+", help="Bilddateien in Reihenfolge (PNG/JPG)")
    a = ap.parse_args()

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

    out = os.path.join(ROOT, "queue", a.id)
    if os.path.exists(out):
        if not a.overwrite:
            sys.exit(f"queue/{a.id} existiert bereits (--overwrite zum Ersetzen)")
        shutil.rmtree(out)
    os.makedirs(out)

    names = []
    for i, src in enumerate(a.images, 1):
        name = f"{i:02d}.jpg"
        img = Image.open(src).convert("RGB")
        img.save(os.path.join(out, name), "JPEG", quality=92, optimize=True)
        if os.path.getsize(os.path.join(out, name)) > 8 * 1024 * 1024:
            sys.exit(f"{name} ist groesser als 8 MB")
        names.append(name)

    post = {"type": a.type, "publish_at": when.isoformat(), "images": names}
    if a.type != "story":
        post["caption"] = caption
    if a.topic:
        post["topic"] = a.topic
    if a.audience:
        post["audience"] = a.audience
    with open(os.path.join(out, "post.json"), "w", encoding="utf-8") as f:
        json.dump(post, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"queue/{a.id}: {len(names)} Bild(er), {a.type}, {when.isoformat()}")


if __name__ == "__main__":
    main()
