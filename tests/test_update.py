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
