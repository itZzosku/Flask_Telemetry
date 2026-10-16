"""Parsers for velocity data. Pure functions: bytes in, normalized strings out, no database.

Supported inputs (detected by content, not extension):
  * ShotView export (Garmin Xero C1), legacy .xls or .xlsx: one sheet per session, every cell text in
    the phone's locale ("768,1", "2 720,8", "--").
  * Own log workbooks (.xlsx): one sheet per day named DDMMYYYY[ suffix], labels in row 2 / row 5,
    shots below; columns are found by label.
  * Kestrel DROP datalog CSV: one row per logging interval, merged onto shots by time.

Normalized string:
  {key, source, file, sheet, date, started_at, name, unit_in, bullet_gr, charge_gr,
   hints{gun_text, path, powder, bullet, brass, factory, kind, charge_label},
   shots[{n, v_ms, t, temp_c, rh, hpa, cold_bore, clean_bore, note, excluded}],
   check{...}?, warnings[]}
"""
import csv
import datetime as dt
import io
import re
import statistics
import zipfile

from .chrono_stats import FPS_TO_MS

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAX_SHOTS_PER_STRING = 2000
SKIP_SHEETS = {"summary", "templete", "template", "all shots"}
GRAMS_TO_GR = 15.4323584
POWDER_RE = re.compile(r"^N\d{3}$", re.I)          # Vihtavuori N133, N150, N555 …
SHEET_DATE_RE = re.compile(r"^(\d{2})(\d{2})(\d{4})(?:\s+(.+))?$")


class ParseError(ValueError):
    pass


# ------------------------------------------------------------------ small helpers
def num(v):
    """Locale-tolerant number: 768,1 · '2\xa0720,8' · 841.5 · '--' / '' → None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("\xa0", "").replace(" ", "").replace(" ", "")
    if not s or s in ("--", "-", "—"):
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def hms(v):
    """'15:33:32' · '13.00' · time · datetime → 'HH:MM:SS' (None when unreadable)."""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.strftime("%H:%M:%S")
    if isinstance(v, dt.time):
        return v.strftime("%H:%M:%S")
    s = str(v).strip().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if not m:
        return None
    h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or se > 59:
        return None
    return f"{h:02d}:{mi:02d}:{se:02d}"


def secs(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def fingerprint(date, t, v_ms):
    """Same shot from two sources → same key. Needs a shot time."""
    if not date or not t or v_ms is None:
        return None
    return f"{date}|{t}|{v_ms:.1f}"


def label(v):
    return " ".join(str(v or "").split()).casefold()


def _shot(n, v_ms, t=None, temp=None, rh=None, hpa=None, cold=0, clean=0, note=""):
    return {"n": n, "v_ms": round(v_ms, 2), "t": t, "temp_c": temp, "rh": rh, "hpa": hpa,
            "cold_bore": cold, "clean_bore": clean, "note": note or "", "excluded": 0}


def weather_warnings(shots):
    """RH must be 0–100 %, pressure 850–1100 hPa; flag a likely swap."""
    out = []
    bad = [s for s in shots if (s["rh"] is not None and not 0 <= s["rh"] <= 100)
           or (s["hpa"] is not None and not 850 <= s["hpa"] <= 1100)]
    if bad:
        s = bad[0]
        swapped = (s["rh"] is not None and s["hpa"] is not None
                   and 850 <= s["rh"] <= 1100 and 0 <= s["hpa"] <= 100)
        out.append({"code": "weather_range", "swap": swapped,
                    "text": f"{len(bad)} laukauksella kosteus {s['rh']} % / paine {s['hpa']} hPa ei ole mahdollinen"
                            + (" – kosteus ja paine näyttävät vaihtaneen paikkaa" if swapped else "")})
    return out


def swap_rh_hpa(string):
    for s in string["shots"]:
        s["rh"], s["hpa"] = s["hpa"], s["rh"]


# ------------------------------------------------------------------ names and paths
def file_stem(filename):
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    while True:
        base, dot, ext = name.rpartition(".")
        if dot and ext.lower() in ("xls", "xlsx", "csv", "txt"):
            name = base
            continue
        return name


def _grains_in(text):
    """Bullet weight named in a factory description: '40gr.' → 40 · '2,6g' → 40.1."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*gr(?![a-z])", text, re.I)
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(r"(\d+[.,]\d+)\s*g(?![a-z])", text, re.I)
    if m:
        return round(float(m.group(1).replace(",", ".")) * GRAMS_TO_GR, 1)
    return None


