"""Loads as structured fields with one uniform, generated name.

Handload:  <calibre> · <bullet maker> <bullet> <weight> gr · <powder> <charge> gr · <brass> brass
           e.g. "6.5 CM · Lapua Scenar 139 gr · N555 44.0 gr · Lapua brass"
Factory:   <calibre> · <maker> <product> <weight> gr
           e.g. ".22 LR · SK Long Range Match 40 gr"

The calibre is part of the identity: the same 123 gr FMJ over the same charge in .308 Win and in
7.62x53R are two loads.

The name is never typed: it is rebuilt from the fields on every save, so two loads with the same
fields get the same name and the importer matches loads by it.
"""
import re

GRAMS_TO_GR = 15.4323584
KNOWN_MAKERS = ["Sellier & Bellot", "Lapua", "Sako", "Norma", "Hornady", "Sierra", "Berger", "Nosler", "RWS",
                "Geco", "SK", "Eley", "CCI", "Fiocchi", "Federal", "Winchester", "Remington", "PMC", "Barnes",
                "Lehigh", "Prvi Partizan", "PPU", "Vihtavuori", "Hodgdon", "Alliant"]
BULLET_MAKER = {"scenar": "Lapua", "scenar-l": "Lapua", "scenar l": "Lapua", "naturalis": "Lapua",
                "mega": "Lapua", "fmj": None, "blade": "Sako", "hammerhead": "Sako", "eld-m": "Hornady",
                "eld-x": "Hornady", "a-tip": "Hornady", "matchking": "Sierra", "hybrid": "Berger"}
LOAD_COLUMNS = {
    "name": "TEXT", "kind": "TEXT", "cal": "TEXT", "bullet_gr": "REAL", "brass": "TEXT", "powder": "TEXT",
    "charge_gr": "REAL", "notes": "TEXT", "archived": "INTEGER",
    # added with the structured load page
    "bullet_maker": "TEXT", "bullet_model": "TEXT", "maker": "TEXT", "product": "TEXT", "primer": "TEXT",
    "coal_mm": "REAL",
}
FIELDS = {"kind": str, "cal": str, "bullet_maker": str, "bullet_model": str, "bullet_gr": float, "brass": str,
          "powder": str, "charge_gr": float, "primer": str, "coal_mm": float, "maker": str, "product": str,
          "notes": str, "archived": int}


def _num(v):
    """44 → '44.0' for a charge (one decimal always)."""
    return f"{v:.1f}"


def _weight(v):
    """Bullet weight: whole grains without decimals, 100.3 as is."""
    return f"{v:g}" if float(v) != int(v) else str(int(v))


def bullet_label(f):
    parts = [f.get("bullet_maker"), f.get("bullet_model")]
    if f.get("bullet_gr"):
        parts.append(_weight(f["bullet_gr"]) + " gr")
    return " ".join(p for p in parts if p)


def load_name(f):
    cal = [f["cal"]] if f.get("cal") else []
    if f.get("kind") == "factory":
        parts = [f.get("maker"), f.get("product")]
        if f.get("bullet_gr"):
            parts.append(_weight(f["bullet_gr"]) + " gr")
        return " · ".join(cal + [" ".join(p for p in parts if p) or "Factory ammo"])
    out = cal + [bullet_label(f) or "Load"]
    pw = " ".join(p for p in [f.get("powder"), _num(f["charge_gr"]) + " gr" if f.get("charge_gr") is not None else None] if p)
    if pw:
        out.append(pw)
    if f.get("brass"):
        out.append(f["brass"] + " brass")
    return " · ".join(out)


