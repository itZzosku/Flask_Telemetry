"""Scope zeroing: the swap arithmetic, and a one-time reader for the old Markdown zeroing note.

Sign convention (stored values, in the scope's unit, MRAD or MOA):
  elev  + = UP,    − = DOWN   (turret reading from the reference frame)
  wind  + = RIGHT, − = LEFT

A swap from position A to position B is simply B − A. The database is the source of truth; parse_note()
only brings an existing note (notes/javelin-zeroing-note.md layout) in once. Printouts are PDFs made on
the page.
"""
import re

MINUS = "−"


# ------------------------------------------------------------------ numbers and words
def decimals(click):
    """0.1 → 1, 0.25 / 0.05 → 2: enough to show one click."""
    s = f"{float(click or 0.1):g}"
    return len(s.split(".")[1]) if "." in s else 0


def rnd(v, click=0.1):
    """Snap to whole clicks – nothing between two clicks can be dialled."""
    c = float(click or 0.1)
    return round(round(float(v) / c) * c, max(2, decimals(c)) + 1) + 0.0


def fmt(v, click=0.1):
    return f"{abs(float(v)):.{decimals(click)}f}"


def is_zero(v, click=0.1):
    return abs(float(v)) < float(click or 0.1) / 2


def elev_dial(v, click=0.1):
    """Dial position: 0 / +1.8 / −1.3"""
    if is_zero(v, click):
        return "0"
    return ("+" if v > 0 else MINUS) + fmt(v, click)


def wind_dial(v, click=0.1):
    """Dial position: 0 / 3.6 LEFT / 0.5 RIGHT"""
    if is_zero(v, click):
        return "0"
    return fmt(v, click) + (" RIGHT" if v > 0 else " LEFT")


def elev_move(d, click=0.1):
    if is_zero(d, click):
        return "0"
    return ("UP " if d > 0 else "DOWN ") + fmt(d, click)


def wind_move(d, click=0.1):
    return wind_dial(d, click)


def clicks(d, click=0.1):
    return int(round(abs(float(d)) / float(click or 0.1)))


def swap(a, b, click=0.1):
    """Dial change from position a to position b (dicts with elev / wind)."""
    de, dw = rnd(b["elev"] - a["elev"], click), rnd(b["wind"] - a["wind"], click)
    return {"d_elev": de, "d_wind": dw, "elev": elev_move(de, click), "wind": wind_move(dw, click),
            "elev_clicks": clicks(de, click), "wind_clicks": clicks(dw, click)}


def parse_elev(text):
    """'+1.8', '**0**', '−1.3', 'UP 4.5', 'DOWN 2.7' → signed float."""
    t = _plain(text).upper().replace(MINUS, "-").replace("–", "-").replace(",", ".")
    m = re.search(r"(-|\+)?\s*(\d+(?:\.\d+)?)", t)
    if not m:
        raise ValueError(f"no number in {text!r}")
    v = float(m.group(2))
    if m.group(1) == "-" or "DOWN" in t:
        v = -v
    return v


def parse_wind(text):
    """'3.6 LEFT', '0', '0.1 R', '−0.4' → signed float (+ right)."""
    t = _plain(text).upper().replace(MINUS, "-").replace("–", "-").replace(",", ".")
    m = re.search(r"(-|\+)?\s*(\d+(?:\.\d+)?)", t)
    if not m:
        raise ValueError(f"no number in {text!r}")
    v = float(m.group(2))
    if re.search(r"\bL(EFT)?\b", t) or m.group(1) == "-":
        v = -v
    return v


def _plain(text):
    return re.sub(r"\*+|`", "", str(text or "")).strip()


def _tokens(text):
    """Same idea as the velocity importer: 'AR-15' → 'ar15', 'T3X' → 't3x'."""
    words = re.findall(r"[a-z0-9]+", str(text or "").casefold())
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if i + 1 < len(words) and w.isalpha() and len(w) <= 2 and words[i + 1].isdigit():
            w += words[i + 1]
            i += 1
        out.append(w)
        i += 1
    return set(out)


