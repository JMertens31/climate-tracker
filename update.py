#!/usr/bin/env python3
"""Daily update for the global temperature tracker.

1. Download the Climate Pulse ERA5 daily global 2 m temperature series.
2. Validate it (header, continuous dates, freshness).
3. Add the stored pre-industrial (1850-1900) offset for each calendar day
   from offsets.csv. The offsets are NOT recalculated here.
4. Compute 30/90/180/365-day running means, the calendar-year-to-date mean,
   the mean of every complete calendar year, linear and exponential trends
   since 1991, days above 1.5 C per year and each year by calendar day.
5. Write the dashboard (site/index.html), the full computed series
   (site/data/series.csv), annual means (site/data/annual.csv) and the latest values (site/data/latest.json and
   data/latest.json, which is committed to keep the repo active).

Standard library only. Exits non-zero on any data problem so the scheduled
run fails loudly and GitHub sends a failure email.

Usage:
    python update.py                 # download and build
    python update.py --input FILE    # build from a local copy of the CSV
"""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import html
import json
import math
import shutil
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE_URL = "https://sites.ecmwf.int/data/climatepulse/data/series/era5_daily_series_2t_global.csv"
EXPECTED_HEADER = ["date", "2t", "clim_91-20", "ano_91-20", "status"]
OFFSETS_FILE = ROOT / "offsets.csv"
SITE_DIR = ROOT / "site"
REPO_LATEST = ROOT / "data" / "latest.json"

WINDOWS = [30, 90, 180, 365]   # rolling windows, days
TREND_START = dt.date(1991, 1, 1)   # running-means chart and trend fits start here (matches 1991-2020)
TREND_SAMPLE_DAYS = 7          # plot every 7th day on the long running-means chart (keeps the page light)
DAILY_DAYS = 60                # days shown in the daily chart
MAX_AGE_DAYS = 10              # fail if the newest value is older than this
PARIS_LEVEL = 1.5
WARMEST_LIST = 5               # rows in the warmest-years table
TWO_DEGREES = 2.0
# Reference periods drawn as dashed lines, as (label, first year, last year).
# Each must lie inside the ERA5 record (1940 onward). Its level is the mean of
# the daily pre-industrial anomalies over the period, computed from the data.
BASELINES = [("1991\u20132020 average", 1991, 2020)]


class DataError(Exception):
    """Raised when the downloaded data fails a check."""


# --------------------------------------------------------------------------- input

def download(url: str, attempts: int = 3) -> str:
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "global-temperature-tracker/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # network errors: retry with backoff
            last = e
            time.sleep(10 * (i + 1))
    raise DataError(f"Download failed after {attempts} attempts: {last}")


