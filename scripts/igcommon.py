"""Gemeinsame Helfer: Accounts (accounts.json), JPEG- und MP4-Pruefung. Nur Standardbibliothek.

Tokens werden hier nie gespeichert oder geloggt: accounts.json enthaelt nur den NAMEN der
Umgebungsvariable (token_secret), der Wert kommt zur Laufzeit aus der Umgebung.
"""
import json
import os
import re
import struct

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ACCOUNT_RE = re.compile(r"[a-z0-9_-]+")
SECRET_RE = re.compile(r"[A-Z_][A-Z0-9_]*")

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = int(os.environ.get("MAX_VIDEO_MB", "100")) * 1024 * 1024  # GitHub lehnt Dateien > 100 MB ab
REEL_MIN_SEC, REEL_MAX_SEC = 3.0, 90.0
REEL_RATIO = 9 / 16
REEL_RATIO_TOL = 0.02


# ---------------------------------------------------------------- Accounts


class Account:
    def __init__(self, name, ig_user_id, token_secret, mirror_data_to_root=False):
        self.name = name
        self.ig_user_id = ig_user_id
        self.token_secret = token_secret
        self.mirror_data_to_root = mirror_data_to_root

    @property
    def token(self):
        return os.environ.get(self.token_secret, "").strip()

    def __repr__(self):
        return f"Account({self.name})"


