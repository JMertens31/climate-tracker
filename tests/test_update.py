"""Tests for update.py. Run with:  python -m unittest discover -s tests -v"""
import csv
import datetime as dt
import json
import math
import random
import calendar
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import update  # noqa: E402

HEADER = "# test file\n# Last updated: test\ndate,2t,clim_91-20,ano_91-20,status\n"


def c3s_offset(d: dt.date) -> float:
    """Independent re-implementation of the C3S formula, for checking offsets.csv."""
    def off(n):
        x = 2 * math.pi * (n - 0.5) / 365
        return 0.88 + 0.05 * math.sin(x) + 0.07 * math.cos(x)
    if d.month == 2 and d.day == 29:
        return (off(59) + off(60)) / 2
    n = dt.date(2025, d.month, d.day).timetuple().tm_yday
    return off(n)


def synthetic_csv(start: dt.date, end: dt.date, ano_fn, prelim_last=True) -> str:
    lines, d = [], start
    while d <= end:
        a = ano_fn(d)
        status = "PRELIMINARY" if (prelim_last and d == end) else "FINAL"
        lines.append(f"{d.isoformat()},{14 + a:.3f},14.000,{a:.3f},{status}")
        d += dt.timedelta(days=1)
    return HEADER + "\n".join(lines) + "\n"


class OffsetsTest(unittest.TestCase):
    def test_offsets_file_matches_c3s_formula(self):
        offs = update.load_offsets()
        self.assertEqual(len(offs), 366)
        for md, v in offs.items():
            m, d = map(int, md.split("-"))
            self.assertAlmostEqual(v, c3s_offset(dt.date(2024, m, d)), places=5, msg=md)

    def test_offsets_range_and_annual_mean(self):
        offs = update.load_offsets()
        vals = [v for k, v in offs.items() if k != "02-29"]
        self.assertAlmostEqual(sum(vals) / 365, 0.88, places=5)
        self.assertGreaterEqual(min(vals), 0.79)
        self.assertLessEqual(max(vals), 0.97)

    def test_known_values(self):
        offs = update.load_offsets()
        self.assertAlmostEqual(offs["10-18"], 0.852, places=3)
        self.assertAlmostEqual(offs["10-31"], 0.871, places=3)


class ComputeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = Path(self.tmp.name) / "site"
        self.latest = Path(self.tmp.name) / "data" / "latest.json"
        self.end = dt.date(2026, 10, 31)
        # A wavy, trending anomaly so every window gives a different answer.
        self.ano = lambda d: round(0.02 * (d.year - 2000) + 0.3 * math.sin(d.toordinal() / 37.0), 3)
        self.text = synthetic_csv(dt.date(2015, 1, 1), self.end, self.ano)

    def tearDown(self):
        self.tmp.cleanup()

    def brute_mean(self, start, end):
        vals, d = [], start
        while d <= end:
            vals.append(round(self.ano(d), 3) + c3s_offset(d))
            d += dt.timedelta(days=1)
        return sum(vals) / len(vals)

    def test_rolling_windows_and_ytd(self):
        res = update.build(self.text, today=dt.date(2026, 11, 2), site_dir=self.site, repo_latest=self.latest)
        tiles = {t["key"]: t for t in res["tiles"]}
        for n in update.WINDOWS:
            start = self.end - dt.timedelta(days=n - 1)
            self.assertEqual(tiles[f"{n}d"]["start"], start.isoformat())
            self.assertAlmostEqual(tiles[f"{n}d"]["value"], self.brute_mean(start, self.end), places=6)
        self.assertAlmostEqual(tiles["ytd"]["value"], self.brute_mean(dt.date(2026, 1, 1), self.end), places=6)
        # change vs a year earlier
        prev_end = dt.date(2025, 10, 31)
        exp = self.brute_mean(self.end - dt.timedelta(days=364), self.end) - \
            self.brute_mean(prev_end - dt.timedelta(days=364), prev_end)
        self.assertAlmostEqual(tiles["365d"]["change_1y"], exp, places=6)
        exp_ytd = self.brute_mean(dt.date(2026, 1, 1), self.end) - self.brute_mean(dt.date(2025, 1, 1), prev_end)
        self.assertAlmostEqual(tiles["ytd"]["change_1y"], exp_ytd, places=6)

    def test_outputs_written(self):
        update.build(self.text, today=dt.date(2026, 11, 2), site_dir=self.site, repo_latest=self.latest)
        html = (self.site / "index.html").read_text()
        self.assertIn("Contains modified Copernicus Climate Change Service information 2026", html)
        self.assertIn("Neither the European Commission nor ECMWF is responsible", html)
        self.assertNotIn("{{", html)
        latest = json.loads(self.latest.read_text())
        self.assertEqual(latest["latest_date"], "2026-10-31")
        self.assertEqual(latest["latest_status"], "PRELIMINARY")
        self.assertEqual(set(latest["means"]), {"30d", "90d", "180d", "365d", "ytd"})
        with (self.site / "data" / "series.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[-1]["date"], "2026-10-31")
        self.assertEqual(rows[0]["mean_30d_1850_1900_c"], "")  # not enough history yet

    def test_leap_day_uses_feb29_offset(self):
        res = update.build(self.text, today=dt.date(2026, 11, 2), site_dir=self.site, repo_latest=self.latest)
        with (self.site / "data" / "series.csv").open() as f:
            row = next(r for r in csv.DictReader(f) if r["date"] == "2024-02-29")
        self.assertAlmostEqual(float(row["offset_1850_1900_c"]), c3s_offset(dt.date(2024, 2, 29)), places=4)
        self.assertIsNotNone(res)


class AnnualTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = Path(self.tmp.name) / "site"
        self.latest = Path(self.tmp.name) / "data" / "latest.json"
        self.ano = lambda d: round(0.03 * (d.year - 2000) + 0.2 * math.sin(d.toordinal() / 41.0), 3)
        # Starts mid-year, so 2014 is partial and must be excluded; 2026 is in progress.
        self.text = synthetic_csv(dt.date(2014, 7, 1), dt.date(2026, 10, 31), self.ano)

    def tearDown(self):
        self.tmp.cleanup()

    def brute_year(self, y):
        d, vals = dt.date(y, 1, 1), []
        while d.year == y:
            vals.append(round(self.ano(d), 3) + c3s_offset(d))
            d += dt.timedelta(days=1)
        return sum(vals) / len(vals), len(vals)

    def test_complete_years_only(self):
        res = update.build(self.text, today=dt.date(2026, 11, 2), site_dir=self.site, repo_latest=self.latest)
        years = [a["year"] for a in res["annual_rows"]]
        self.assertEqual(years, list(range(2015, 2026)))

    def test_annual_values_and_leap_days(self):
        res = update.build(self.text, today=dt.date(2026, 11, 2), site_dir=self.site, repo_latest=self.latest)
        for a in res["annual_rows"]:
            exp, n = self.brute_year(a["year"])
            self.assertEqual(a["days"], n)
            self.assertAlmostEqual(a["pi"], exp, places=6, msg=a["year"])
        self.assertEqual(next(a for a in res["annual_rows"] if a["year"] == 2024)["days"], 366)

    def test_ranks_and_outputs(self):
        res = update.build(self.text, today=dt.date(2026, 11, 2), site_dir=self.site, repo_latest=self.latest)
        ranked = sorted(res["annual_rows"], key=lambda a: a["rank"])
        self.assertEqual([a["rank"] for a in ranked], list(range(1, len(ranked) + 1)))
        self.assertTrue(all(x["pi"] >= y["pi"] for x, y in zip(ranked, ranked[1:])))
        with (self.site / "data" / "annual.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["year"], "2015")
        self.assertEqual(rows[-1]["year"], "2025")
        latest = json.loads(self.latest.read_text())
        self.assertEqual(latest["last_complete_year"]["year"], 2025)
        self.assertEqual(latest["warmest_year"]["year"], ranked[0]["year"])
        html = (self.site / "index.html").read_text()
        self.assertIn("Annual Averages, 2015&ndash;2025", html)
        self.assertIn("Warmest 5 Years", html)
        self.assertNotIn("{{", html)

    def test_ordinal(self):
        self.assertEqual([update.ordinal(n) for n in (2, 3, 4, 11, 12, 13, 21, 22, 103)],
                         ["2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "103rd"])


class YearContextTest(unittest.TestCase):
    """Hand-checkable example: values by year 2000-2005."""
    def setUp(self):
        vals = {2000: 0.50, 2001: 0.70, 2002: 0.60, 2003: 0.80, 2004: 0.40, 2005: 0.70}
        self.ctx = {c["year"]: c for c in update.year_context(
            [{"year": y, "pi": v} for y, v in vals.items()])}

    def test_first_year(self):
        c = self.ctx[2000]
        self.assertEqual((c["rank_then"], c["years_then"]), (1, 1))
        self.assertEqual((c["later_years"], c["later_warmer"], c["later_colder"]), (5, 4, 1))
        self.assertEqual(c["rank_now"], 5)

    def test_record_at_the_time(self):
        self.assertEqual(self.ctx[2001]["rank_then"], 1)   # 0.70 beats 0.50
        self.assertEqual(self.ctx[2003]["rank_then"], 1)   # 0.80 beats all before
        self.assertEqual(self.ctx[2003]["later_warmer"], 0)
        self.assertEqual(self.ctx[2003]["rank_now"], 1)

    def test_middle_rank_at_the_time(self):
        c = self.ctx[2002]                                   # 0.60 vs 0.50, 0.70
        self.assertEqual((c["rank_then"], c["years_then"]), (2, 3))
        self.assertEqual((c["later_years"], c["later_warmer"], c["later_colder"]), (3, 2, 1))

    def test_coldest_at_the_time(self):
        c = self.ctx[2004]                                   # 0.40 is the lowest so far
        self.assertEqual((c["rank_then"], c["years_then"]), (5, 5))
        self.assertEqual((c["later_warmer"], c["later_colder"]), (1, 0))

    def test_ties(self):
        c = self.ctx[2005]                                   # 0.70 equals 2001
        self.assertTrue(c["tied_then"])
        self.assertEqual(c["rank_then"], 2)                  # only 2003 is strictly warmer
        self.assertEqual(c["later_years"], 0)
        self.assertEqual(self.ctx[2001]["later_warmer"], 1)  # 2005's tie does not count as warmer


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = Path(self.tmp.name) / "site"
        self.latest = Path(self.tmp.name) / "data" / "latest.json"
        self.ano = lambda d: round(0.02 * (d.year - 2005) + 0.1 * math.cos(d.toordinal() / 29.0), 3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_1991_2020_level(self):
        text = synthetic_csv(dt.date(1985, 1, 1), dt.date(2026, 10, 31), self.ano)
        res = update.build(text, today=dt.date(2026, 11, 2), site_dir=self.site, repo_latest=self.latest)
        d, vals = dt.date(1991, 1, 1), []
        while d <= dt.date(2020, 12, 31):
            vals.append(round(self.ano(d), 3) + c3s_offset(d))
            d += dt.timedelta(days=1)
        self.assertEqual(len(res["baselines"]), 1)
        self.assertAlmostEqual(res["baselines"][0]["value"], sum(vals) / len(vals), places=6)
        html = (self.site / "index.html").read_text()
        self.assertIn("Running Means Today", html)
        self.assertIn("the 1991&ndash;2020 average (", html)
        self.assertNotIn("{{", html)

    def test_real_anomalies_put_baseline_at_offset_mean(self):
        # If every 1991-2020 anomaly is zero, the baseline sits exactly at the mean offset (0.88).
        text = synthetic_csv(dt.date(1985, 1, 1), dt.date(2026, 10, 31),
                             lambda d: 0.0 if 1991 <= d.year <= 2020 else 0.5)
        res = update.build(text, today=dt.date(2026, 11, 2), site_dir=self.site, repo_latest=self.latest)
        self.assertAlmostEqual(res["baselines"][0]["value"], 0.88, places=3)

    def test_short_record_omits_line(self):
        text = synthetic_csv(dt.date(2015, 1, 1), dt.date(2026, 10, 31), self.ano)
        res = update.build(text, today=dt.date(2026, 11, 2), site_dir=self.site, repo_latest=self.latest)
        self.assertEqual(res["baselines"], [])
        self.assertNotIn("{{", (self.site / "index.html").read_text())


def rows_from(fn, start=dt.date(1991, 1, 1), end=dt.date(2026, 10, 31)):
    """Minimal rows with a known pre-industrial anomaly, for testing the analysis functions directly."""
    out, d = [], start
    while d <= end:
        out.append({"date": d, "pi": fn(d)})
        d += dt.timedelta(days=1)
    return out


class TrendFitTest(unittest.TestCase):
    def test_linear_recovers_known_line(self):
        t0 = update.decimal_year(dt.date(1991, 1, 1))
        rows = rows_from(lambda d: 0.5 + 0.025 * (update.decimal_year(d) - t0))   # 0.25 C/decade
        fit = update.fit_trends(rows)
        self.assertAlmostEqual(fit["linear"]["a"], 0.5, places=6)
        self.assertAlmostEqual(fit["linear"]["per_decade"], 0.25, places=6)
        # reaches 1.5 when t = 40 years -> 2031-01-01
        self.assertEqual(fit["linear"]["reaches_threshold"]["date"][:7], "2031-01")
        self.assertFalse(fit["linear"]["reaches_threshold"]["passed"])

    def test_exponential_recovers_known_curve(self):
        t0 = update.decimal_year(dt.date(1991, 1, 1))
        rows = rows_from(lambda d: 0.6 * math.exp(0.02 * (update.decimal_year(d) - t0)))
        fit = update.fit_trends(rows)
        self.assertAlmostEqual(fit["exponential"]["A"], 0.6, places=6)
        self.assertAlmostEqual(fit["exponential"]["k"], 0.02, places=6)
        # 1.5 = 0.6 e^(0.02 t) -> t = ln(2.5)/0.02 = 45.8 years -> late 2036
        self.assertEqual(fit["exponential"]["reaches_threshold"]["date"][:4], "2036")
        exp_now = fit["exponential"]["now"]
        self.assertAlmostEqual(fit["exponential"]["per_decade_now"], exp_now * (math.exp(0.2) - 1), places=9)

    def test_exponential_handles_values_at_or_below_zero(self):
        t0 = update.decimal_year(dt.date(1991, 1, 1))
        base = lambda d: 0.6 * math.exp(0.02 * (update.decimal_year(d) - t0))
        # a few negative days (as after Pinatubo) must not break the fit
        rows = rows_from(lambda d: -0.05 if d.year == 1992 and d.month < 3 else base(d))
        fit = update.fit_trends(rows)
        self.assertIsNotNone(fit["exponential"])
        self.assertAlmostEqual(fit["exponential"]["k"], 0.02, delta=0.001)

    def test_exponential_matches_brute_force_least_squares(self):
        rows = rows_from(lambda d: 0.5 + 0.03 * (d.year - 1991) + 0.1 * math.sin(d.toordinal() / 50))
        fit = update.fit_trends(rows)["exponential"]
        t0 = update.decimal_year(dt.date(1991, 1, 1))
        ts = [update.decimal_year(r["date"]) - t0 for r in rows]
        ys = [r["pi"] for r in rows]
        def sse(A, k):
            return sum((y - A * math.exp(k * t)) ** 2 for y, t in zip(ys, ts))
        best = sse(fit["A"], fit["k"])
        for dA, dk in ((0.001, 0), (-0.001, 0), (0, 0.0001), (0, -0.0001)):
            self.assertLessEqual(best, sse(fit["A"] + dA, fit["k"] + dk))

    def test_no_crossing_when_above_from_the_start(self):
        rows = rows_from(lambda d: 1.6 + 0.001 * (d.year - 1991))
        self.assertIsNone(update.fit_trends(rows)["linear"]["reaches_threshold"])

    def test_crossing_already_passed(self):
        t0 = update.decimal_year(dt.date(1991, 1, 1))
        rows = rows_from(lambda d: 0.5 + 0.04 * (update.decimal_year(d) - t0))   # 1.5 after 25 years -> 2016
        c = update.fit_trends(rows)["linear"]["reaches_threshold"]
        self.assertTrue(c["passed"])
        self.assertEqual(c["date"][:4], "2016")
        self.assertEqual(update.crossing_text(c, "; it"), "; it passed 1.5 \u00b0C in January 2016.")

    def test_decimal_year_round_trip(self):
        for d in (dt.date(2024, 2, 29), dt.date(2026, 10, 31), dt.date(1991, 1, 1)):
            self.assertEqual(update.from_decimal_year(update.decimal_year(d)), d)


class DaysAboveTest(unittest.TestCase):
    def test_counts_start_at_first_year_and_include_zero_years(self):
        hot = {dt.date(2016, 3, 1), dt.date(2016, 3, 2), dt.date(2019, 7, 4)}
        rows = rows_from(lambda d: 1.6 if d in hot else (1.5 if d.year == 2017 else 1.0),
                         start=dt.date(2010, 1, 1), end=dt.date(2020, 6, 30))
        res = update.days_above(rows)
        self.assertEqual(res["first_day"], "2016-03-01")
        self.assertEqual(res["years"], [2016, 2017, 2018, 2019, 2020])
        self.assertEqual(res["counts"], [2, 0, 0, 1, 0])   # exactly 1.5 is not "above"

    def test_none_above(self):
        res = update.days_above(rows_from(lambda d: 1.0, start=dt.date(2020, 1, 1)))
        self.assertEqual(res["years"], [])

    def test_text(self):
        above = {"years": [2016, 2017, 2026], "counts": [5, 9, 3], "first_day": "2016-02-10"}
        txt = update.days_above_text(above, "2026-10-31")
        self.assertIn("10 Feb 2016", txt)
        self.assertIn("2017 had the most, 9 days.", txt)
        self.assertIn("2026 has had 3 days so far", txt)


class CalendarTest(unittest.TestCase):
    def test_leap_and_partial_years(self):
        rows = rows_from(lambda d: round(d.month / 10, 2), start=dt.date(2023, 12, 30), end=dt.date(2025, 1, 2))
        cal = update.by_calendar_day(rows)
        self.assertEqual(len(cal["slots"]), 366)
        self.assertEqual(cal["years"], [2023, 2024, 2025])
        feb29 = cal["slots"].index("02-29")
        v = dict(zip(cal["years"], cal["values"]))
        self.assertEqual(v[2024][feb29], 0.2)
        self.assertIsNone(v[2023][feb29])
        self.assertEqual(sum(x is not None for x in v[2023]), 2)     # 30-31 Dec only
        self.assertEqual(sum(x is not None for x in v[2024]), 366)
        self.assertEqual(v[2025][cal["slots"].index("01-02")], 0.1)
        self.assertIsNone(cal["base_1991_2020"])                  # 1991-2020 not covered here

    def test_daily_1991_2020_average(self):
        rows = rows_from(lambda d: 0.5 + 0.01 * (d.year - 1991), start=dt.date(1990, 1, 1), end=dt.date(2026, 1, 1))
        cal = update.by_calendar_day(rows)
        base = cal["base_1991_2020"]
        self.assertEqual(len(base), 366)
        self.assertAlmostEqual(base[0], 0.5 + 0.01 * 14.5, places=3)   # mean over 30 years
        self.assertAlmostEqual(base[cal["slots"].index("02-29")], 0.5 + 0.01 * 15, places=3)  # 1992..2020 leap years


class PageSectionsTest(unittest.TestCase):
    def test_new_sections_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            ano = lambda d: round(0.02 * (d.year - 2000) + 0.1 * math.sin(d.toordinal() / 30.0) + 0.7, 3)
            text = synthetic_csv(dt.date(1985, 1, 1), dt.date(2026, 10, 31), ano)
            res = update.build(text, today=dt.date(2026, 11, 2), site_dir=Path(tmp) / "site",
                               repo_latest=Path(tmp) / "latest.json")
            html = (Path(tmp) / "site" / "index.html").read_text()
            self.assertIn("Running Means and Long-Term Trend Since 1991", html)
            self.assertIn("2026 Against Every Year Since 1985", html)
            self.assertIn("Days Above 1.5 &deg;C Each Year", html)
            self.assertNotIn("{{", html)
            self.assertEqual(res["trend"]["dates"][-1], "2026-10-31")
            self.assertTrue(res["trend"]["dates"][0] >= "1991-01-01")
            self.assertEqual(len(res["trend"]["linear"]), len(res["trend"]["dates"]))
            latest = json.loads((Path(tmp) / "latest.json").read_text())
            self.assertIn("trend_since_1991", latest)


def forecast_rows(fn, start=dt.date(1985, 1, 1), end=dt.date(2026, 11, 1)):
    """Rows with ano/pi/offset like update.build produces, for an anomaly function fn(date)."""
    update.OFFSETS.clear()
    update.OFFSETS.update(update.load_offsets())
    rows, d = [], start
    while d <= end:
        a = fn(d)
        off = update.OFFSETS[d.strftime("%m-%d")]
        rows.append({"date": d, "ano": a, "offset": off, "pi": a + off})
        d += dt.timedelta(days=1)
    return rows


class ForecastTest(unittest.TestCase):
    def test_noise_free_trend_is_forecast_exactly(self):
        t0 = update.decimal_year(dt.date(1991, 1, 1))
        fn = lambda d: 0.1 + 0.02 * (update.decimal_year(d) - t0)
        rows = forecast_rows(fn)
        fc = update.forecast_year(rows)
        truth = sum(fn(d) + update.OFFSETS[d.strftime("%m-%d")]
                    for d in (dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(365))) / 365
        self.assertAlmostEqual(fc["annual_median"], truth, places=6)
        self.assertAlmostEqual(fc["annual_high"] - fc["annual_low"], 0, places=6)
        self.assertEqual(fc["days_left"], 60)

    def test_uses_no_data_after_the_cut(self):
        random.seed(5)
        noise = {}
        fn = lambda d: noise.setdefault(d, random.gauss(0, 0.1)) + 0.02 * (d.year - 2000)
        rows = forecast_rows(fn, end=dt.date(2026, 10, 1))
        cut = next(i for i, r in enumerate(rows) if r["date"] == dt.date(2015, 6, 30))
        a = update.forecast_year(rows, cut_index=cut)
        for r in rows[cut + 1:]:
            r["ano"] += 5.0
            r["pi"] += 5.0
        b = update.forecast_year(rows, cut_index=cut)
        self.assertEqual(a["annual"], b["annual"])
        self.assertEqual(a["days"], b["days"])

    def test_complete_year_returns_none(self):
        rows = forecast_rows(lambda d: 0.5, end=dt.date(2025, 12, 31))
        self.assertIsNone(update.forecast_year(rows))

    def test_range_covers_truth_on_noisy_data(self):
        # AR(1) daily noise on a trend: the 90% range should contain the true outcome in most years.
        random.seed(11)
        series, e, d = {}, 0.0, dt.date(1985, 1, 1)
        while d <= dt.date(2026, 10, 1):
            e = 0.97 * e + random.gauss(0, 0.03)
            series[d] = 0.02 * (d.year - 2000) + e
            d += dt.timedelta(days=1)
        rows = forecast_rows(lambda d: series[d], end=dt.date(2026, 10, 1))
        idx = {r["date"]: i for i, r in enumerate(rows)}
        hits = total = 0
        for y in range(2008, 2026):
            fc = update.forecast_year(rows, cut_index=idx[dt.date(y, 6, 30)])
            truth = sum(r["pi"] for r in rows if r["date"].year == y) / (366 if calendar.isleap(y) else 365)
            hits += fc["annual_low"] <= truth <= fc["annual_high"]
            total += 1
        self.assertGreaterEqual(hits / total, 0.7)

    def test_days_include_observed(self):
        rows = forecast_rows(lambda d: 0.7 if d.year < 2026 else (0.8 if d.month < 6 else 0.5))
        fc = update.forecast_year(rows)
        observed = sum(r["pi"] > 1.5 for r in rows if r["date"].year == 2026)
        self.assertEqual(fc["days_observed"], observed)
        self.assertGreaterEqual(fc["days_low"], observed)

    def test_rank_odds_normal(self):
        # Centre 1.55, 90% range 1.50-1.60 -> sd = 0.05 / 1.645
        fc = {"annual_median": 1.55, "annual_low": 1.50, "annual_high": 1.60}
        prev = [{"pi": 1.60}, {"pi": 1.50}, {"pi": 1.30}]
        odds = update.rank_odds(fc, prev)
        sd = 0.05 / update.Z90
        p_record = 1 - update.normal_cdf((1.60 - 1.55) / sd)
        self.assertAlmostEqual(odds["rank_share"][1], p_record, places=6)      # = 5%
        self.assertAlmostEqual(odds["rank_share"][1], 0.05, places=3)
        self.assertAlmostEqual(odds["rank_share"][2], 0.90, places=3)
        self.assertAlmostEqual(sum(odds["rank_share"].values()), 1.0, places=3)
        self.assertAlmostEqual(odds["above_threshold"], 0.95, places=3)

    def test_wording(self):
        self.assertEqual(update.fmt_chance(0.02), "less than 5%")
        self.assertEqual(update.fmt_chance(0.97), "more than 95%")
        self.assertEqual(update.fmt_chance(0.62), "about 60%")


def oni_text_from(values: dict) -> str:
    """NOAA-format ONI text from {(season, year): anomaly}."""
    lines = ["SEAS  YR   TOTAL   ANOM"]
    for (season, year), v in values.items():
        lines.append(f"  {season} {year}  27.00  {v:5.2f}")
    return "\n".join(lines) + "\n"


def synthetic_oni(first=1985, last_abs=None, seed=7):
    """Monthly ONI-like series keyed by absolute end month, AR(1) with ~4-year cycles."""
    random.seed(seed)
    out, v = {}, 0.0
    for abs_m in range(first * 12 + 2, (last_abs or 2026 * 12 + 9) + 1):
        v = 0.92 * v + random.gauss(0, 0.35)
        out[abs_m] = max(-2.5, min(2.5, v))
    return out


def season_of(end_abs: int) -> tuple:
    """(season code, year) for the ONI season ending in absolute month end_abs (inverse of parse_oni)."""
    end_year, end_month = (end_abs - 1) // 12, (end_abs - 1) % 12 + 1
    if end_month == 1:
        return "NDJ", end_year - 1
    return update.ONI_SEASONS[end_month - 2], end_year


class OniTest(unittest.TestCase):
    def test_parse_and_lag_rule(self):
        text = oni_text_from({("NDJ", 2025): -0.6, ("DJF", 2026): -0.5, ("JAS", 2026): 2.16})
        oni = update.parse_oni(text)
        self.assertEqual(oni[2026 * 12 + 1], -0.6)          # NDJ 2025 ends in January 2026
        self.assertEqual(oni[2026 * 12 + 2], -0.5)          # DJF 2026 ends in February
        self.assertEqual(oni[2026 * 12 + 9], 2.16)          # JAS 2026 ends in September
        self.assertEqual(update.oni_known_on(dt.date(2026, 11, 3), oni), 2.16)   # Nov -> season ending Sep
        self.assertEqual(update.oni_known_on(dt.date(2026, 3, 15), oni), -0.6)   # Mar -> season ending Jan
        self.assertIsNone(update.oni_known_on(dt.date(2026, 10, 3), oni))        # Aug season missing
        self.assertEqual(update.oni_season_label(dt.date(2026, 11, 3)), "Jul\u2013Sep 2026")
        self.assertEqual(update.oni_season_label(dt.date(2026, 3, 15)), "Nov 2025\u2013Jan 2026")

    def test_season_round_trip(self):
        for m in range(2020 * 12 + 1, 2023 * 12 + 1):
            self.assertIn(m, update.parse_oni(oni_text_from({season_of(m): 0.1})))

    def test_bad_or_stale_index_is_ignored(self):
        self.assertIsNone(update.load_oni_text("not the right file", dt.date(2026, 11, 3)))
        stale = oni_text_from({("JFM", 2026): 0.1})
        self.assertIsNone(update.load_oni_text(stale, dt.date(2026, 11, 3)))
        fresh = oni_text_from({("JAS", 2026): 2.16})
        self.assertIsNotNone(update.load_oni_text(fresh, dt.date(2026, 11, 3)))
        self.assertIsNone(update.load_oni_text(None, dt.date(2026, 11, 3)))

    def test_enso_model_helps_when_enso_drives_temperature(self):
        oni = synthetic_oni()
        random.seed(3)
        e, series, d = 0.0, {}, dt.date(1985, 1, 1)
        while d <= dt.date(2026, 10, 1):
            e = 0.97 * e + random.gauss(0, 0.025)
            lagged = oni.get(d.year * 12 + d.month - 3, 0.0)      # temperature follows ENSO ~3 months later
            series[d] = 0.02 * (d.year - 2000) + 0.12 * lagged + e
            d += dt.timedelta(days=1)
        rows = forecast_rows(lambda d: series[d], end=dt.date(2026, 10, 1))
        idx = {r["date"]: i for i, r in enumerate(rows)}
        err = {"base": 0.0, "enso": 0.0}
        for y in range(2008, 2026):
            truth = sum(r["pi"] for r in rows if r["date"].year == y) / (366 if calendar.isleap(y) else 365)
            for model, o in (("base", None), ("enso", oni)):
                fc = update.forecast_year(rows, cut_index=idx[dt.date(y, 4, 15)], oni=o)
                self.assertEqual(fc["model"], model)
                err[model] += abs(fc["annual_median"] - truth)
        self.assertLess(err["enso"], err["base"] * 0.9)

    def test_falls_back_when_current_value_missing(self):
        oni = synthetic_oni(last_abs=2026 * 12 + 6)                # newest season ends June
        rows = forecast_rows(lambda d: 0.5 + 0.01 * (d.year - 2000), end=dt.date(2026, 11, 1))
        fc = update.forecast_year(rows, oni=oni)                   # needs the season ending September
        self.assertEqual(fc["model"], "base")
        self.assertEqual(fc["spread"], update.FORECAST_SPREAD)

    def test_page_names_the_index(self):
        oni = synthetic_oni()
        text = oni_text_from({season_of(m): v for m, v in oni.items()})
        with tempfile.TemporaryDirectory() as tmp:
            ano = lambda d: round(0.02 * (d.year - 2000) + 0.1 * math.sin(d.toordinal() / 30.0) + 0.7, 3)
            csv_text = synthetic_csv(dt.date(1985, 1, 1), dt.date(2026, 11, 1), ano)
            res = update.build(csv_text, today=dt.date(2026, 11, 3), site_dir=Path(tmp) / "site",
                               repo_latest=Path(tmp) / "latest.json", oni_text=text)
            self.assertEqual(res["forecast"]["model"], "enso")
            html = (Path(tmp) / "site" / "index.html").read_text()
            self.assertIn("NOAA&#x27;s El Ni\u00f1o index (Jul\u2013Sep 2026: ", html)
            self.assertIn('<a href="https://github.com/JMertens31/climate-tracker#how-the-projection-works">see how the projection works</a>.', html)
            self.assertIn("NOAA Climate Prediction Center", html)
            latest = json.loads((Path(tmp) / "latest.json").read_text())
            self.assertEqual(latest["projection"]["model"], "enso")


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.ano = lambda d: 0.5
        self.text = synthetic_csv(dt.date(2015, 1, 1), dt.date(2026, 10, 31), self.ano)

    def test_bad_header(self):
        with self.assertRaises(update.DataError):
            update.parse_series(self.text.replace("ano_91-20", "anomaly"))

    def test_gap_in_dates(self):
        rows = update.parse_series(self.text)
        del rows[1000]
        with self.assertRaises(update.DataError):
            update.validate(rows, dt.date(2026, 11, 2))

    def test_stale_data(self):
        rows = update.parse_series(self.text)
        with self.assertRaises(update.DataError):
            update.validate(rows, dt.date(2026, 12, 1))

    def test_unknown_status(self):
        rows = update.parse_series(self.text.replace("PRELIMINARY", "TEMP"))
        with self.assertRaises(update.DataError):
            update.validate(rows, dt.date(2026, 11, 2))


if __name__ == "__main__":
    unittest.main()