def position_name(z):
    rifle = (z.get("rifle") or "").strip() or "Rifle"
    return f"{rifle} — {z['label']}" if z.get("label") else rifle


def short_name(z):
    rifle = (z.get("rifle") or "").strip() or "Rifle"
    return f"{rifle} ({z['label']})" if z.get("label") else rifle


# ------------------------------------------------------------------ Markdown tables
def _tables(md):
    """Every pipe table as (heading/paragraph just above it, header cells, rows of cells)."""
    lines = md.splitlines()
    out, i, last_text = [], 0, ""
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            head = _cells(ln)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append((last_text, head, rows))
            continue
        if ln:
            last_text = ln
        i += 1
    return out


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_note(md):
    """Markdown zeroing note → {"scope": {...}, "zeros": [...], "swaps": [...]}.

    Raises ValueError when the dial-position table is missing.
    """
    scope = {"name": None, "unit": "MRAD", "click": 0.1, "zero_stop": None, "notes": "", "status": None,
             "updated": None, "reference": None}
    m = re.search(r"^#\s+(.+?)\s+[—–-]\s+Scope Zeroing Note", md, re.M)
    if m:
        scope["name"] = _plain(m.group(1))
    m = re.search(r"Scope:\s*\*\*(.+?)\*\*", md)
    if m:
        scope["name"] = scope["name"] or _plain(m.group(1))
    m = re.search(r"All values in \*\*(MRAD|MOA|MIL)\*\*", md, re.I)
    if m:
        scope["unit"] = "MOA" if m.group(1).upper() == "MOA" else "MRAD"
        scope["click"] = 0.25 if scope["unit"] == "MOA" else 0.1
    m = re.search(r"click[s]?\s*(?:value)?\s*[:=]?\s*\*?\*?(\d+(?:[.,]\d+)?)", md, re.I)
    if m:
        scope["click"] = float(m.group(1).replace(",", "."))
    m = re.search(r"\*\*Reference:\s*(.+?)\s*=\s*0\s*/\s*0\*\*", md)
    if m:
        scope["reference"] = _plain(m.group(1))
    m = re.search(r"\*\*Last updated:\s*(\d{4}-\d{2}-\d{2})\*\*|\*Last updated:\s*(\d{4}-\d{2}-\d{2})\s*(?:[—–-]\s*(.*?))?\.?\*\s*$",
                  md, re.M)
    if m:
        scope["updated"] = m.group(1) or m.group(2)
        scope["status"] = (m.group(3) or "").strip().rstrip(".") or None

    rifles, zeros, swaps = {}, [], []
    for above, head, rows in _tables(md):
        h = [c.casefold() for c in head]
        if h and h[0] == "rifle" and any("zero distance" in c for c in h):
            ci = {k: next((j for j, c in enumerate(h) if k in c), None) for k in ("caliber", "zero distance", "rail")}
            for r in rows:
                name = _plain(r[0])
                dist = r[ci["zero distance"]] if ci["zero distance"] is not None else ""
                dm = re.search(r"(\d+(?:[.,]\d+)?)", dist)
                rifles[name] = {
                    "cal": _plain(r[ci["caliber"]]) if ci["caliber"] is not None else None,
                    "distance_m": float(dm.group(1).replace(",", ".")) if dm else None,
                    "mount": _plain(r[ci["rail"]]) if ci["rail"] is not None else None}
        elif h and h[0].startswith("rifle") and any("elevation dial" in c for c in h):
            ie = next(j for j, c in enumerate(h) if "elevation" in c)
            iw = next((j for j, c in enumerate(h) if "windage" in c), None)
            for r in rows:
                first = r[0]
                is_ref = "(reference)" in first.casefold()
                first = re.sub(r"\(reference\)", "", first, flags=re.I)
                name = _plain(first)
                if re.search(r"\s[—–]\s", name):
                    rifle, label = (p.strip() for p in re.split(r"\s+[—–]\s+", name, maxsplit=1))
                else:
                    rifle, label = name, ""
                zeros.append({"rifle": rifle, "label": label or None, "elev": parse_elev(r[ie]),
                              "wind": parse_wind(r[iw]) if iw is not None else 0.0, "is_ref": is_ref})
        elif h and h[0] in ("swap", "change") and len(h) >= 3:
            prefix = ""
            pm = re.match(r"\*\*(.+?) load change", above)
            if pm and h[0] == "change":
                prefix = _plain(pm.group(1))
            for r in rows:
                parts = re.split(r"\s*(?:→|->)\s*", _plain(r[0]))
                if len(parts) == 2:
                    swaps.append({"from": (prefix + " " + parts[0]).strip(), "to": (prefix + " " + parts[1]).strip(),
                                  "d_elev": parse_elev(r[1]), "d_wind": parse_wind(r[2])})
    if not zeros:
        raise ValueError("No dial-position table (| Rifle / load | Elevation dial | Windage dial |) found.")
    for z in zeros:
        z.update({k: v for k, v in (rifles.get(z["rifle"]) or {}).items()})
        if scope["reference"] and not any(x["is_ref"] for x in zeros) \
                and _plain(scope["reference"]).casefold() in (z["rifle"].casefold(), position_name(z).casefold()):
            z["is_ref"] = True

    m = re.search(r"Zero stop is set \*\*(\d+(?:[.,]\d+)?)\s*(?:MRAD|MOA|MIL)?\s*(below|above)\*\*", md, re.I)
    if m:
        ref = next((z for z in zeros if z["is_ref"]), None)
        base = ref["elev"] if ref else 0.0
        d = float(m.group(1).replace(",", "."))
        scope["zero_stop"] = round(base - d if m.group(2).lower() == "below" else base + d, 3)

    # Free notes: from '## Notes' to the change log / footer (both are regenerated)
    m = re.search(r"^##\s+Notes\b.*", md, re.M | re.S)
    if m:
        body = m.group(0)
        body = re.split(r"^##\s+Change log\b", body, flags=re.M)[0]
        body = re.sub(r"^\*\*?Last updated:.*$", "", body, flags=re.M)
        scope["notes"] = body.strip().rstrip("-").strip()
    return {"scope": scope, "zeros": zeros, "swaps": swaps}