def parse_series(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        raise DataError("No data rows found")
    header = [h.strip() for h in lines[0].split(",")]
    if header != EXPECTED_HEADER:
        raise DataError(f"Unexpected header {header}; expected {EXPECTED_HEADER}")
    rows = []
    for ln in csv.reader(lines[1:]):
        try:
            rows.append({
                "date": dt.date.fromisoformat(ln[0].strip()),
                "t2m": float(ln[1]),
                "clim": float(ln[2]),
                "ano": float(ln[3]),
                "status": ln[4].strip().upper(),
            })
        except (ValueError, IndexError) as e:
            raise DataError(f"Bad row {ln!r}: {e}") from e
    return rows


def validate(rows: list[dict], today: dt.date) -> None:
    if len(rows) < 365 * 6:
        raise DataError(f"Only {len(rows)} rows; expected decades of data")
    for a, b in zip(rows, rows[1:]):
        if (b["date"] - a["date"]).days != 1:
            raise DataError(f"Dates not continuous between {a['date']} and {b['date']}")
    for r in rows:
        if r["status"] not in ("FINAL", "PRELIMINARY"):
            raise DataError(f"Unknown status {r['status']!r} on {r['date']}")
        if not -5 < r["ano"] < 5:
            raise DataError(f"Implausible anomaly {r['ano']} on {r['date']}")
    age = (today - rows[-1]["date"]).days
    if age > MAX_AGE_DAYS:
        raise DataError(f"Newest value is {rows[-1]['date']}, {age} days old; source may have stopped updating")


def load_offsets(path: Path = OFFSETS_FILE) -> dict[str, float]:
    with path.open() as f:
        offsets = {r["month_day"]: float(r["offset_c"]) for r in csv.DictReader(f)}
    if len(offsets) != 366:
        raise DataError(f"{path.name} has {len(offsets)} days; expected 366")
    return offsets


# --------------------------------------------------------------------------- compute

def add_preindustrial(rows: list[dict], offsets: dict[str, float]) -> None:
    for r in rows:
        r["offset"] = offsets[r["date"].strftime("%m-%d")]
        r["pi"] = r["ano"] + r["offset"]


def rolling_means(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def one_year_earlier(d: dt.date) -> dt.date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 Feb -> 28 Feb
        return d.replace(year=d.year - 1, day=28)


def ytd_mean(rows: list[dict], end: dt.date) -> tuple[float, dt.date]:
    start = dt.date(end.year, 1, 1)
    vals = [r["pi"] for r in rows if start <= r["date"] <= end]
    return sum(vals) / len(vals), start


def annual_means(rows: list[dict], latest: dt.date) -> list[dict]:
    """Mean of each COMPLETE calendar year before the year of the latest data point."""
    sums: dict[int, list] = {}
    for r in rows:
        y = r["date"].year
        if y >= latest.year:
            continue
        b = sums.setdefault(y, [0.0, 0.0, 0])
        b[0] += r["pi"]
        b[1] += r["ano"]
        b[2] += 1
    out = []
    for y in sorted(sums):
        pi_sum, ano_sum, n = sums[y]
        if n == (366 if calendar.isleap(y) else 365):   # skip a partial first year
            out.append({"year": y, "pi": pi_sum / n, "ano": ano_sum / n, "days": n})
    ranked = sorted(out, key=lambda a: a["pi"], reverse=True)
    for i, a in enumerate(ranked, start=1):
        a["rank"] = i
    return out


def year_context(annual: list[dict]) -> list[dict]:
    """For each complete year, how it ranked when it ended and how later years compare.

    Values are compared at the 0.001 degC precision shown on the page, so the
    sentences never contradict the displayed numbers. A year only counts as
    warmer or colder if it differs at that precision; equal values are ties.
    """
    vals = [round(a["pi"], 3) for a in annual]
    out = []
    for i, a in enumerate(annual):
        v = vals[i]
        before, after = vals[:i], vals[i + 1:]
        out.append({
            "year": a["year"],
            "value": v,
            # Rank among all years from the start of the record up to and including this one.
            "rank_then": 1 + sum(x > v for x in before),
            "years_then": i + 1,
            "tied_then": any(x == v for x in before),
            # Later complete years.
            "later_years": len(after),
            "later_warmer": sum(x > v for x in after),
            "later_colder": sum(x < v for x in after),
            # Rank among all complete years in the record today.
            "rank_now": 1 + sum(x > v for x in vals[:i] + after),
            "years_now": len(vals),
        })
    return out


def decimal_year(d: dt.date) -> float:
    start = dt.date(d.year, 1, 1)
    days = 366 if calendar.isleap(d.year) else 365
    return d.year + (d - start).days / days


def from_decimal_year(x: float) -> dt.date:
    y = int(math.floor(x))
    days = 366 if calendar.isleap(y) else 365
    return dt.date(y, 1, 1) + dt.timedelta(days=min(days - 1, int(round((x - y) * days))))


def fit_trends(rows: list[dict], start: dt.date = TREND_START, threshold: float = PARIS_LEVEL) -> dict:
    """Least-squares trends through the daily pre-industrial anomalies from `start` to the latest day.

    linear:       y = a + b*t
    exponential:  y = A*exp(k*t), fitted by least squares on y itself. (A spreadsheet
                  "exponential trendline" fits ln(y) instead, which fails when any day is
                  at or below zero, as some were after the 1991 Pinatubo eruption.)
    t is in years since `start`. Each fit reports its value on the latest day and the
    date it reaches (or reached) `threshold`.
    """
    pts = [r for r in rows if r["date"] >= start]
    t0 = decimal_year(start)
    ts = [decimal_year(r["date"]) - t0 for r in pts]
    ys = [r["pi"] for r in pts]
    t_end = ts[-1]

    def ols(xs, vs):
        n = len(xs)
        mx, mv = sum(xs) / n, sum(vs) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (v - mv) for x, v in zip(xs, vs)) / sxx
        return mv - slope * mx, slope

    def crossing(t_star):
        """Date the fitted line reaches the threshold: {"date", "passed"}; None if it never
        does, or was already above it when the fit period began."""
        if t_star is None or t_star < 0:
            return None
        return {"date": from_decimal_year(t0 + t_star).isoformat(), "passed": t_star <= t_end}

    a, b = ols(ts, ys)
    linear = {"a": a, "b": b, "per_decade": b * 10, "now": a + b * t_end,
              "reaches_threshold": crossing((threshold - a) / b if b > 0 else None)}

    # Exponential: least squares directly on y (not on ln y), so days at or below zero still count.
    # For a given growth rate k the best A has a closed form; k is found by golden-section search.
    def best_a(k):
        e = [math.exp(k * t) for t in ts]
        return sum(y * w for y, w in zip(ys, e)) / sum(w * w for w in e)

    def sse(k):
        A = best_a(k)
        return sum((y - A * math.exp(k * t)) ** 2 for y, t in zip(ys, ts))

    lo, hi = -0.2, 0.2
    g = (math.sqrt(5) - 1) / 2
    c, d = hi - g * (hi - lo), lo + g * (hi - lo)
    fc, fd = sse(c), sse(d)
    for _ in range(80):
        if fc < fd:
            hi, d, fd = d, c, fc
            c = hi - g * (hi - lo)
            fc = sse(c)
        else:
            lo, c, fc = c, d, fd
            d = lo + g * (hi - lo)
            fd = sse(d)
    k = (lo + hi) / 2
    A = best_a(k)
    exponential = None
    if A > 0:
        now = A * math.exp(k * t_end)
        exponential = {"A": A, "k": k, "now": now, "per_decade_now": now * (math.exp(10 * k) - 1),
                       "reaches_threshold": crossing(math.log(threshold / A) / k if k > 0 else None)}

    return {"start": start.isoformat(), "t0": t0, "linear": linear, "exponential": exponential}


def days_above(rows: list[dict], threshold: float = PARIS_LEVEL) -> dict:
    """Days per calendar year with a daily anomaly above `threshold`, from the first year with one."""
    counts: dict[int, int] = {}
    first_day = None
    for r in rows:
        if r["pi"] > threshold:
            counts[r["date"].year] = counts.get(r["date"].year, 0) + 1
            first_day = first_day or r["date"]
    if first_day is None:
        return {"years": [], "counts": [], "first_day": None}
    years = list(range(first_day.year, rows[-1]["date"].year + 1))
    return {"years": years, "counts": [counts.get(y, 0) for y in years], "first_day": first_day.isoformat()}


def by_calendar_day(rows: list[dict]) -> dict:
    """One series per year, indexed by calendar day (366 slots; 29 Feb empty in other years)."""
    slots = [(dt.date(2024, 1, 1) + dt.timedelta(days=i)).strftime("%m-%d") for i in range(366)]
    pos = {md: i for i, md in enumerate(slots)}
    series: dict[int, list] = {}
    for r in rows:
        series.setdefault(r["date"].year, [None] * 366)[pos[r["date"].strftime("%m-%d")]] = round(r["pi"], 2)
    years = sorted(series)
    # 1991-2020 average for each calendar day (only if the whole period is present).
    base = None
    if all(y in series for y in range(1991, 2021)):
        base = []
        for i in range(366):
            vals = [series[y][i] for y in range(1991, 2021) if series[y][i] is not None]
            base.append(round(sum(vals) / len(vals), 3))
    return {"slots": slots, "years": years, "values": [series[y] for y in years], "base_1991_2020": base}


def baseline_levels(rows: list[dict]) -> list[dict]:
    """Level of each reference period on the 1850-1900 scale (degC)."""
    out = []
    for label, y0, y1 in BASELINES:
        vals = [r["pi"] for r in rows if y0 <= r["date"].year <= y1]
        expected = (dt.date(y1, 12, 31) - dt.date(y0, 1, 1)).days + 1
        if len(vals) != expected:   # period not fully covered: skip the line rather than draw a wrong one
            print(f"WARNING: baseline {y0}-{y1} not fully covered by the data; line omitted", file=sys.stderr)
            continue
        out.append({"label": label, "start": y0, "end": y1, "value": sum(vals) / len(vals)})
    return out


def compute(rows: list[dict]) -> dict:
    pis = [r["pi"] for r in rows]
    means = {n: rolling_means(pis, n) for n in WINDOWS}
    for n in WINDOWS:
        for r, m in zip(rows, means[n]):
            r[f"m{n}"] = m

    idx = {r["date"]: i for i, r in enumerate(rows)}
    last = rows[-1]
    prev_date = one_year_earlier(last["date"])
    prev = rows[idx[prev_date]] if prev_date in idx else None

    tiles = []
    for n in WINDOWS:
        start = rows[len(rows) - n]["date"]
        tiles.append({
            "key": f"{n}d",
            "label": f"{n}-day mean" if n != 365 else "365-day mean",
            "value": last[f"m{n}"],
            "start": start.isoformat(),
            "end": last["date"].isoformat(),
            "change_1y": (last[f"m{n}"] - prev[f"m{n}"]) if prev and prev[f"m{n}"] is not None else None,
        })
    ytd, ytd_start = ytd_mean(rows, last["date"])
    ytd_prev = ytd_mean(rows, prev_date)[0] if prev else None
    tiles.append({
        "key": "ytd",
        "label": f"{last['date'].year} year to date",
        "value": ytd,
        "start": ytd_start.isoformat(),
        "end": last["date"].isoformat(),
        "change_1y": (ytd - ytd_prev) if ytd_prev is not None else None,
    })

    # Chart data
    # Every TREND_SAMPLE_DAYS-th day back from the latest, so the newest day is always plotted.
    trend = [r for r in rows[::-1][::TREND_SAMPLE_DAYS]
             if r["date"] >= TREND_START and r["m365"] is not None][::-1]
    fits = fit_trends(rows)
    daily = rows[-DAILY_DAYS:]
    annual = annual_means(rows, last["date"])

    return {
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "latest_date": last["date"].isoformat(),
        "latest_status": last["status"],
        "latest_daily": last["pi"],
        "tiles": tiles,
        "trend": {
            "dates": [r["date"].isoformat() for r in trend],
            **{f"m{n}": [round(r[f"m{n}"], 3) for r in trend] for n in WINDOWS},
            "linear": [round(fits["linear"]["a"] + fits["linear"]["b"] * (decimal_year(r["date"]) - fits["t0"]), 3)
                       for r in trend],
            "exponential": None if not fits["exponential"] else
                [round(fits["exponential"]["A"] * math.exp(fits["exponential"]["k"] * (decimal_year(r["date"]) - fits["t0"])), 3)
                 for r in trend],
        },
        "fits": fits,
        "days_above": days_above(rows),
        "calendar": by_calendar_day(rows),
        "daily": {
            "dates": [r["date"].isoformat() for r in daily],
            "values": [round(r["pi"], 3) for r in daily],
            "status": [r["status"] for r in daily],
        },
        "annual_rows": annual,
        "baselines": baseline_levels(rows),
        "annual": {
            "years": [a["year"] for a in annual],
            "values": [round(a["pi"], 3) for a in annual],
        },
        "year_context": year_context(annual),
    }


# --------------------------------------------------------------------------- output

def fmt_signed(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{digits}f}".replace("-", "−")


def ordinal(n: int) -> str:
    if n == 1:
        return ""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fmt_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')} {d.year}"


def fmt_range(a: str, b: str) -> str:
    da, db = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    if da.year == db.year:
        return f"{da.day} {da.strftime('%b')} – {db.day} {db.strftime('%b')} {db.year}"
    return f"{fmt_date(a)} – {fmt_date(b)}"


def write_series_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "status", "t2m_c", "anomaly_1991_2020_c", "offset_1850_1900_c",
                    "anomaly_1850_1900_c"] + [f"mean_{n}d_1850_1900_c" for n in WINDOWS])
        for r in rows:
            w.writerow([r["date"].isoformat(), r["status"], f"{r['t2m']:.3f}", f"{r['ano']:.3f}",
                        f"{r['offset']:.4f}", f"{r['pi']:.3f}"]
                       + ["" if r[f"m{n}"] is None else f"{r[f'm{n}']:.3f}" for n in WINDOWS])