def short_name(f):
    """Without calibre and brass, for places that already know the rifle: 'Lapua Scenar 139 gr · N555 44.0 gr',
    'SK Long Range Match 40 gr'."""
    if f.get("kind") == "factory":
        parts = [f.get("maker"), f.get("product")] + ([_weight(f["bullet_gr"]) + " gr"] if f.get("bullet_gr") else [])
        return " ".join(p for p in parts if p) or "Factory ammo"
    out = [bullet_label(f) or "Load"]
    pw = " ".join(p for p in [f.get("powder"), _num(f["charge_gr"]) + " gr" if f.get("charge_gr") is not None else None] if p)
    if pw:
        out.append(pw)
    return " · ".join(out)


def ladder_group(f):
    """Everything but the charge: the line a charge ladder is drawn along."""
    out = ([f["cal"]] if f.get("cal") else []) + [bullet_label(f) or "Load"]
    if f.get("powder"):
        out.append(f["powder"])
    if f.get("brass"):
        out.append(f["brass"] + " brass")
    return " · ".join(out)


def clean(body):
    """Typed, trimmed field values from a request body / spec; name generated."""
    vals = {}
    for k, typ in FIELDS.items():
        v = body.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            vals[k] = 0 if k == "archived" else None
            continue
        if typ is float:
            v = float(str(v).replace(",", "."))
            if not 0 <= v < 10000:
                raise ValueError("bad_" + k)
            vals[k] = v
        elif typ is int:
            vals[k] = 1 if v in (True, 1, "1", "true", "on") else 0
        else:
            vals[k] = " ".join(str(v).split())[:120]
    vals["kind"] = vals["kind"] if vals.get("kind") in ("handload", "factory") else "handload"
    if vals["kind"] == "factory":
        vals.update(bullet_maker=None, bullet_model=None, brass=None, powder=None, charge_gr=None,
                    primer=None, coal_mm=None)
    else:
        vals.update(maker=None, product=None)
    vals["name"] = load_name(vals)
    return vals


# ------------------------------------------------------------------ from file names / legacy rows
def split_maker(text):
    """'Sellier & Bellot 223. Rem 55gr. FMJ' → ('Sellier & Bellot', '223. Rem 55gr. FMJ')."""
    t = " ".join(str(text or "").split())
    for m in sorted(KNOWN_MAKERS, key=len, reverse=True):
        if t.casefold().startswith(m.casefold() + " ") or t.casefold() == m.casefold():
            return m, t[len(m):].strip()
    first, _, rest = t.partition(" ")
    return (first or None), rest.strip()


def strip_weight(text):
    """Remove bullet-weight tokens ('40gr.', '2,6g', '9,3g_144g', '55gr.') and tidy separators."""
    t = re.sub(r"\b\d+(?:[.,]\d+)?\s*g(?:r)?\.?(?:_\d+(?:[.,]\d+)?\s*g(?:r)?\.?)?(?![a-z])", " ", str(text or ""), flags=re.I)
    return " ".join(t.replace("_", " ").split()).lstrip(" ,-").rstrip(" ,.-")


def factory_grains(text):
    """Bullet weight printed in a factory product name; grams rounded to whole grains (2,6 g → 40 gr)."""
    t = str(text or "")
    if re.search(r"_(\d+(?:[.,]\d+)?)\s*g\b", t) or re.search(r"\d\s*gr\b", t, re.I):
        return grains_from(t)
    w = grains_from(t)
    return float(round(w)) if w else None


def grains_from(text):
    t = str(text or "")
    m = re.search(r"_(\d+(?:[.,]\d+)?)\s*g\b", t)            # '9,3g_144g' → the grain figure after _
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*gr\b", t, re.I)
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*g\b", t, re.I)
    if m:
        return round(float(m.group(1).replace(",", ".")) * GRAMS_TO_GR, 1)
    return None


def bullet_fields(text, bullet_gr=None):
    """'9 g Scenar' → maker Lapua, model Scenar, 139 gr (weight from the sheet when given)."""
    t = " ".join(str(text or "").split())
    w = grains_from(t)
    model = strip_weight(t)
    maker = None
    mk, rest = split_maker(model)
    if mk and mk in KNOWN_MAKERS and rest:
        maker, model = mk, rest
    if not maker:
        maker = BULLET_MAKER.get(model.casefold())
    return {"bullet_maker": maker, "bullet_model": model or None, "bullet_gr": bullet_gr or w}