def _charge(text):
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*gr\.?", text.strip(), re.I)
    return float(m.group(1).replace(",", ".")) if m else None


def _brass(text):
    return re.sub(r"\s*brass$", "", text.strip(), flags=re.I)


def parse_load_name(stem):
    """'T3X Super Varmint, 9 g Scenar, Lapua brass, 44.0 gr' → gun/bullet/brass/charge hints;
    'T1X, SK 40gr. Long Range Match' → factory; underscore variants are accepted too."""
    # Separators are ", " — a bare comma is a decimal ("2,6g", "6,5 g HPCE")
    parts = [p.strip() for p in re.split(r",\s+", stem)]
    if len(parts) >= 4:
        gun, bullet, brass, last = parts[0], parts[1], parts[2], ", ".join(parts[3:])
        return {"gun_text": gun, "kind": "handload", "bullet": bullet, "brass": _brass(brass),
                "charge_gr": _charge(last), "charge_label": last, "factory": None}
    if len(parts) >= 2 and not re.search(r"\bbrass\b", stem, re.I):
        ammo = ", ".join(parts[1:])
        return {"gun_text": parts[0], "kind": "factory", "factory": ammo, "bullet": None,
                "brass": None, "charge_gr": None, "charge_label": None,
                "bullet_gr": _grains_in(ammo)}
    flat = stem.replace("_", " ")
    m = re.match(r"^(?P<gun>.+?)\s+(?P<bullet>\d+(?:[.,]\d+)?\s*g\s+.+?)\s+(?P<brass>\S+)\s+brass\s+(?P<rest>.+)$",
                 flat, re.I)
    if m:
        return {"gun_text": m["gun"].strip(), "kind": "handload", "bullet": m["bullet"].strip(),
                "brass": m["brass"], "charge_gr": _charge(m["rest"]), "charge_label": m["rest"].strip(),
                "factory": None}
    return {"gun_text": flat.strip(), "kind": "factory", "factory": flat.strip(), "bullet": None,
            "brass": None, "charge_gr": None, "charge_label": None, "bullet_gr": _grains_in(flat)}


def path_hints(relpath, known_powders=()):
    """Folder names above the file: powder (N555, N133, … or a known name) and the rest as gun candidates."""
    parts = [p for p in relpath.replace("\\", "/").split("/")[:-1] if p and p != "."]
    known = {k.casefold() for k in known_powders}
    powder = next((p for p in parts if POWDER_RE.match(p) or p.casefold() in known), None)
    return {"powder": powder.upper() if powder and POWDER_RE.match(powder) else powder,
            "folders": [p for p in parts if p != powder]}


# ------------------------------------------------------------------ workbook readers
def _sheets_xls(data):
    import xlrd
    try:
        book = xlrd.open_workbook(file_contents=data, on_demand=True)
    except Exception as e:
        raise ParseError(f"ei luettava .xls-tiedosto ({e})")
    for sh in book.sheets():
        rows = [[sh.cell_value(r, c) for c in range(min(sh.ncols, 40))] for r in range(min(sh.nrows, 3000))]
        yield sh.name, rows


def _sheets_xlsx(data):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise ParseError(f"ei luettava .xlsx-tiedosto ({e})")
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(max_row=3000, max_col=40, values_only=True)]
        yield ws.title, rows
    wb.close()


def sniff(data):
    if data[:8] == OLE2_MAGIC:
        return "xls"
    if data[:2] == b"PK":
        try:
            if "xl/workbook.xml" in zipfile.ZipFile(io.BytesIO(data)).namelist():
                return "xlsx"
        except zipfile.BadZipFile:
            pass
        return None
    head = data[:600].decode("utf-8-sig", "ignore")
    if "FORMATTED DATE_TIME" in head or head.startswith('"Device Name"'):
        return "kestrel"
    return None


def _is_shotview(rows):
    return any(r and str(r[0]).strip() == "#" and len(r) > 1 and str(r[1]).startswith("Speed")
               for r in rows[:6])


def _is_log_sheet(rows):
    return any(any(label(c).startswith("speed") for c in r) and any(label(c) == "shot number" for c in r)
               for r in rows[:12])