def write_annual_csv(annual: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["year", "days", "anomaly_1991_2020_c", "anomaly_1850_1900_c", "rank_warmest"])
        for a in annual:
            w.writerow([a["year"], a["days"], f"{a['ano']:.3f}", f"{a['pi']:.3f}", a["rank"]])


def latest_json(result: dict) -> dict:
    annual = result["annual_rows"]
    last_year = annual[-1] if annual else None
    warmest = min(annual, key=lambda a: a["rank"]) if annual else None
    return {
        "latest_date": result["latest_date"],
        "latest_status": result["latest_status"],
        "generated_utc": result["generated_utc"],
        "baseline": "1850-1900 (C3S daily offset applied to ERA5 1991-2020 anomalies)",
        "units": "degC",
        "latest_daily": round(result["latest_daily"], 3),
        "means": {t["key"]: {"value": round(t["value"], 3), "start": t["start"], "end": t["end"],
                             "change_vs_1y_earlier": None if t["change_1y"] is None else round(t["change_1y"], 3)}
                  for t in result["tiles"]},
        "last_complete_year": None if not last_year else
            {"year": last_year["year"], "value": round(last_year["pi"], 3), "rank_warmest": last_year["rank"]},
        "warmest_year": None if not warmest else {"year": warmest["year"], "value": round(warmest["pi"], 3)},
        "trend_since_1991": {
            "linear": {"value_now": round(result["fits"]["linear"]["now"], 3),
                       "per_decade": round(result["fits"]["linear"]["per_decade"], 3),
                       "reaches_1_5": result["fits"]["linear"]["reaches_threshold"]},
            "exponential": None if not result["fits"]["exponential"] else
                {"value_now": round(result["fits"]["exponential"]["now"], 3),
                 "per_decade_now": round(result["fits"]["exponential"]["per_decade_now"], 3),
                 "reaches_1_5": result["fits"]["exponential"]["reaches_threshold"]},
        },
        "days_above_1_5_this_year": (result["days_above"]["counts"] or [None])[-1],
        "source": SOURCE_URL,
    }


def crossing_text(c: dict | None, lead: str) -> str:
    if not c:
        return "."
    d = dt.date.fromisoformat(c["date"])
    when = f"{d.strftime('%B')} {d.year}"
    return f"{lead} {'passed' if c['passed'] else 'reaches'} 1.5 \u00b0C in {when}."


def trend_text(fits: dict, latest_iso: str) -> str:
    lin, exp_ = fits["linear"], fits["exponential"]
    when = fmt_date(latest_iso)
    out = (f"Fitted to every day since {fmt_date(fits['start'])}, the linear trend puts long-term warming at "
           f"{fmt_signed(lin['now'])} \u00b0C on {when}, rising {lin['per_decade']:.2f} \u00b0C per decade")
    out += crossing_text(lin["reaches_threshold"], "; on that line it")
    if exp_:
        out += (f" The exponential fit puts it at {fmt_signed(exp_['now'])} \u00b0C, currently rising "
                f"{exp_['per_decade_now']:.2f} \u00b0C per decade")
        out += crossing_text(exp_["reaches_threshold"], ", and")
    return out


