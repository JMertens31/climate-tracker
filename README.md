# Daily Global Temperature Tracker

A daily-updated dashboard of global surface air temperature above the 1850–1900 pre-industrial baseline, built from Copernicus ERA5 data.

**Live dashboard:** `https://<owner>.github.io/<repo>/` (replace with your Pages URL)

The page is laid out in this order: global surface air temperature anomaly (the running means below, as bars), daily values for the last 60 days, days above 1.5 °C each year, this year against every year by calendar day, the long-term anomaly since 1991, and annual averages. The top chart shows five running means as bars, each ending on the latest available day, with the 365-day mean in blue and dashed lines at 1.5 °C, 2 °C and the 1991–2020 average:

| Measure | Definition |
| --- | --- |
| 30-day | Mean of the last 30 daily values |
| 90-day | Mean of the last 90 daily values |
| 180-day | Mean of the last 180 daily values |
| 365-day (headline) | Mean of the last 365 daily values |
| Year to date | Mean from 1 January of the current year to the latest day |

Other sections:

- **Long-term global surface air temperature anomaly:** the 30-day and 365-day means from 1 January 1991 (the start of the 1991–2020 period), with two trends fitted by least squares to every daily value since then: a straight line, and an exponential curve (y = A·e^(kt)). The page states each trend's current level, rate per decade and when it reaches (or passed) 1.5 °C. The exponential is fitted to the values themselves rather than to their logarithms, because some days in 1991–92 (after the Pinatubo eruption) were at or below the 1850–1900 level. Its curvature depends on measuring from 1850–1900, so it indicates acceleration, not a forecast.
- **This year against every year since 1940, by calendar day:** one line per year, with the warmest complete year in red and the current year (and its projected remainder) in blue, and the 1991–2020 average for each calendar day as a dashed curve.
- **Daily values, last 60 days:** with dashed lines at 1.5 °C, 2 °C and the 1991–2020 average.
- **Days above 1.5 °C each year:** from the first year with such a day; the current year is shown separately as incomplete.

- **Year-end projection:** until 31 December, the page projects the current year's final average and its final count of days above 1.5 °C, each with a 90% range. It shows them as a light bar with a whisker in the annual and days-above charts, as a shaded band on the calendar-day chart, and as text giving the most likely rank and the chance of a record.

### How the Projection Works

The rest of the year is simulated about 1,000 times. Each simulation starts from the long-term warming trend and from how far the last 30 days sit above it. It then follows the actual ups and downs of a past year from the same date, using every year since 1991 except 1991–1993, when the Pinatubo eruption cooled the planet. It also takes into account NOAA's Oceanic Niño Index for the latest season NOAA has published (for example, Jul–Sep 2026: +2.16, El Niño), because El Niño tends to warm global temperatures over the following months and La Niña to cool them. The 90% range and the odds are calibrated against the forecasts this method would have made for every year from 2005 to 2025.

**Why early-year projections are less certain.** The El Niño index is a lagging indicator. It describes Pacific sea temperatures from roughly two to four months earlier, so it can't see an El Niño or La Niña that is only just starting. That matters most early in the year, when most of the year is still to come. In 2026, the projection made on 1 March used the latest index available, which showed La Niña (−0.6 for November–January). It expected the year to finish at +1.39 °C, with a 90% range of +1.29 to +1.52 °C. El Niño then developed in April and became strong by June, and 2026 finished well above that range. Projections narrow as the year goes on: from September onward, the 90% range is typically under 0.1 °C wide.

**In more detail,** `forecast_year` in `update.py`:

