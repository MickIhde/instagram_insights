#!/usr/bin/env python3
"""Migriert das alte Layout auf Accounts (idempotent, loescht nichts):

  queue/<id>/          -> queue/agentur/<id>/
  posted/<id>.json     -> posted/agentur/<id>.json
  data/*.csv|json      -> kopiert nach data/agentur/ (die Originale in data/ bleiben)

Aufruf:  python scripts/migrate_to_accounts.py [--account agentur] [--dry-run]
Danach committen. Ohne Altbestand tut das Skript nichts.
"""
import argparse
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def main(root=ROOT):
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="agentur")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    queue, posted, data = (os.path.join(root, x) for x in ("queue", "posted", "data"))
    moves = []
    if os.path.isdir(queue):
        for x in sorted(os.listdir(queue)):
            if not x.startswith((".", "_")) and os.path.isfile(os.path.join(queue, x, "post.json")):
                moves.append((os.path.join(queue, x), os.path.join(queue, a.account, x)))
    if os.path.isdir(posted):
        for x in sorted(os.listdir(posted)):
            if x.endswith(".json") and os.path.isfile(os.path.join(posted, x)):
                moves.append((os.path.join(posted, x), os.path.join(posted, a.account, x)))
    for src, dst in moves:
        if os.path.exists(dst):
            sys.exit(f"Ziel existiert schon, nichts verschoben: {os.path.relpath(dst, root)}")
    for src, dst in moves:
        print(f"{'(trocken) ' if a.dry_run else ''}{os.path.relpath(src, root)} -> {os.path.relpath(dst, root)}")
        if not a.dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
    if os.path.isdir(data):
        for x in ("account_daily.csv", "media.json", "last_run.json"):
            src, dst = os.path.join(data, x), os.path.join(data, a.account, x)
            if os.path.isfile(src) and not os.path.exists(dst):
                print(f"{'(trocken) ' if a.dry_run else ''}data/{x} -> data/{a.account}/{x} (Kopie)")
                if not a.dry_run:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
    if not moves:
        print("Keine Eintraege im alten Layout.")


if __name__ == "__main__":
    main()