def match_position(name, zeros):
    """'AR-15 (55 gr)' / 'T3X' → index of the one position whose rifle + label contain all its words."""
    want = _tokens(name)
    hits = [i for i, z in enumerate(zeros) if want and want <= _tokens(z["rifle"]) | _tokens(z.get("label"))]
    if len(hits) > 1:      # 'T1X' vs 'T1X — 40 gr': prefer the position without a label
        exact = [i for i in hits if _tokens(zeros[i]["rifle"]) | _tokens(zeros[i].get("label")) == want]
        hits = exact or hits
    return hits[0] if len(hits) == 1 else None


def check_swaps(zeros, swaps, click=0.1):
    """Compare the note's hand-written swap tables with the dial positions."""
    ok, bad = 0, []
    for s in swaps:
        a, b = match_position(s["from"], zeros), match_position(s["to"], zeros)
        if a is None or b is None:
            bad.append(f"{s['from']} → {s['to']}: could not match the names")
            continue
        calc = swap(zeros[a], zeros[b], click)
        if is_zero(calc["d_elev"] - s["d_elev"], click) and is_zero(calc["d_wind"] - s["d_wind"], click):
            ok += 1
        else:
            bad.append(f"{s['from']} → {s['to']}: note says {elev_move(s['d_elev'], click)} / "
                       f"{wind_move(s['d_wind'], click)}, positions give {calc['elev']} / {calc['wind']}")
    return {"ok": ok, "bad": bad}