# ------------------------------------------------------------------ ShotView
def parse_shotview_sheet(sheet_name, rows, filename=""):
    hdr_i = next(i for i, r in enumerate(rows[:6]) if r and str(r[0]).strip() == "#")
    hdr = [str(c or "") for c in rows[hdr_i]]
    unit = "fps" if "FPS" in hdr[1].upper() else "m/s"
    col = {label(h).split(" (")[0]: i for i, h in enumerate(hdr)}
    title = str(rows[0][0] or "") if rows else ""
    m = re.search(r"([\d.,]+)\s*gr", title, re.I)
    bullet_gr = num(m.group(1)) if m else None

    shots, i = [], hdr_i + 1
    while i < len(rows) and rows[i] and str(rows[i][0]).strip() not in ("-", ""):
        r = rows[i]
        v = num(r[1])
        if v is not None:
            v_ms = v * FPS_TO_MS if unit == "fps" else v
            get = lambda name: r[col[name]] if name in col and col[name] < len(r) else None
            shots.append(_shot(len(shots) + 1, v_ms, hms(get("time")),
                               cold=1 if str(get("cold bore") or "").strip() else 0,
                               clean=1 if str(get("clean bore") or "").strip() else 0,
                               note=str(get("shot notes") or "").strip()))
        i += 1

    summary = {}
    for r in rows[i:]:
        if r and r[0] not in (None, "", "-"):
            summary[label(r[0])] = r[1] if len(r) > 1 else None
    date = started = None
    if summary.get("date"):
        try:
            d = dt.datetime.strptime(" ".join(str(summary["date"]).split()), "%B %d, %Y %H:%M")
            date, started = d.date().isoformat(), d.strftime("%H:%M")
        except ValueError:
            pass
    if not date:
        m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})", sheet_name)
        if m:
            date, started = m.group(1), f"{m.group(2)}:{m.group(3)}"
    if bullet_gr is None:
        bullet_gr = num(summary.get("projectile weight (grains)"))

    conv = FPS_TO_MS if unit == "fps" else 1.0
    f_avg, f_sd, f_es = (num(summary.get(k)) for k in ("average speed", "std dev", "spread"))
    vs = [s["v_ms"] for s in shots]
    check = {"file_avg": f_avg and round(f_avg * conv, 1), "file_sd": f_sd and round(f_sd * conv, 1),
             "file_es": f_es and round(f_es * conv, 1)}
    if vs and f_avg is not None:
        ok = abs(statistics.mean(vs) - f_avg * conv) <= 0.11
        # 2024 ShotView versions exported STD DEV as 0,0 — only compare a real figure
        if len(vs) >= 2 and f_sd:
            ok = ok and abs(statistics.pstdev(vs) - f_sd * conv) <= 0.11
        if f_es is not None:
            ok = ok and abs((max(vs) - min(vs)) - f_es * conv) <= 0.11
        check["ok"] = ok
    warnings = [] if check.get("ok", True) else [
        {"code": "check_mismatch", "text": "laskettu keskiarvo/SD/hajonta poikkeaa ShotView'n yhteenvedosta"}]
    if not date:
        warnings.append({"code": "no_date", "text": "päivämäärää ei löytynyt"})
    return {"source": "shotview", "file": filename, "sheet": sheet_name, "date": date,
            "started_at": started, "name": title or sheet_name, "unit_in": unit,
            "bullet_gr": bullet_gr, "charge_gr": None, "hints": {"gun_text": title or None},
            "shots": shots[:MAX_SHOTS_PER_STRING], "check": check, "warnings": warnings}


# ------------------------------------------------------------------ own log workbooks
def _find(labels, *prefixes):
    """Leftmost column whose label starts with one of the prefixes."""
    for i, lab in enumerate(labels):
        if any(lab.startswith(p) for p in prefixes):
            return i
    return None


