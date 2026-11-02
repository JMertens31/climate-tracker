#!/usr/bin/env python3
"""Generate offsets.csv: the C3S daily offset from 1991-2020 to 1850-1900.

Run ONCE. The output is committed to the repository and read by update.py on
every daily run; the offsets are never recalculated by the daily job.

Source: C3S Climate Bulletin, "About the data and analysis"
https://climate.copernicus.eu/climate-bulletin-about-data-and-analysis

    offset(n) = 0.88 + 0.05*sin(2*pi*(n - 0.5)/365) + 0.07*cos(2*pi*(n - 0.5)/365)

where n = 1..365 is the day of a 365-day year (29 February omitted).
For 29 February the offset is the mean of 28 February and 1 March.

Usage:  python tools/make_offsets.py            (writes offsets.csv in the repo root)
"""
import csv
import datetime as dt
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "offsets.csv"


def offset(n: int) -> float:
    x = 2 * math.pi * (n - 0.5) / 365
    return 0.88 + 0.05 * math.sin(x) + 0.07 * math.cos(x)


def main() -> None:
    rows = []
    # 2025 is a non-leap year, so it gives n = 1..365 in calendar order.
    for n in range(1, 366):
        d = dt.date(2025, 1, 1) + dt.timedelta(days=n - 1)
        rows.append((d.strftime("%m-%d"), n, offset(n)))
        if d.strftime("%m-%d") == "02-28":
            rows.append(("02-29", "", (offset(59) + offset(60)) / 2))

    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month_day", "day_of_365_year", "offset_c"])
        for md, n, off in rows:
            w.writerow([md, n, f"{off:.6f}"])
    print(f"Wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
