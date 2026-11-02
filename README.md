# Global Temperature Tracker

A daily-updated dashboard of global surface air temperature above the 1850–1900 pre-industrial baseline, built from Copernicus ERA5 data.

**Live dashboard:** `https://<owner>.github.io/<repo>/` (replace with your Pages URL)

It shows five running means, each ending on the latest available day:

| Measure | Definition |
| --- | --- |
| 30-day | Mean of the last 30 daily values |
| 90-day | Mean of the last 90 daily values |
| 180-day | Mean of the last 180 daily values |
| 365-day (headline) | Mean of the last 365 daily values |
| Year to date | Mean from 1 January of the current year to the latest day |

Each is also compared with the same window one year earlier.

## How It Works

A GitHub Actions workflow (`.github/workflows/daily.yml`) runs once a day at 12:17 UTC:

1. Runs the tests in `tests/`.
2. `update.py` downloads the Climate Pulse daily CSV and checks it: expected columns, no missing dates, newest value no more than 10 days old. Any failure stops the run, and GitHub emails the person who last edited the workflow.
3. Adds the stored pre-industrial offset for each calendar day from `offsets.csv`.
4. Computes the running means and builds the site in `site/`: `index.html`, `data/series.csv` (full computed series) and `data/latest.json`.
5. Commits `data/latest.json` to the repo (one small change per day, which also keeps the schedule from being switched off for inactivity).
6. Publishes `site/` to GitHub Pages.

No API keys or secrets are needed. The script uses only the Python standard library.

## Method

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
- **One dataset:** ERA5 is a reanalysis. Other records (NASA GISTEMP, NOAA, HadCRUT5, Berkeley Earth) differ by a few hundredths of a degree.

## Running It by Hand

- **On GitHub:** Actions tab → **Daily update** → **Run workflow**.
- **Locally:** `python update.py`, then open `site/index.html`. Use `python update.py --input file.csv` to build from a saved copy of the data.
- **Tests:** `python -m unittest discover -s tests -v`

## Changing Things

| To change | Edit |
| --- | --- |
| Window lengths, chart history, freshness limit | Constants at the top of `update.py` (`WINDOWS`, `TREND_YEARS`, `DAILY_DAYS`, `MAX_AGE_DAYS`) |
| Run time | `cron` line in `.github/workflows/daily.yml` (times are UTC) |
| Page text and styling | `HTML_TEMPLATE` in `update.py` |
| Offsets, if C3S revises them | Update the formula in `tools/make_offsets.py`, run `python tools/make_offsets.py`, commit the new `offsets.csv` and note the change here |

## Repository Contents

| Path | Purpose |
| --- | --- |
| `update.py` | Daily download, checks, calculation and site build |
| `offsets.csv` | Stored daily offsets, 1991–2020 to 1850–1900 |
| `tools/make_offsets.py` | One-off generator for `offsets.csv` |
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