def parse_log_sheet(sheet_name, rows, filename="", hints=None):
    """One day sheet → one string, or one per charge when a 'Charge weight' column is filled."""
    m = SHEET_DATE_RE.match(sheet_name.strip())
    shot_hdr = next(i for i, r in enumerate(rows[:12]) if any(label(c) == "shot number" for c in r))
    labels = [label(c) for c in rows[shot_hdr]]
    c_n, c_t = _find(labels, "shot number"), _find(labels, "timestamp")
    c_v = _find(labels, "speed")
    c_temp, c_rh, c_p = _find(labels, "temperature"), _find(labels, "humidity"), _find(labels, "pressure")
    c_chg = _find(labels, "charge weight")
    unit = "fps" if "fps" in labels[c_v] else "m/s"

    # Header block: labels on one row, values on the next (row 2/3 in your sheets)
    date = started = bullet_gr = None
    for hi in range(0, shot_hdr):
        hl = [label(c) for c in rows[hi]]
        if "date" in hl and hi + 1 < len(rows):
            vals = rows[hi + 1]
            get = lambda i: vals[i] if i is not None and i < len(vals) else None
            d = get(hl.index("date"))
            if isinstance(d, dt.datetime):
                date = d.date().isoformat()
            elif isinstance(d, dt.date):
                date = d.isoformat()
            started = hms(get(_find(hl, "time")))
            bullet_gr = num(get(_find(hl, "bullet weight")))
            break
    warnings = []
    if m:
        name_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        if date and date != name_date:
            warnings.append({"code": "date_mismatch", "text": f"välilehden nimi {name_date}, solussa {date}"})
        date = date or name_date
    if started:
        started = started[:5]

    groups, charge = {}, None
    for r in rows[shot_hdr + 1:]:
        get = lambda i: r[i] if i is not None and i < len(r) else None
        v = num(get(c_v))
        if v is None:
            continue
        if c_chg is not None and num(get(c_chg)) is not None:
            charge = num(get(c_chg))
        v_ms = v * FPS_TO_MS if unit == "fps" else v
        lst = groups.setdefault(charge, [])
        lst.append(_shot(len(lst) + 1, v_ms, hms(get(c_t)), num(get(c_temp)), num(get(c_rh)), num(get(c_p))))

    out = []
    split = len(groups) > 1 or (c_chg is not None and None not in groups)
    for chg, shots in groups.items():
        s = {"source": "workbook", "file": filename, "sheet": sheet_name, "date": date,
             "started_at": started, "name": sheet_name + (f" · {chg:g} gr" if split and chg else ""),
             "unit_in": unit, "bullet_gr": bullet_gr, "charge_gr": chg if split else None,
             "hints": dict(hints or {}), "shots": shots[:MAX_SHOTS_PER_STRING],
             "warnings": list(warnings) + weather_warnings(shots)}
        if split and chg:
            s["hints"]["charge_gr"] = chg
        out.append(s)
    return out


def _drop_superseded(strings):
    """'29122024' and '29122024 Fixed' with the same shots: keep the Fixed one."""
    keep, notes = [], []
    fixed = [s for s in strings if re.search(r"\bfixed\b", s["sheet"], re.I)]
    for s in strings:
        twin = next((f for f in fixed if f is not s and f["date"] == s["date"]
                     and not re.search(r"\bfixed\b", s["sheet"], re.I)), None)
        if twin:
            a = {fingerprint(s["date"], x["t"], x["v_ms"]) for x in s["shots"]}
            b = {fingerprint(twin["date"], x["t"], x["v_ms"]) for x in twin["shots"]}
            if a and len(a & b) >= len(a) / 2:
                notes.append({"code": "superseded", "text": f"välilehti {s['sheet']} ohitettu: {twin['sheet']} sisältää samat laukaukset"})
                continue
        keep.append(s)
    return keep, notes


def _shift_year(d, years):
    try:
        return d.replace(year=d.year + years)
    except ValueError:                     # 29 Feb
        return d.replace(year=d.year + years, day=28)


def _date_outliers(strings):
    """Flag a likely year typo: the day is ~a year from every other day, and the same day with the
    year moved by one lands within a month of another day (05012024 among Jan 2025 days)."""
    days = sorted({s["date"] for s in strings if s["date"]})
    if len(days) < 2:
        return
    for s in strings:
        if not s["date"]:
            continue
        d = dt.date.fromisoformat(s["date"])
        others = [dt.date.fromisoformat(x) for x in days if x != s["date"]]
        nearest = min(abs((d - o).days) for o in others)
        if not 330 <= nearest <= 400:
            continue
        for years in (1, -1):
            alt = _shift_year(d, years)
            if min(abs((alt - o).days) for o in others) <= 31:
                s["warnings"].append({"code": "date_outlier", "suggest": alt.isoformat(),
                                      "text": f"{s['date']} on vuoden päässä taulukon muista päivistä – "
                                              f"pitäisikö olla {alt.isoformat()}?"})
                break


