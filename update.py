#!/usr/bin/env python3
"""Daily update for the global temperature tracker.

1. Download the Climate Pulse ERA5 daily global 2 m temperature series.
2. Validate it (header, continuous dates, freshness).
3. Add the stored pre-industrial (1850-1900) offset for each calendar day
   from offsets.csv. The offsets are NOT recalculated here.
4. Compute 30/90/180/365-day running means, the calendar-year-to-date mean
   and the mean of every complete calendar year.
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
TREND_YEARS = 5                # history shown in the trend chart
DAILY_DAYS = 60                # days shown in the daily chart
MAX_AGE_DAYS = 10              # fail if the newest value is older than this
PARIS_LEVEL = 1.5
WARMEST_LIST = 5               # rows in the warmest-years table


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
    trend_start = last["date"]
    for _ in range(TREND_YEARS):
        trend_start = one_year_earlier(trend_start)
    trend = [r for r in rows if r["date"] > trend_start and r["m365"] is not None]
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
        },
        "daily": {
            "dates": [r["date"].isoformat() for r in daily],
            "values": [round(r["pi"], 3) for r in daily],
            "status": [r["status"] for r in daily],
        },
        "annual_rows": annual,
        "annual": {
            "years": [a["year"] for a in annual],
            "values": [round(a["pi"], 3) for a in annual],
        },
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
        "source": SOURCE_URL,
    }


def render_html(result: dict) -> str:
    tiles_html = []
    for t in result["tiles"]:
        main = " tile--main" if t["key"] == "365d" else ""
        tiles_html.append(
            f'<div class="tile{main}">'
            f'<div class="tile-label">{html.escape(t["label"])}</div>'
            f'<div class="tile-value">{fmt_signed(t["value"])}<span class="unit">°C</span></div>'
            f'<div class="tile-period">{html.escape(fmt_range(t["start"], t["end"]))}</div>'
            f'<div class="tile-change">{fmt_signed(t["change_1y"])} °C vs a year earlier</div>'
            f'</div>'
        )
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
    data_json = json.dumps({"trend": result["trend"], "daily": result["daily"], "annual": result["annual"],
                            "record_year": warmest["year"], "paris": PARIS_LEVEL,
                            "windows": WINDOWS}).replace("</", "<\\/")
    prelim = " (preliminary)" if result["latest_status"] == "PRELIMINARY" else ""
    year = dt.date.fromisoformat(result["latest_date"]).year
    return (HTML_TEMPLATE
            .replace("{{TILES}}", "\n".join(tiles_html))
            .replace("{{LATEST}}", html.escape(fmt_date(result["latest_date"]) + prelim))
            .replace("{{GENERATED}}", html.escape(result["generated_utc"]))
            .replace("{{YEAR}}", str(year))
            .replace("{{SOURCE_URL}}", SOURCE_URL)
            .replace("{{TREND_YEARS}}", str(TREND_YEARS))
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
  .tiles { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 16px; margin: 8px 0 40px; }
  .tile { background: var(--panel); border-radius: 16px; padding: 18px 18px 16px; }
  .tile--main { background: var(--ink); color: var(--white); }
  .tile-label { font-weight: 600; font-size: 14px; }
  .tile-value { font-weight: 800; font-size: 34px; line-height: 1.15; margin: 6px 0 4px; white-space: nowrap; }
  .tile-value .unit { font-size: 18px; font-weight: 700; margin-left: 2px; }
  .tile-period, .tile-change { font-size: 12.5px; }
  .card { margin: 0 0 40px; }
  .card p { margin: 0 0 12px; font-size: 13.5px; }
  .chart-box { position: relative; height: 360px; }
  .chart-box.short { height: 280px; }
  .note { background: var(--tint); border-radius: 16px; padding: 16px 20px; font-size: 13.5px; margin: 0 0 40px; }
  footer { border-top: 1px solid var(--track); padding-top: 16px; font-size: 12px; }
  footer p { margin: 0 0 8px; }
  a { color: var(--accent); }
  .rank { border-collapse: collapse; margin-top: 20px; font-size: 13.5px; min-width: 320px; }
  .rank caption { text-align: left; font-weight: 700; margin-bottom: 6px; }
  .rank th, .rank td { padding: 6px 16px 6px 0; border-bottom: 1px solid var(--track); text-align: left; }
  .rank th { font-weight: 600; color: var(--muted); }
  .rank td:nth-child(3), .rank td:nth-child(4), .rank th:nth-child(3), .rank th:nth-child(4) { text-align: right; }
  @media (max-width: 900px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 480px) { h1 { font-size: 24px; } .tile-value { font-size: 28px; } .chart-box { height: 300px; } }
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

  <section class="tiles">
{{TILES}}
  </section>

  <section class="card">
    <h2>Running Means, Last {{TREND_YEARS}} Years</h2>
    <p>Each line is the average of the preceding 30, 90, 180 or 365 days. Dashed line: 1.5 &deg;C.</p>
    <div class="chart-box"><canvas id="trend" role="img" aria-label="Running mean temperature anomalies over the last {{TREND_YEARS}} years"></canvas></div>
  </section>

  <section class="card">
    <h2>Daily Values, Last {{DAILY_DAYS}} Days</h2>
    <p>Each day's global mean anomaly. The most recent day is preliminary and may change slightly.</p>
    <div class="chart-box short"><canvas id="daily" role="img" aria-label="Daily temperature anomalies over the last {{DAILY_DAYS}} days"></canvas></div>
  </section>

  <section class="card">
    <h2>Annual Averages, {{ANNUAL_FIRST}}&ndash;{{ANNUAL_LAST}}</h2>
    <p>{{ANNUAL_SENTENCE}} Each bar is a complete calendar year; the warmest is highlighted. ERA5 values before about 1950 rest on fewer observations and are less certain. <a href="data/annual.csv">Download annual values (CSV)</a>.</p>
    <div class="chart-box"><canvas id="annual" role="img" aria-label="Annual mean temperature anomalies {{ANNUAL_FIRST}} to {{ANNUAL_LAST}}"></canvas></div>
    <table class="rank">
      <caption>Warmest {{WARMEST_N}} Years</caption>
      <thead><tr><th>Rank</th><th>Year</th><th>vs 1850&ndash;1900 (&deg;C)</th><th>vs 1991&ndash;2020 (&deg;C)</th></tr></thead>
      <tbody>
{{WARMEST_ROWS}}
      </tbody>
    </table>
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
  var C = { ink: '#1F2937', blue: '#0072B2', green: '#009E73', orange: '#E69F00', red: '#D55E00', accent: '#2563EB', grid: '#E5E7EB' };
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

  // Trend chart: 365-day in dark ink (headline), shorter windows in Okabe-Ito colours.
  var colors = { 365: C.ink, 180: C.blue, 90: C.green, 30: C.orange };
  var order = [365, 180, 90, 30];
  var trendSets = order.map(function (n) {
    return { label: n + '-day', data: D.trend['m' + n], borderColor: colors[n], backgroundColor: colors[n],
             borderWidth: n === 365 ? 3 : 1.5, pointRadius: 0, tension: 0, order: n === 365 ? 0 : 1 };
  });
  trendSets.push({ label: '1.5 °C', data: D.trend.dates.map(function () { return D.paris; }),
                   borderColor: C.red, borderDash: [6, 5], borderWidth: 1.5, pointRadius: 0, order: 2 });
  new Chart(document.getElementById('trend'), {
    type: 'line',
    data: { labels: D.trend.dates, datasets: trendSets },
    options: {
      maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom', labels: { usePointStyle: true, pointStyle: 'line', boxWidth: 28 } },
        tooltip: { callbacks: { title: function (it) { return label(it[0].label, true); },
                                label: function (it) { return it.dataset.label + ': ' + signed(it.parsed.y); } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 0,
             callback: function (v, i) { var d = D.trend.dates[i]; return d.slice(5) === '01-01' ? d.slice(0, 4) : null; } } },
        y: { grid: { color: C.grid }, ticks: { callback: function (v) { return v.toFixed(1) + '°'; } },
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
        backgroundColor: D.daily.values.map(function (_, i) { return i === n - 1 ? C.accent : C.ink; }) },
      { type: 'line', label: '1.5 °C', data: D.daily.dates.map(function () { return D.paris; }),
        borderColor: C.red, borderDash: [6, 5], borderWidth: 1.5, pointRadius: 0 } ] },
    options: {
      maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { title: function (it) { return label(it[0].label, true); },
                                label: function (it) {
                                  if (it.datasetIndex === 1) return '1.5 °C reference';
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

  // Annual chart: one bar per complete year, warmest year highlighted.
  new Chart(document.getElementById('annual'), {
    type: 'bar',
    data: { labels: D.annual.years, datasets: [
      { label: 'Annual mean', data: D.annual.values, borderRadius: 2, borderSkipped: false, categoryPercentage: 0.9, barPercentage: 0.9,
        backgroundColor: D.annual.years.map(function (y) { return y === D.record_year ? C.accent : C.ink; }) },
      { type: 'line', label: '1.5 \u00b0C', data: D.annual.years.map(function () { return D.paris; }),
        borderColor: C.red, borderDash: [6, 5], borderWidth: 1.5, pointRadius: 0 } ] },
    options: {
      maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { title: function (it) { return String(it[0].label); },
                                label: function (it) { return it.datasetIndex === 1 ? '1.5 \u00b0C reference' : signed(it.parsed.y); } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 0,
             callback: function (v, i) { var y = D.annual.years[i]; return y % 10 === 0 ? String(y) : null; } } },
        y: { beginAtZero: true, grid: { color: C.grid }, ticks: { callback: function (v) { return v.toFixed(1) + '\u00b0'; } },
             title: { display: true, text: '\u00b0C above 1850\u20131900' } }
      }
    }
  });
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