1. A straight-line trend is fitted to the daily anomalies (relative to 1991–2020) from 1991 to the latest day.
2. Two things describe the current state: how far the last 30 days sit above or below that trend, and the El Niño index (NOAA's Oceanic Niño Index, ONI) for the most recent 3-month season that NOAA had published by then (the season ending two months before the forecast month).
3. Every past year since 1991 (except 1991–1993, cooled by the Pinatubo eruption) contributes its actual path for the rest of the year, starting on the same date and up to 15 days either side. From those paths the model learns, for each day ahead, how much of today's departure usually remains and how much each unit of El Niño (or La Niña) adds, and keeps each path's remaining day-to-day variation.
4. Each simulation = trend + those two effects + one past year's variation + the stored pre-industrial offset. The year-end range, the days above 1.5 °C and the daily band come from the spread of the simulations.
5. The odds of each rank (and of finishing above 1.5 °C) come from a bell curve centred on the simulations' median, with the width of their 90% range.
6. Only data available on the forecast date is used, so a backtest never sees the future.

**If the El Niño index is unavailable** (NOAA down, file format changed, or the latest season more than 4 months old), the projection falls back to the temperature-only model with its own calibration, and the page says so. The rest of the site is unaffected.

**Calibration (ERA5, forecasts on the 15th of each month January–November, 2005–2025; 231 cases):**

| | Temperature only | With El Niño index (used) |
| --- | --- | --- |
| 90% range held the outcome, raw | 83.5% | 81.8% |
| Widening factor for 90% | 1.3 (`FORECAST_SPREAD`) → 90.0% | 1.35 (`FORECAST_SPREAD_ENSO`) → 90.9% |
| Average error of the median | 0.039 °C | 0.036 °C |
| Average width of the 90% range | 0.170 °C | 0.142 °C |
| Brier score of rank odds (lower is better) | 0.346 | 0.315 |

Grouped by the chance they stated, the El Niño model's odds came true about as often as they said: about 13% for forecasts that said about 13%, 41% for about 37%, 56% for about 63%, 88% for about 85%. Adding whether El Niño was strengthening or weakening did not help. Even a perfect forecast of El Niño four months ahead would only have cut the average error to 0.031 °C. Re-run `python tools/backtest_forecast.py` after any change to the model.

**Limits.** The median was on average about 0.015 °C below the outcome, because a straight-line trend lags slightly behind recent warming. Some years remain badly missed: in June 2023 both models expected about +1.29 °C for a year that finished at +1.48 °C. Chances below 5% or above 95% are shown as "less than 5%" or "more than 95%". "Record" means the ERA5 record; other datasets rank years differently.

It also shows the **annual average of every complete calendar year from 1940** (the start of the ERA5 record) with the warmest five years ranked. A year appears once all its days are in the data, normally in the first days of January.

A **year lookup** below the annual chart lets readers pick any complete year (from the menu or by clicking its bar) and see:

- how it ranked when it ended (for example, the warmest year on record at the time, or the 3rd warmest);
- how many later years have been warmer, and where it ranks among the years from then to now;
- where it ranks among all complete years today.

"On record" means the ERA5 record, which starts in 1940. Years are compared at the 0.001 °C precision shown on the page; equal values count as ties, not as warmer. The rankings are calculated in `update.py` (`year_context`) and tested; the page only turns them into sentences.

Hovering over a bar shows the same window one year earlier. Under the chart, a sentence reports the longest span of whole years (counted back from the latest day) whose average is above 1.5 °C, the next-longest span's average, and the 10-year and 20-year averages. A value that would round to 1.50 is shown to three decimals so it is clear which side of 1.5 °C it is on. All averages from 1 to 20 years are also in `latest.json`.

## How It Works

A GitHub Actions workflow (`.github/workflows/daily.yml`) runs once a day at 12:17 UTC:

1. Runs the tests in `tests/`.
2. `update.py` downloads the Climate Pulse daily CSV and checks it: expected columns, no missing dates, newest value no more than 10 days old. Any failure stops the run, and GitHub emails the person who last edited the workflow.
3. Adds the stored pre-industrial offset for each calendar day from `offsets.csv`.
4. Computes the running means and annual averages, and builds the site in `site/`: `index.html`, `data/series.csv` (full daily series), `data/annual.csv` (complete calendar years) and `data/latest.json`.
5. Commits `data/latest.json` to the repo (one small change per day, which also keeps the schedule from being switched off for inactivity).
6. Publishes `site/` to GitHub Pages.

No API keys or secrets are needed. The script uses only the Python standard library.

## Method

- **El Niño index (projection only):** NOAA Climate Prediction Center, Oceanic Niño Index, `https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt`. NOAA data are in the public domain; the page credits NOAA CPC.
- **Source:** ERA5 daily global mean 2 m air temperature and its anomaly relative to the 1991–2020 daily climatology, from [Copernicus Climate Pulse](https://pulse.climate.copernicus.eu/): `https://sites.ecmwf.int/data/climatepulse/data/series/era5_daily_series_2t_global.csv`. Data run from 1 January 1940 and are about 2 days behind real time. The newest day is flagged `PRELIMINARY` and usually becomes final about 3 days behind real time.
- **Pre-industrial baseline:** the 1991–2020 anomaly plus the C3S daily offset to 1850–1900 ([C3S method](https://climate.copernicus.eu/climate-bulletin-about-data-and-analysis)):

  `offset(n) = 0.88 + 0.05·sin(2π(n − 0.5)/365) + 0.07·cos(2π(n − 0.5)/365)`

  where `n` = 1–365 is the day of a 365-day year. For 29 February the offset is the mean of 28 February and 1 March. The offset ranges from 0.79 to 0.97 °C and averages exactly 0.88 °C over a year.
- **Offsets are calculated once.** `tools/make_offsets.py` generated `offsets.csv` (366 rows, one per calendar day). The daily job only reads that file. Regenerate it only if C3S revises the formula (see Changing Things).
- **Check against C3S:** this method reproduces the C3S published 2024 annual value of 1.60 °C above pre-industrial (computed: 1.599 °C).

## Limits

- **Paris Agreement:** the 1.5 °C limit refers to warming averaged over about 20 years. A 30-day, 365-day or calendar-year value above 1.5 °C is not, on its own, a breach.
- **Uncertainty:** the pre-industrial offset comes from other datasets, not ERA5, so values above pre-industrial carry roughly ±0.1 °C uncertainty. Differences between windows and years are more precise than the absolute level.
- **Preliminary data:** the latest day may change slightly. Each run reprocesses the whole series, so revisions are picked up automatically.
- **Early years:** ERA5 before about 1950 rests on far fewer observations, especially over the oceans and during the Second World War, so the 1940s values are less certain than later ones.
- **The 1991–2020 line:** it is the 1991–2020 annual average, +0.88 °C above 1850–1900. That is exact for the 365-day and annual values. For shorter windows the matching 1991–2020 level shifts with the season (about 0.79–0.97 °C), so the line is a guide there, not an exact baseline.
- **One dataset:** ERA5 is a reanalysis. Other records (NASA GISTEMP, NOAA, HadCRUT5, Berkeley Earth) differ by a few hundredths of a degree.

## Running It by Hand

- **On GitHub:** Actions tab → **Daily update** → **Run workflow**.
- **Locally:** `python update.py`, then open `site/index.html`. Use `python update.py --input file.csv` to build from a saved copy of the data.
- **Tests:** `python -m unittest discover -s tests -v`

## Changing Things

| To change | Edit |
| --- | --- |
| Reference periods drawn as dashed lines (for example, add `("1981\u20132010 average", 1981, 2010)`) | `BASELINES` at the top of `update.py`; any period from 1941 onward works |
| Window lengths, chart history, freshness limit | Constants at the top of `update.py` (`WINDOWS`, `TREND_START`, `TREND_SAMPLE_DAYS`, `DAILY_DAYS`, `MAX_AGE_DAYS`) |
| Run time | `cron` line in `.github/workflows/daily.yml` (times are UTC) |
| Page text and styling | `HTML_TEMPLATE` in `update.py` |
| Offsets, if C3S revises them | Update the formula in `tools/make_offsets.py`, run `python tools/make_offsets.py`, commit the new `offsets.csv` and note the change here |

## Repository Contents

| Path | Purpose |
| --- | --- |
| `update.py` | Daily download, checks, calculation and site build |
| `offsets.csv` | Stored daily offsets, 1991–2020 to 1850–1900 |
| `tools/make_offsets.py` | One-off generator for `offsets.csv` |
| `tools/backtest_forecast.py` | Checks the year-end projection (both models, ranges and odds) against past years |
| `tests/test_update.py` | Tests for offsets, running means, outputs and data checks |
| `data/latest.json` | Latest values, committed daily |
| `.github/workflows/daily.yml` | Schedule, build and publish |

## Data Licence and Attribution

The temperature data are Copernicus Climate Change Service (C3S) information, used under the [Licence to Use Copernicus Products](https://object-store.os-api.cci2.ecmwf.int/cci2-prod-catalogue/licences/licence-to-use-copernicus-products/licence-to-use-copernicus-products_b4b9451f54cffa16ecef5c912c9cebd6979925a956e3fa677976e0cf198c2c18.pdf) (version 1.2, November 2019). Because this project modifies the data (pre-industrial offset, running means), any publication of its outputs must carry:

> Contains modified Copernicus Climate Change Service information [year]
>
> Neither the European Commission nor ECMWF is responsible for any use that may be made of the Copernicus information or data it contains.

The dashboard footer includes both statements automatically. Copernicus and ECMWF logos are not used.

## Code Licence

The code in this repository is licensed under the terms in `LICENSE`. That licence covers the code only, not the Copernicus data or derived values, which remain under the Copernicus licence above.

## Maintainer

Personal project, maintained by Joël Mertens. Not affiliated with or endorsed by any organisation, including Copernicus, ECMWF or the European Commission.