def load_accounts(root=ROOT):
    """Liest accounts.json. Wirft ValueError bei kaputter Datei (lieber laut als still falsch)."""
    path = os.path.join(root, "accounts.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out, seen = [], set()
    for a in raw.get("accounts", []):
        name = str(a.get("name", ""))
        secret = str(a.get("token_secret", ""))
        if not ACCOUNT_RE.fullmatch(name):
            raise ValueError(f"accounts.json: ungueltiger Name {name!r} (nur a-z, 0-9, _ und -)")
        if name in seen:
            raise ValueError(f"accounts.json: Name {name!r} doppelt")
        if not SECRET_RE.fullmatch(secret):
            raise ValueError(f"accounts.json: token_secret von {name!r} muss ein Variablenname sein (z.B. IG_TOKEN)")
        seen.add(name)
        uid = str(a.get("ig_user_id", "")).strip()
        # Altbetrieb: fuer "agentur" darf die Repo-Variable IG_USER_ID weiterhin die ID setzen
        if name == "agentur" and os.environ.get("IG_USER_ID", "").strip():
            uid = os.environ["IG_USER_ID"].strip()
        out.append(Account(name, uid, secret, bool(a.get("mirror_data_to_root"))))
    if not out:
        raise ValueError("accounts.json: keine Accounts")
    return out


def account_problem(acct, need_token=True):
    """Gibt einen Text zurueck, warum der Account uebersprungen werden muss, sonst None."""
    if not acct.ig_user_id.isdigit():
        return f"ig_user_id fehlt oder ist keine Zahl (accounts.json)"
    if need_token and not acct.token:
        return f"Token fehlt (Secret/Variable {acct.token_secret} nicht gesetzt)"
    return None


def log_skip(acct, reason):
    """GitHub-Actions-Warnung (gelb), ohne einen Wert preiszugeben."""
    print(f"::warning title=Account {acct.name} uebersprungen::{reason}")
    print(f"[{acct.name}] UEBERSPRUNGEN: {reason}")


# ---------------------------------------------------------------- JPEG


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


# ---------------------------------------------------------------- MP4


def _boxes(f, start, end):
    """Iteriert ueber ISO-BMFF-Boxen in [start, end): (typ, payload_start, payload_end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        head = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            raise ValueError(f"Box {typ!r} ungueltig/abgeschnitten")
        yield typ.decode("latin-1"), pos + head, pos + size
        pos += size


def _read(f, a, n):
    f.seek(a)
    return f.read(n)


def mp4_info(path):
    """Liest Dauer, Aufloesung und Codecs aus einer MP4-Datei. Wirft ValueError bei Problemen.

    Rueckgabe: {"duration", "width", "height", "video_codec", "audio_codec", "brand"}
    (audio_codec ist None, wenn keine Tonspur existiert).
    """
    size = os.path.getsize(path)
    info = {"duration": None, "width": None, "height": None,
            "video_codec": None, "audio_codec": None, "brand": None}
    with open(path, "rb") as f:
        top = list(_boxes(f, 0, size))
        kinds = [t for t, _, _ in top]
        if not top or kinds[0] != "ftyp":
            raise ValueError("keine MP4-Datei (ftyp fehlt)")
        a, _b = top[0][1], top[0][2]
        info["brand"] = _read(f, a, 4).decode("latin-1")
        if "moov" not in kinds:
            raise ValueError("moov fehlt (Datei unvollstaendig?)")
        _, ms, me = next(x for x in top if x[0] == "moov")
        for typ, s, e in _boxes(f, ms, me):
            if typ == "mvhd":
                ver = _read(f, s, 1)[0]
                if ver == 1:
                    ts, dur = struct.unpack(">IQ", _read(f, s + 20, 12))
                else:
                    ts, dur = struct.unpack(">II", _read(f, s + 12, 8))
                if ts:
                    info["duration"] = dur / ts
            elif typ == "trak":
                _parse_trak(f, s, e, info)
    if info["duration"] is None:
        raise ValueError("Dauer nicht lesbar")
    if info["video_codec"] is None:
        raise ValueError("keine Videospur")
    return info


def _parse_trak(f, s, e, info):
    width = height = None
    handler = codec = None
    swap = False
    for typ, ts, te in _boxes(f, s, e):
        if typ == "tkhd":
            ver = _read(f, ts, 1)[0]
            mo = 52 if ver == 1 else 40  # Offset der Matrix
            m = struct.unpack(">9i", _read(f, ts + mo, 36))
            w, h = struct.unpack(">II", _read(f, ts + mo + 36, 8))
            width, height = w / 65536, h / 65536
            swap = m[0] == 0 and m[1] != 0  # 90/270 Grad gedreht
        elif typ == "mdia":
            for t2, s2, e2 in _boxes(f, ts, te):
                if t2 == "hdlr":
                    handler = _read(f, s2 + 8, 4).decode("latin-1")
                elif t2 == "minf":
                    for t3, s3, e3 in _boxes(f, s2, e2):
                        if t3 != "stbl":
                            continue
                        for t4, s4, e4 in _boxes(f, s3, e3):
                            if t4 == "stsd" and e4 - s4 >= 16:
                                codec = _read(f, s4 + 12, 4).decode("latin-1")
    if handler == "vide" and info["video_codec"] is None:
        info["video_codec"] = codec or "?"
        if width and height:
            info["width"], info["height"] = (int(height), int(width)) if swap else (int(width), int(height))
    elif handler == "soun" and info["audio_codec"] is None:
        info["audio_codec"] = codec or "?"


def validate_reel(d, video, cover=None):
    """Prueft ein Reel-Video (+ optionales Cover) im Ordner d. Gibt eine Fehlerliste zurueck."""
    errs = []
    path = os.path.join(d, video)
    if not video.lower().endswith(".mp4"):
        errs.append(f"{video}: muss eine .mp4-Datei sein")
    if not os.path.isfile(path):
        errs.append(f"{video}: Datei fehlt")
    else:
        size = os.path.getsize(path)
        if size > MAX_VIDEO_BYTES:
            errs.append(f"{video}: groesser als {MAX_VIDEO_BYTES // (1024 * 1024)} MB")
        try:
            i = mp4_info(path)
        except (ValueError, struct.error, OSError) as ex:
            errs.append(f"{video}: {ex}")
        else:
            if not REEL_MIN_SEC <= i["duration"] <= REEL_MAX_SEC:
                errs.append(f"{video}: Dauer {i['duration']:.1f} s ausserhalb {REEL_MIN_SEC:.0f} bis {REEL_MAX_SEC:.0f} s")
            if i["video_codec"] not in ("avc1", "avc3"):
                errs.append(f"{video}: Videocodec {i['video_codec']} statt H.264 (avc1)")
            if i["audio_codec"] not in (None, "mp4a"):
                errs.append(f"{video}: Audiocodec {i['audio_codec']} statt AAC (mp4a)")
            if i["width"] and i["height"]:
                if abs(i["width"] / i["height"] - REEL_RATIO) > REEL_RATIO_TOL:
                    errs.append(f"{video}: Format {i['width']}x{i['height']} ist nicht 9:16")
            else:
                errs.append(f"{video}: Aufloesung nicht lesbar")
    if cover:
        cp = os.path.join(d, cover)
        if not os.path.isfile(cp):
            errs.append(f"{cover}: Datei fehlt")
        else:
            with open(cp, "rb") as f:
                data = f.read()
            if len(data) > MAX_IMAGE_BYTES:
                errs.append(f"{cover}: groesser als 8 MB")
            try:
                jpeg_size(data)
            except ValueError as ex:
                errs.append(f"{cover}: {ex}")
    return errs