def days_above_text(above: dict, latest_iso: str) -> str:
    if not above["years"]:
        return "No day in the record has been above 1.5 \u00b0C."
    years, counts = above["years"], above["counts"]
    current = dt.date.fromisoformat(latest_iso).year
    complete = [(y, c) for y, c in zip(years, counts) if y < current]
    out = f"The first day above 1.5 \u00b0C in the record was {fmt_date(above['first_day'])}."
    if complete:
        y_max, c_max = max(complete, key=lambda yc: (yc[1], yc[0]))
        out += f" {y_max} had the most, {c_max} days."
    if years[-1] == current:
        n = counts[-1]
        out += f" {current} has had {n} {'day' if n == 1 else 'days'} so far, to {fmt_date(latest_iso)}."
    return out


def render_html(result: dict) -> str:
    tiles = result["tiles"]
    base = result["baselines"][0] if result["baselines"] else None
    headline = next(t for t in tiles if t["key"] == "365d")
    now_sentence = f"The 365-day mean stands at {fmt_signed(headline['value'])} \u00b0C"
    if base:
        now_sentence += (f", {abs(headline['value'] - base['value']):.2f} \u00b0C "
                         f"{'above' if headline['value'] >= base['value'] else 'below'} the "
                         f"{base['start']}\u2013{base['end']} average")
    now_sentence += "."
    changes = " \u00b7 ".join(f"{t['label'].replace(' mean', '')} {fmt_signed(t['change_1y'])} \u00b0C"
                               for t in tiles)
    now = {
        "labels": [t["label"].replace(" mean", "") for t in tiles],
        "keys": [t["key"] for t in tiles],
        "values": [round(t["value"], 3) for t in tiles],
        "periods": [fmt_range(t["start"], t["end"]) for t in tiles],
        "changes": [None if t["change_1y"] is None else round(t["change_1y"], 3) for t in tiles],
    }
    annual = result["annual_rows"]
    ranked = sorted(annual, key=lambda a: a["rank"])
    warmest, last_year = ranked[0], annual[-1]
    if last_year is warmest:
        sentence = f"{last_year['year']} was the warmest year in the record, at {fmt_signed(last_year['pi'])} \u00b0C."
    else:
        sentence = (f"{warmest['year']} is the warmest year in the record, at {fmt_signed(warmest['pi'])} \u00b0C. "
                    f"{last_year['year']} was {fmt_signed(last_year['pi'])} \u00b0C, "
                    f"the {ordinal(last_year['rank'])} warmest.")
    table_rows = "\n".join(
        f"<tr><td>{a['rank']}</td><td>{a['year']}</td><td>{fmt_signed(a['pi'])}</td><td>{fmt_signed(a['ano'])}</td></tr>"
        for a in ranked[:WARMEST_LIST])
    trend_sentence = trend_text(result["fits"], result["latest_date"])
    above_sentence = days_above_text(result["days_above"], result["latest_date"])
    cal = result["calendar"]
    current_year = dt.date.fromisoformat(result["latest_date"]).year
    data_json = json.dumps({"trend": result["trend"], "daily": result["daily"], "annual": result["annual"],
                            "above": result["days_above"], "calendar": cal, "current_year": current_year,
                            "record_year": warmest["year"], "context": result["year_context"],
                            "now": now, "paris": PARIS_LEVEL, "two": TWO_DEGREES,
                            "baselines": [{"label": b["label"], "value": round(b["value"], 3)}
                                          for b in result["baselines"]],
                            "windows": WINDOWS}).replace("</", "<\\/")
    prelim = " (preliminary)" if result["latest_status"] == "PRELIMINARY" else ""
    year = dt.date.fromisoformat(result["latest_date"]).year
    return (HTML_TEMPLATE
            .replace("{{NOW_SENTENCE}}", html.escape(now_sentence))
            .replace("{{NOW_CHANGES}}", html.escape(changes))
            .replace("{{BASE_NOTE}}", f" and the 1991&ndash;2020 average ({base['value']:+.2f} &deg;C)" if base else "")
            .replace("{{LATEST}}", html.escape(fmt_date(result["latest_date"]) + prelim))
            .replace("{{GENERATED}}", html.escape(result["generated_utc"]))
            .replace("{{YEAR}}", str(year))
            .replace("{{SOURCE_URL}}", SOURCE_URL)
            .replace("{{TREND_FROM}}", str(TREND_START.year))
            .replace("{{TREND_SENTENCE}}", html.escape(trend_sentence))
            .replace("{{ABOVE_SENTENCE}}", html.escape(above_sentence))
            .replace("{{ABOVE_FIRST}}", str(result["days_above"]["years"][0]) if result["days_above"]["years"] else "")
            .replace("{{CAL_FIRST}}", str(cal["years"][0]))
            .replace("{{CAL_PREV}}", str(current_year - 1))
            .replace("{{CURRENT_YEAR}}", str(current_year))
            .replace("{{RECORD_YEAR}}", str(warmest["year"]))
            .replace("{{DAILY_DAYS}}", str(DAILY_DAYS))
            .replace("{{ANNUAL_FIRST}}", str(annual[0]["year"]))
            .replace("{{ANNUAL_LAST}}", str(last_year["year"]))
            .replace("{{ANNUAL_SENTENCE}}", html.escape(sentence))
            .replace("{{WARMEST_N}}", str(min(WARMEST_LIST, len(ranked))))
            .replace("{{WARMEST_ROWS}}", table_rows)
            .replace("{{DATA}}", data_json))


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Global Temperature Tracker</title>
<meta name="description" content="Daily running means of global surface air temperature above the 1850-1900 pre-industrial baseline, from Copernicus ERA5.">
<style>
  :root {
    --ink: #1F2937; --muted: #4B5563; --accent: #2563EB; --white: #FFFFFF;
    --panel: #F3F4F6; --tint: #EFF6FF; --track: #D1D5DB;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--white); color: var(--ink);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; font-size: 15px; line-height: 1.55; }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 32px 16px 48px; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
  .site-name { font-weight: 700; font-size: 16px; }
  h1 { font-size: 30px; font-weight: 700; line-height: 1.2; margin: 32px 0 8px; }
  h1 .hl { color: var(--accent); }
  h2 { font-size: 20px; font-weight: 700; margin: 0 0 4px; }
  .lead { margin: 0 0 24px; max-width: 760px; }
  .meta { font-size: 13px; }
  .small { font-size: 13px; color: var(--muted); margin: 10px 0 0; }
  .card { margin: 0 0 40px; }
  .card p { margin: 0 0 12px; font-size: 13.5px; }
  .chart-box { position: relative; height: 360px; }
  .chart-box.short { height: 280px; }
  .chart-box.tall { height: 400px; }
  .note { background: var(--tint); border-radius: 16px; padding: 16px 20px; font-size: 13.5px; margin: 0 0 40px; }
  footer { border-top: 1px solid var(--track); padding-top: 16px; font-size: 12px; }
  footer p { margin: 0 0 8px; }
  a { color: var(--accent); }
  .lookup { background: var(--panel); border-radius: 16px; padding: 20px; margin-top: 24px; }
  .lookup h3 { font-size: 17px; margin: 0 0 4px; }
  .lookup .hint { font-size: 13px; color: var(--muted); margin: 0 0 12px; }
  .lookup label { font-weight: 600; margin-right: 8px; }
  .lookup select { font: inherit; font-size: 15px; padding: 6px 10px; border: 1px solid var(--track);
                   border-radius: 8px; background: var(--white); color: var(--ink); }
  .lookup-result { margin-top: 16px; }
  .lookup-value { font-size: 26px; font-weight: 800; line-height: 1.2; }
  .lookup-value span { font-size: 15px; font-weight: 400; color: var(--muted); margin-left: 6px; }
  .lookup-result ul { margin: 10px 0 0; padding-left: 20px; }
  .lookup-result li { margin-bottom: 6px; }
  .rank { border-collapse: collapse; margin-top: 20px; font-size: 13.5px; min-width: 320px; }
  .rank caption { text-align: left; font-weight: 700; margin-bottom: 6px; }
  .rank th, .rank td { padding: 6px 16px 6px 0; border-bottom: 1px solid var(--track); text-align: left; }
  .rank th { font-weight: 600; color: var(--muted); }
  .rank td:nth-child(3), .rank td:nth-child(4), .rank th:nth-child(3), .rank th:nth-child(4) { text-align: right; }
  @media (max-width: 480px) { h1 { font-size: 24px; } .chart-box { height: 300px; } .chart-box.tall { height: 380px; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="site-name">Global Temperature Tracker</div>
    <div class="meta">Latest data: <strong>{{LATEST}}</strong> &middot; Updated {{GENERATED}}</div>
  </header>

  <h1>Global Temperature <span class="hl">Above Pre-Industrial</span></h1>
  <p class="lead">Running means of daily global surface air temperature, in &deg;C above the 1850&ndash;1900 average. The 365-day mean is the headline figure; shorter windows react to weather and El Ni&ntilde;o.</p>

  <section class="card">
    <h2>Running Means Today</h2>
    <p>{{NOW_SENTENCE}} Bars show each running mean ending on the latest day; the 365-day mean is in blue. Dashed lines mark 1.5 &deg;C, 2 &deg;C{{BASE_NOTE}}.</p>
    <div class="chart-box tall"><canvas id="now" role="img" aria-label="Current 30-day, 90-day, 180-day, 365-day and year-to-date mean temperature anomalies"></canvas></div>
    <p class="small">Change from the same period a year earlier: {{NOW_CHANGES}}.</p>
  </section>

  <section class="card">
    <h2>Running Means and Long-Term Trend Since {{TREND_FROM}}</h2>
    <p>{{TREND_SENTENCE}}</p>
    <p>Coloured lines are the averages of the preceding 30, 90, 180 or 365 days. Black is a straight-line trend and purple an exponential trend, both fitted to the daily values. Dashed lines mark 1.5 &deg;C and the 1991&ndash;2020 average. The exponential fit depends on measuring from 1850&ndash;1900; it shows whether warming is speeding up, not a forecast.</p>
    <div class="chart-box tall"><canvas id="trend" role="img" aria-label="Running mean temperature anomalies and fitted trends since {{TREND_FROM}}"></canvas></div>
  </section>

  <section class="card">
    <h2>Daily Values, Last {{DAILY_DAYS}} Days</h2>
    <p>Each day's global mean anomaly. The most recent day is preliminary and may change slightly.</p>
    <div class="chart-box short"><canvas id="daily" role="img" aria-label="Daily temperature anomalies over the last {{DAILY_DAYS}} days"></canvas></div>
  </section>

  <section class="card">
    <h2>{{CURRENT_YEAR}} Against Every Year Since {{CAL_FIRST}}</h2>
    <p>Each grey line is one year from {{CAL_FIRST}} to {{CAL_PREV}}, darker for more recent years. {{RECORD_YEAR}}, the warmest complete year, is in blue and {{CURRENT_YEAR}} so far in black. The grey dashed curve is the 1991&ndash;2020 average for each date, which rises and falls with the seasons. Hover to compare a date.</p>
    <div class="chart-box tall"><canvas id="calendar" role="img" aria-label="Daily temperature anomaly by calendar day for every year since {{CAL_FIRST}}"></canvas></div>
  </section>

  <section class="card">
    <h2>Annual Averages, {{ANNUAL_FIRST}}&ndash;{{ANNUAL_LAST}}</h2>
    <p>{{ANNUAL_SENTENCE}} Each bar is a complete calendar year; the warmest is in blue and the year selected below in orange. ERA5 values before about 1950 rest on fewer observations and are less certain. <a href="data/annual.csv">Download annual values (CSV)</a>.</p>
    <div class="chart-box"><canvas id="annual" role="img" aria-label="Annual mean temperature anomalies {{ANNUAL_FIRST}} to {{ANNUAL_LAST}}"></canvas></div>

    <div class="lookup">
      <h3>How Does a Year Compare?</h3>
      <p class="hint">Choose a year, or click its bar in the chart above.</p>
      <label for="year-select">Year</label>
      <select id="year-select"></select>
      <div id="year-result" class="lookup-result" aria-live="polite"></div>
    </div>
    <table class="rank">
      <caption>Warmest {{WARMEST_N}} Years</caption>
      <thead><tr><th>Rank</th><th>Year</th><th>vs 1850&ndash;1900 (&deg;C)</th><th>vs 1991&ndash;2020 (&deg;C)</th></tr></thead>
      <tbody>
{{WARMEST_ROWS}}
      </tbody>
    </table>
  </section>

  <section class="card">
    <h2>Days Above 1.5 &deg;C Each Year</h2>
    <p>{{ABOVE_SENTENCE}} Each bar counts the days in that year whose global mean was more than 1.5 &deg;C above 1850&ndash;1900, from {{ABOVE_FIRST}}, the first year with one. The current year, still in progress, is in blue.</p>
    <div class="chart-box short"><canvas id="above" role="img" aria-label="Number of days above 1.5 degrees per year"></canvas></div>
  </section>

  <div class="note"><strong>Reading these numbers:</strong> the Paris Agreement's 1.5 &deg;C limit refers to warming averaged over about 20 years. A 30-day, 365-day or calendar-year value above 1.5 &deg;C is not, on its own, a breach of that limit.</div>

  <footer>
    <p><strong>Method.</strong> ERA5 daily global mean 2 m air temperature anomalies relative to 1991&ndash;2020, from <a href="https://pulse.climate.copernicus.eu/">Copernicus Climate Pulse</a> (<a href="{{SOURCE_URL}}">data file</a>). Converted to the 1850&ndash;1900 baseline with the C3S daily offset (0.79&ndash;0.97 &deg;C by day of year; <a href="https://climate.copernicus.eu/climate-bulletin-about-data-and-analysis">C3S method</a>). Pre-industrial values carry about &plusmn;0.1 &deg;C uncertainty from the offset. <a href="data/series.csv">Download the full daily series (CSV)</a>.</p>
    <p>Data: ERA5 daily global surface air temperature via Copernicus Climate Pulse (C3S/ECMWF). Contains modified Copernicus Climate Change Service information {{YEAR}}. Neither the European Commission nor ECMWF is responsible for any use that may be made of the Copernicus information or data it contains.</p>
  </footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
(function () {
  var D = {{DATA}};
  // Colour-blind-safe series colours (Okabe-Ito), with the headline series in dark ink.
  var C = { ink: '#1F2937', purple: '#CC79A7', blue: '#0072B2', green: '#009E73', orange: '#E69F00', red: '#D55E00', darkred: '#991B1B',
            grey: '#6B7280', accent: '#2563EB', grid: '#E5E7EB' };
  if (typeof Chart === 'undefined') {
    document.querySelectorAll('.chart-box').forEach(function (b) { b.textContent = 'Charts could not load. The numbers above are current.'; });
    return;
  }
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif';
  Chart.defaults.color = C.ink;
  var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function label(iso, withYear) {
    var p = iso.split('-');
    return Number(p[2]) + ' ' + months[Number(p[1]) - 1] + (withYear ? ' ' + p[0] : '');
  }
  function signed(v) { return (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(2) + ' °C'; }

  // Dashed reference lines across the full plot width, labelled in the right margin.
  // Lines sit behind the bars and data lines; labels are nudged apart if they would overlap.
  var font = Chart.defaults.font.family;
  var REF = { paris: { value: D.paris, label: '1.5 \u00b0C', color: C.red },
              two: { value: D.two, label: '2 \u00b0C', color: C.darkred },
              base: D.baselines.length ? { value: D.baselines[0].value, label: '1991\u20132020', color: C.grey } : null };
  function refs() { return Array.prototype.slice.call(arguments).filter(Boolean); }
  Chart.register({
    id: 'refLines',
    beforeDatasetsDraw: function (chart, args, opts) {
      var a = chart.chartArea, y = chart.scales.y, ctx = chart.ctx;
      (opts.lines || []).forEach(function (l) {
        var py = y.getPixelForValue(l.value);
        if (py < a.top - 1 || py > a.bottom + 1) { return; }
        ctx.save(); ctx.strokeStyle = l.color; ctx.lineWidth = 1.5; ctx.setLineDash([6, 5]);
        ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke(); ctx.restore();
      });
    },
    afterDraw: function (chart, args, opts) {
      var a = chart.chartArea, y = chart.scales.y, ctx = chart.ctx, placed = [];
      (opts.lines || []).slice().sort(function (p, q) { return q.value - p.value; }).forEach(function (l) {
        var py = y.getPixelForValue(l.value);
        if (py < a.top - 1 || py > a.bottom + 1) { return; }
        placed.forEach(function (q) { if (Math.abs(py - q) < 13) { py = q + 13; } });
        placed.push(py);
        ctx.save(); ctx.fillStyle = l.color; ctx.font = '600 11.5px ' + font;
        ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(l.label, a.right + 6, py); ctx.restore();
      });
    }
  });
  var refPad = { right: 78 };

  // Running means today: one bar per window, 365-day in blue, value printed above each bar.
  var nowColors = D.now.keys.map(function (k) { return k === '365d' ? C.accent : C.ink; });
  new Chart(document.getElementById('now'), {
    type: 'bar',
    data: { labels: D.now.labels, datasets: [
      { label: 'Running mean', data: D.now.values, backgroundColor: nowColors, borderRadius: 6,
        borderSkipped: false, maxBarThickness: 96 } ] },
    plugins: [{
      id: 'barValues',
      afterDatasetsDraw: function (chart) {
        var ctx = chart.ctx;
        var size = chart.width < 560 ? 12 : 15;
        chart.getDatasetMeta(0).data.forEach(function (bar, i) {
          ctx.save(); ctx.fillStyle = C.ink; ctx.font = '700 ' + size + 'px ' + font;
          ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
          ctx.fillText(signed(D.now.values[i]).replace(' °C', '\u00b0'), bar.x, bar.y - 6); ctx.restore();
        });
      }
    }],
    options: {
      maintainAspectRatio: false, animation: false, layout: { padding: refPad },
      plugins: {
        legend: { display: false },
        refLines: { lines: refs(REF.two, REF.paris, REF.base) },
        tooltip: { callbacks: {
          title: function (it) { return it[0].label + ' mean, ' + D.now.periods[it[0].dataIndex]; },
          label: function (it) {
            var c = D.now.changes[it.dataIndex];
            return [signed(it.parsed.y) + ' above 1850\u20131900'].concat(c === null ? [] : [signed(c) + ' vs a year earlier']);
          } } }
      },
      scales: {
        x: { grid: { display: false },
             ticks: { autoSkip: false, maxRotation: 0, font: { size: 13, weight: '600' },
                      callback: function (v, i) {
                        // Narrow screens: short labels only (dates are in the tooltip).
                        if (this.chart.width < 560) { return D.now.keys[i] === 'ytd' ? 'YTD' : D.now.keys[i]; }
                        return [D.now.labels[i], D.now.periods[i]];
                      } } },
        y: { min: 0, max: Math.ceil((Math.max(D.two, Math.max.apply(null, D.now.values)) + 0.2) * 10) / 10,
             grid: { color: C.grid },
             ticks: { stepSize: 0.5, callback: function (v) { return Math.abs(v * 2 - Math.round(v * 2)) < 1e-9 ? v.toFixed(1) + '\u00b0' : ''; } },
             title: { display: true, text: '\u00b0C above 1850\u20131900' } }
      }
    }
  });

  // Trend chart: 365-day in blue (headline), shorter windows in Okabe-Ito colours.
  var colors = { 365: C.accent, 180: C.green, 90: C.orange, 30: C.grey };
  var order = [365, 180, 90, 30];
  var trendSets = order.map(function (n) {
    // Shorter windows are drawn lighter so the 365-day line and the trendlines stand out.
    var col = n === 365 ? colors[n] : colors[n] + '99';
    return { label: n + '-day', data: D.trend['m' + n], borderColor: col, backgroundColor: col,
             borderWidth: n === 365 ? 2.5 : 1, pointRadius: 0, tension: 0, order: n === 365 ? 1 : 2 };
  });
  trendSets.push({ label: 'Linear trend', data: D.trend.linear, borderColor: C.ink, backgroundColor: C.ink,
                   borderWidth: 2, pointRadius: 0, order: 0 });
  if (D.trend.exponential) {
    trendSets.push({ label: 'Exponential trend', data: D.trend.exponential, borderColor: C.purple, backgroundColor: C.purple,
                     borderWidth: 2, pointRadius: 0, order: 0 });
  }
  var trendMin = Math.min.apply(null, D.trend.m30.concat(REF.base ? [REF.base.value] : []));
  new Chart(document.getElementById('trend'), {
    type: 'line',
    data: { labels: D.trend.dates, datasets: trendSets },
    options: {
      maintainAspectRatio: false, animation: false, layout: { padding: refPad },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        refLines: { lines: refs(REF.paris, REF.base) },
        legend: { position: 'bottom', labels: { usePointStyle: true, pointStyle: 'line', boxWidth: 28 } },
        tooltip: { callbacks: { title: function (it) { return label(it[0].label, true); },
                                label: function (it) { return it.dataset.label + ': ' + signed(it.parsed.y); } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 0,
             callback: function (v, i) {
               // Label the first plotted point of every 5th year.
               var y = Number(D.trend.dates[i].slice(0, 4));
               var prev = i > 0 ? Number(D.trend.dates[i - 1].slice(0, 4)) : null;
               return (y !== prev && y % 5 === 0) ? String(y) : null; } } },
        y: { suggestedMin: Math.floor((trendMin - 0.05) * 10) / 10, grid: { color: C.grid },
             ticks: { callback: function (v) { return v.toFixed(1) + '°'; } },
             title: { display: true, text: '°C above 1850–1900' } }
      }
    }
  });

  // Daily chart: dark bars, latest (preliminary) day highlighted.
  var n = D.daily.values.length;
  new Chart(document.getElementById('daily'), {
    type: 'bar',
    data: { labels: D.daily.dates, datasets: [
      { label: 'Daily anomaly', data: D.daily.values, borderRadius: 4, borderSkipped: false,
        backgroundColor: D.daily.values.map(function (_, i) { return i === n - 1 ? C.accent : C.ink; }) } ] },
    options: {
      maintainAspectRatio: false, animation: false, layout: { padding: refPad },
      plugins: {
        legend: { display: false },
        refLines: { lines: [REF.paris] },
        tooltip: { callbacks: { title: function (it) { return label(it[0].label, true); },
                                label: function (it) {
                                  var s = D.daily.status[it.dataIndex] === 'PRELIMINARY' ? ' (preliminary)' : '';
                                  return signed(it.parsed.y) + s; } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8,
             callback: function (v, i) { return label(D.daily.dates[i], false); } } },
        y: { beginAtZero: true, grid: { color: C.grid }, ticks: { callback: function (v) { return v.toFixed(1) + '°'; } },
             title: { display: true, text: '°C above 1850–1900' } }
      }
    }
  });

  // Annual chart: one bar per complete year, warmest year highlighted, selected year in orange.
  var selectedYear = null;
  function barColors() {
    return D.annual.years.map(function (y) {
      return y === selectedYear ? C.orange : (y === D.record_year ? C.accent : C.ink);
    });
  }
  var annualChart = new Chart(document.getElementById('annual'), {
    type: 'bar',
    data: { labels: D.annual.years, datasets: [
      { label: 'Annual mean', data: D.annual.values, borderRadius: 2, borderSkipped: false, categoryPercentage: 0.9, barPercentage: 0.9,
        backgroundColor: barColors() } ] },
    options: {
      maintainAspectRatio: false, animation: false, layout: { padding: refPad },
      onClick: function (evt, els) {
        var bar = els.filter(function (e) { return e.datasetIndex === 0; })[0];
        if (bar) { selectYear(D.annual.years[bar.index]); }
      },
      onHover: function (evt, els) { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
      plugins: {
        legend: { display: false },
        refLines: { lines: [REF.paris] },
        tooltip: { callbacks: { title: function (it) { return String(it[0].label); },
                                label: function (it) { return signed(it.parsed.y); } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 0,
             callback: function (v, i) { var y = D.annual.years[i]; return y % 10 === 0 ? String(y) : null; } } },
        y: { beginAtZero: true, grid: { color: C.grid }, ticks: { callback: function (v) { return v.toFixed(1) + '\u00b0'; } },
             title: { display: true, text: '\u00b0C above 1850\u20131900' } }
      }
    }
  });

  // Every year by calendar day: history in greys (older lighter), record year blue, current year black.
  var cal = D.calendar, firstY = cal.years[0], lastY = cal.years[cal.years.length - 1];
  function greyFor(y) {
    var f = (y - firstY) / Math.max(1, (D.current_year - 1 - firstY));   // 0 = oldest, 1 = newest
    var c = Math.round(225 - f * 110);                                   // 225 (light) to 115 (dark)
    return 'rgb(' + c + ',' + (c + 2) + ',' + (c + 6) + ')';
  }
  var calSets = cal.years.map(function (y, i) {
    var hi = y === D.current_year ? 'current' : (y === D.record_year ? 'record' : null);
    return { label: String(y), data: cal.values[i], spanGaps: true, pointRadius: 0, tension: 0,
             borderColor: hi === 'current' ? C.ink : (hi === 'record' ? C.accent : greyFor(y)),
             borderWidth: hi ? 2.5 : 0.8, order: hi ? 0 : 1, highlight: hi };
  });
  if (cal.base_1991_2020) {
    calSets.push({ label: '1991\u20132020 average', data: cal.base_1991_2020, pointRadius: 0, tension: 0,
                   borderColor: '#374151', borderDash: [6, 4], borderWidth: 2, order: 0, highlight: 'base' });
  }
  var monthsAt = {};
  cal.slots.forEach(function (md, i) { if (md.slice(3) === '01') { monthsAt[i] = months[Number(md.slice(0, 2)) - 1]; } });
  new Chart(document.getElementById('calendar'), {
    type: 'line',
    data: { labels: cal.slots, datasets: calSets },
    options: {
      maintainAspectRatio: false, animation: false, layout: { padding: refPad },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        refLines: { lines: refs(REF.paris) },
        tooltip: {
          filter: function (it) { return !!it.dataset.highlight; },
          itemSort: function (p, q) { return q.parsed.y - p.parsed.y; },
          callbacks: {
            title: function (it) { var md = cal.slots[it[0].dataIndex]; return Number(md.slice(3)) + ' ' + months[Number(md.slice(0, 2)) - 1]; },
            label: function (it) { return it.dataset.label + ': ' + signed(it.parsed.y); } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 0,
             callback: function (v, i) { return monthsAt[i] || null; } } },
        y: { grid: { color: C.grid }, ticks: { callback: function (v) { return v.toFixed(1) + '\u00b0'; } },
             title: { display: true, text: '\u00b0C above 1850\u20131900' } }
      }
    }
  });

  // Days above 1.5 C per year: current (incomplete) year in blue.
  if (D.above.years.length) {
    new Chart(document.getElementById('above'), {
      type: 'bar',
      data: { labels: D.above.years, datasets: [
        { label: 'Days above 1.5 \u00b0C', data: D.above.counts, borderRadius: 3, borderSkipped: false,
          backgroundColor: D.above.years.map(function (y) { return y === D.current_year ? C.accent : C.ink; }) } ] },
      options: {
        maintainAspectRatio: false, animation: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            title: function (it) { var y = Number(it[0].label); return y === D.current_year ? y + ' (so far)' : String(y); },
            label: function (it) { return it.parsed.y + (it.parsed.y === 1 ? ' day' : ' days') + ' above 1.5 \u00b0C'; } } }
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true } },
          y: { beginAtZero: true, max: 366, grid: { color: C.grid }, ticks: { stepSize: 60 },
               title: { display: true, text: 'Days' } }
        }
      }
    });
  }

  // Year lookup: turns the ranks computed in update.py into sentences.
  function ord(n) {
    var s = (n % 100 >= 11 && n % 100 <= 13) ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th');
    return n + s;
  }
  function years(n) { return n === 1 ? '1 year' : n + ' years'; }
  var first = D.annual.years[0], last = D.annual.years[D.annual.years.length - 1];

  function describe(c) {
    var out = [], tie = c.tied_then ? ' (tied)' : '', k = c.years_then - 1;
    var earlier = k === 1 ? first : (k === 2 ? 'both earlier years' : 'all ' + k + ' earlier years back to ' + first);
    // 1) Standing when the year ended.
    if (c.years_then === 1) {
      out.push(c.year + ' is the first year in the record, so there is no earlier year to compare it with.');
    } else if (c.rank_then === 1) {
      out.push('When it ended, ' + c.year + ' was the <strong>warmest year on record</strong>' + tie +
               ', beating ' + earlier + '.');
    } else if (c.rank_then === c.years_then) {
      out.push('When it ended, ' + c.year + ' was the <strong>coldest year on record</strong>' + tie +
               ', cooler than ' + earlier + '.');
    } else {
      out.push('When it ended, ' + c.year + ' was the <strong>' + ord(c.rank_then) + '-warmest year on record</strong>' + tie +
               ', of the ' + c.years_then + ' years from ' + first + ' to ' + c.year + '.');
    }
    // 2) What has happened since.
    var n = c.later_years, w = c.later_warmer, span = c.year + ' to ' + last;
    if (n === 0) {
      out.push('It is the most recent complete year, so there are no later years to compare yet.');
    } else if (w === 0) {
      out.push(n === 1 ? 'The year since has not been warmer.'
                       : 'None of the ' + n + ' years since has been warmer.');
    } else if (w === n) {
      out.push((n === 1 ? 'The year since has been warmer' : 'Every one of the ' + n + ' years since has been warmer') +
               ', making ' + c.year + ' the <strong>coldest year from ' + span + '</strong>.');
    } else {
      out.push(w + ' of the ' + years(n) + ' since ' + (w === 1 ? 'has' : 'have') + ' been warmer, making ' + c.year +
               ' the <strong>' + ord(c.later_colder + 1) + '-coldest</strong> of the ' + (n + 1) + ' years from ' + span + '.');
    }
    // 3) Where it stands today.
    if (n > 0) {
      if (c.rank_now === 1) {
        out.push('It remains the <strong>warmest year on record</strong>.');
      } else if (c.rank_now === c.years_now) {
        out.push('Today it is the coldest of all ' + c.years_now + ' complete years on record.');
      } else {
        out.push('Today it ranks ' + ord(c.rank_now) + '-warmest of all ' + c.years_now + ' complete years on record.');
      }
    }
    return out;
  }

  var select = document.getElementById('year-select'), result = document.getElementById('year-result');
  var byYear = {};
  D.context.forEach(function (c) { byYear[c.year] = c; });
  D.annual.years.slice().reverse().forEach(function (y) {
    var o = document.createElement('option'); o.value = y; o.textContent = y; select.appendChild(o);
  });
  function selectYear(y) {
    var c = byYear[y];
    if (!c) { return; }
    selectedYear = y;
    select.value = String(y);
    result.innerHTML = '<div class="lookup-value">' + signed(c.value) + '<span>above 1850\u20131900</span></div>' +
      '<ul>' + describe(c).map(function (t) { return '<li>' + t + '</li>'; }).join('') + '</ul>';
    annualChart.data.datasets[0].backgroundColor = barColors();
    annualChart.update('none');
  }
  select.addEventListener('change', function () { selectYear(Number(select.value)); });
  selectYear(last);
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- main

def build(text: str, today: dt.date | None = None, site_dir: Path = SITE_DIR,
          repo_latest: Path = REPO_LATEST) -> dict:
    rows = parse_series(text)
    validate(rows, today or dt.datetime.now(dt.timezone.utc).date())
    add_preindustrial(rows, load_offsets())
    result = compute(rows)

    if site_dir.exists():
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text(render_html(result), encoding="utf-8")
    write_series_csv(rows, site_dir / "data" / "series.csv")
    write_annual_csv(result["annual_rows"], site_dir / "data" / "annual.csv")
    latest = json.dumps(latest_json(result), indent=2) + "\n"
    (site_dir / "data" / "latest.json").write_text(latest, encoding="utf-8")
    repo_latest.parent.mkdir(parents=True, exist_ok=True)
    repo_latest.write_text(latest, encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, help="read the CSV from a local file instead of downloading")
    args = ap.parse_args()
    try:
        text = args.input.read_text(encoding="utf-8") if args.input else download(SOURCE_URL)
        result = build(text)
    except DataError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Latest data {result['latest_date']} ({result['latest_status'].lower()})")
    for t in result["tiles"]:
        print(f"  {t['label']:<22} {t['value']:+.3f} degC  ({t['start']} to {t['end']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
