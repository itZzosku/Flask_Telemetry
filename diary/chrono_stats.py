"""Velocity statistics shared by the API and the CLI.

All figures are computed from included shots (m/s). SD is the sample SD (÷ n−1), which estimates
the load's true spread; `sd_garmin` is the population SD (÷ n) that the Xero/ShotView shows.
"""
import math

GRAIN_KG = 0.06479891e-3
FPS_TO_MS = 0.3048

# Two-sided 95 % t critical values, df 1..30
_T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]


def t95(df):
    if df < 1:
        return None
    if df <= 30:
        return _T95[df - 1]
    if df <= 60:
        return 2.000 + (2.042 - 2.000) * (60 - df) / 30
    if df <= 120:
        return 1.980 + (2.000 - 1.980) * (120 - df) / 60
    return 1.960


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def stats(velocities, bullet_gr=None):
    """Summary of a list of velocities (m/s). SD/ES need n ≥ 3, the CI needs n ≥ 5."""
    v = [float(x) for x in velocities if x is not None]
    n = len(v)
    out = {"n": n, "avg": None, "sd": None, "sd_garmin": None, "es": None,
           "min": None, "max": None, "ci_low": None, "ci_high": None, "ke_j": None}
    if not n:
        return out
    avg = sum(v) / n
    out.update(avg=_r(avg), min=_r(min(v), 1), max=_r(max(v), 1))
    if n >= 2:
        ss = sum((x - avg) ** 2 for x in v)
        sd, sd_pop = math.sqrt(ss / (n - 1)), math.sqrt(ss / n)
        if n >= 3:
            out.update(sd=_r(sd), sd_garmin=_r(sd_pop), es=_r(max(v) - min(v), 1))
        if n >= 5:
            half = t95(n - 1) * sd / math.sqrt(n)
            out.update(ci_low=_r(avg - half), ci_high=_r(avg + half))
    if bullet_gr:
        # Mean of the per-shot energies (not the energy at the mean velocity, which is a little lower)
        out["ke_j"] = _r(sum(ke_joules(bullet_gr, x) for x in v) / n, 0)
    return out


def ke_joules(bullet_gr, v_ms):
    return 0.5 * bullet_gr * GRAIN_KG * v_ms ** 2


def slope(points):
    """Least-squares slope of [(x, y)], or None with fewer than 3 points or no x spread."""
    pts = [(float(x), float(y)) for x, y in points if x is not None and y is not None]
    if len(pts) < 3:
        return None
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx


def temp_sensitivity(day_points, min_days=3, min_span=10.0):
    """m/s per °C from [(temp_c, avg)], only when the days span enough temperature."""
    pts = [(t, a) for t, a in day_points if t is not None and a is not None]
    if len(pts) < min_days:
        return None
    temps = [t for t, _ in pts]
    if max(temps) - min(temps) < min_span:
        return None
    s = slope(pts)
    return None if s is None else round(s, 3)
