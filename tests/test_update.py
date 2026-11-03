"""Tests for update.py. Run with:  python -m unittest discover -s tests -v"""
import csv
import datetime as dt
import json
import math
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