def from_hints(hints, bullet_gr, cal):
    """Structured load proposed from a file name / folder (see chrono_import.parse_load_name)."""
    if not hints:
        return None
    if hints.get("kind") == "factory" and hints.get("factory"):
        maker, rest = split_maker(hints["factory"])
        return clean({"kind": "factory", "cal": cal, "maker": maker, "product": strip_weight(rest) or None,
                      "bullet_gr": factory_grains(hints["factory"]) or bullet_gr})
    if hints.get("kind") == "handload" and hints.get("bullet"):
        f = bullet_fields(hints["bullet"], bullet_gr)
        return clean(dict(f, kind="handload", cal=cal, brass=hints.get("brass"), powder=hints.get("powder"),
                          charge_gr=hints.get("charge_gr")))
    return None


def from_legacy(row):
    """A load row saved before the structured fields existed."""
    if row.get("kind") == "factory":
        maker, rest = split_maker(row.get("name"))
        return clean({"kind": "factory", "cal": row.get("cal"), "maker": maker,
                      "product": strip_weight(rest) or None,
                      "bullet_gr": factory_grains(row.get("name")) or row.get("bullet_gr"),
                      "notes": row.get("notes"), "archived": row.get("archived")})
    src = row.get("bullet") or (row.get("name") or "").split(",")[0]
    f = bullet_fields(src, row.get("bullet_gr"))
    return clean(dict(f, kind="handload", cal=row.get("cal"), brass=row.get("brass"), powder=row.get("powder"),
                      charge_gr=row.get("charge_gr"), notes=row.get("notes"), archived=row.get("archived")))


def migrate(conn):
    """Add the new columns and fill them for rows saved before this version. Idempotent."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(loads)")}
    for col, typ in LOAD_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE loads ADD COLUMN {col} {typ}")
    conn.row_factory, old_rf = None, conn.row_factory
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(loads)")]
        rows = [dict(zip(cols, r)) for r in conn.execute(
            "SELECT * FROM loads WHERE bullet_model IS NULL AND maker IS NULL AND product IS NULL")]
    finally:
        conn.row_factory = old_rf
    for row in rows:
        vals = from_legacy(row)
        conn.execute("UPDATE loads SET " + ",".join(f"{k}=?" for k in vals) + " WHERE id=?",
                     (*vals.values(), row["id"]))
    backfill_calibres(conn)
    return len(rows)


def backfill_calibres(conn):
    """Loads without a calibre take it from the guns that fired them, when they all agree; then every
    name is regenerated so it carries the calibre. Returns the number of renamed loads."""
    import json
    gun_cal = {}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"guns", "chrono_strings"} <= tables:
        return 0
    for gid, data in conn.execute("SELECT id, data FROM guns"):
        cal = (json.loads(data) or {}).get("cal")
        if cal:
            gun_cal[gid] = " ".join(str(cal).split())
    for (lid,) in conn.execute("SELECT id FROM loads WHERE cal IS NULL OR cal=''").fetchall():
        cals = {gun_cal.get(g) for (g,) in conn.execute(
            "SELECT DISTINCT gun_id FROM chrono_strings WHERE load_id=? AND gun_id IS NOT NULL", (lid,))}
        cals.discard(None)
        if len(cals) == 1:
            conn.execute("UPDATE loads SET cal=? WHERE id=?", (cals.pop(), lid))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(loads)")]
    renamed = 0
    for r in conn.execute("SELECT * FROM loads").fetchall():
        row = dict(zip(cols, r))
        name = load_name(row)
        if name != row["name"]:
            conn.execute("UPDATE loads SET name=? WHERE id=?", (name, row["id"]))
            renamed += 1
    return renamed