# ------------------------------------------------------------------ Kestrel
def parse_kestrel(data):
    """Kestrel DROP datalog CSV → [{date, t, temp_c, rh, hpa}] (device serial is not kept)."""
    text = data.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    hdr_i = next((i for i, r in enumerate(rows[:20]) if r and r[0].strip() == "FORMATTED DATE_TIME"), None)
    if hdr_i is None:
        raise ParseError("Kestrel-CSV:n otsikkoriviä ei löytynyt")
    hdr = [label(c) for c in rows[hdr_i]]
    ci = lambda name: hdr.index(name) if name in hdr else None
    c_t, c_rh, c_p = ci("temperature"), ci("relative humidity"), ci("station pressure")
    out = []
    for r in rows[hdr_i + 2:]:            # skip the units row
        if not r or not r[0].strip():
            continue
        stamp = r[0].strip()
        for fmt in ("%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
            try:
                d = dt.datetime.strptime(stamp, fmt)
                break
            except ValueError:
                d = None
        if not d:
            continue
        g = lambda i: num(r[i]) if i is not None and i < len(r) else None
        out.append({"date": d.date().isoformat(), "t": d.strftime("%H:%M:%S"),
                    "temp_c": g(c_t), "rh": g(c_rh), "hpa": g(c_p)})
    return out


def apply_weather(strings, records, window_min=10):
    """Fill empty temp/RH/pressure on each shot from the closest record (same date, ±window). Returns count."""
    by_date = {}
    for rec in records:
        by_date.setdefault(rec["date"], []).append((secs(rec["t"]), rec))
    filled = 0
    for s in strings:
        recs = by_date.get(s.get("date"))
        if not recs:
            continue
        for shot in s["shots"]:
            if not shot.get("t"):
                continue
            ts = secs(shot["t"])
            dist, rec = min(((abs(ts - x), r) for x, r in recs), key=lambda p: p[0])
            if dist > window_min * 60:
                continue
            changed = False
            for k in ("temp_c", "rh", "hpa"):
                if shot.get(k) is None and rec.get(k) is not None:
                    shot[k] = rec[k]
                    changed = True
            filled += changed
    return filled


# ------------------------------------------------------------------ entry point
def parse_file(filename, data, relpath=None, known_powders=()):
    """One uploaded file → {strings, weather, notes}. Raises ParseError when the content is not recognised."""
    kind = sniff(data)
    if kind == "kestrel":
        return {"strings": [], "weather": parse_kestrel(data), "notes": []}
    if kind not in ("xls", "xlsx"):
        raise ParseError(f"{filename}: ei ShotView-vienti, nopeustaulukko eikä Kestrel-CSV")
    sheets = list((_sheets_xls if kind == "xls" else _sheets_xlsx)(data))
    stem = file_stem(filename)
    ph = path_hints(relpath or filename, known_powders)
    strings, notes, log_hints = [], [], None
    for name, rows in sheets:
        if label(name) in SKIP_SHEETS or not rows:
            continue
        if _is_shotview(rows):
            strings.append(parse_shotview_sheet(name, rows, filename))
        elif _is_log_sheet(rows):
            if not SHEET_DATE_RE.match(name.strip()):
                notes.append({"code": "skipped_sheet", "text": f"välilehti {name} ohitettu (ei päivätty välilehti)"})
                continue
            if log_hints is None:
                log_hints = parse_load_name(stem)
                log_hints.update(powder=ph["powder"], path=ph["folders"])
            strings.extend(parse_log_sheet(name, rows, filename, log_hints))
    strings = [s for s in strings if s["shots"]]
    if log_hints is not None:
        strings, more = _drop_superseded(strings)
        notes += more
        _date_outliers([s for s in strings if s["source"] == "workbook"])
        for s in strings:
            if s["source"] == "workbook" and s["bullet_gr"] is None:
                s["bullet_gr"] = log_hints.get("bullet_gr")
    if not strings and not notes:
        notes.append({"code": "empty", "text": f"{filename}: ei laukauksia"})
    for i, s in enumerate(strings):
        s["key"] = f"{stem}#{s['sheet']}#{s.get('charge_gr') or ''}#{i}"
    return {"strings": strings, "weather": [], "notes": notes}


def parse_many(files, known_powders=()):
    """files = [(filename, bytes, relpath)] → merged result with Kestrel weather applied."""
    strings, weather, notes, errors = [], [], [], []
    for filename, data, relpath in files:
        try:
            res = parse_file(filename, data, relpath, known_powders)
        except ParseError as e:
            errors.append(str(e))
            continue
        strings += res["strings"]
        weather += res["weather"]
        notes += res["notes"]
    filled = apply_weather(strings, weather) if weather else 0
    return {"strings": strings, "weather_records": len(weather), "weather_filled": filled,
            "notes": notes, "errors": errors}
