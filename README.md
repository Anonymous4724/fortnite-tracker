# Fortnite Comp Tracker

*[Version française](README.fr.md)*

Forecasts the point thresholds of Fortnite competitive tournaments — how many points a
given finishing rank will require — before the tournament starts and while it is running.

Thresholds are published only after the fact. A player deciding whether to keep playing,
or how aggressively, is guessing. This estimates the answer from the tournament's scoring
table, its field size, and what earlier editions of comparable tournaments did.

**Measured, out of sample: 6.4 % median error on the cold forecast, 86 % of real thresholds
inside the quoted range**, over 290 hand-checked thresholds from 66 tournaments, leaving one
tournament out at a time. [`docs/methodology.md`](docs/methodology.md) says how that was
measured and where the model stops being believable.

![Fitted curve over the observed thresholds](analysis/figures/curve.png)

---

## The model

    threshold(rank) = level · exp(−a · (q^b − q_ref^b)),    q = rank / field

`level` is the tournament's threshold at rank 20; `a = 1.255` and `b = 0.370` describe the
shape of the standings, and are shared across tournaments. The shape is a property of how
Fortnite scores placement and eliminations; the level is what changes from one week to the
next.

Two consequences make the tool work at all:

- **Field size barely matters.** The elasticity `d ln(threshold) / d ln(field) = a·b·(q^b − q_ref^b)`
  comes out near 0.05, so doubling the number of teams moves the threshold about 3.5 %. A
  rough guess at turnout is good enough, which is fortunate, because turnout is exactly what
  you cannot know in advance.
- **The level is read at rank 20, not rank 1.** Between editions of the same tournament the
  pace moves 7.7 % at rank 1 but 3.4 % at rank 20. Rank 1 is one team having a good night;
  rank 20 is a population statistic. Anchoring on rank 1 costs 2.8 points of forecast error,
  the one thing in the anchor choice the data settles outright
  ([`analysis/anchor.py`](analysis/anchor.py)).

Where no comparable edition exists, the scoring-to-points ratio is shrunk toward a prior with
weight `n / (n + 2)`. That path is the weakest part of the model and the methodology note
prices it: 34.6 % median error, against 5.9 % once a single comparable edition exists.

---

## Results

Leave-one-tournament-out, forecasting cold — before any reading of the live standings.

| rank band | n | median error | mean | band coverage |
|---|---:|---:|---:|---:|
| 1 – 5 | 94 | 8.7 % | 12.2 % | 85 % |
| 6 – 25 | 94 | 5.7 % | 12.6 % | 82 % |
| 26 – 100 | 47 | 9.5 % | 12.7 % | 81 % |
| 101 – 500 | 38 | 6.0 % | 10.9 % | 97 % |
| beyond 500 | 17 | 2.2 % | 3.3 % | 100 % |
| **all** | **290** | **6.4 %** | **11.7 %** | **86 %** |

Against the two baselines worth beating, on the 206 rows where all three can answer: this
model 5.68 %, carrying the last edition's threshold forward 6.03 %, the category's median
7.56 %. Bootstrapped over tournaments the gap to carry-forward is +0.36 points with a 95 %
interval of [−3.24, +4.07] — **not a win**, and the methodology note says so. Where the model
earns its place is beyond rank 500 (2.0 % against 9.5 %, because the curve extrapolates to
ranks no previous edition published) and on the 84 of 290 rows where no comparable edition
exists at all and the baselines have nothing to say.

The bands are slightly conservative: 86 % empirical coverage against a nominal 80 %.

---

## Running it

Python 3.10 or later. No dependencies beyond Flask.

```bash
pip install -r requirements.txt
python app.py                 # opens http://127.0.0.1:5000
```

Windows: double-click `run_windows.bat`.

Live standings come from the [Cito](https://citoapi.com) API — a free key allows 500 requests
a month. The app asks for it on first start and keeps it in `cito_key.txt`, which is
git-ignored. Everything except the live import works without a key: the model is trained from
thresholds you type in yourself.

The interface is English by default with a French switch in the header
([`i18n.py`](i18n.py) holds the translations).

### The research layer

```bash
pip install -r analysis/requirements.txt
python -m analysis.fit           # refit a and b, with intervals
python -m analysis.diagnostics   # residuals, clustering, heteroskedasticity
python -m analysis.validate      # leave-one-out against two baselines
python -m analysis.anchor        # why the level is read at rank 20
python -m analysis.figures       # regenerate the figures
```

`analysis/` uses numpy, pandas, scipy and matplotlib. The app does not: nothing under
`analysis/` is imported by `app.py` or the model, so the app still runs on a bare Python
install. The arrow points one way.

---

## Layout

| | |
|---|---|
| `app.py` | Flask routes, and the input validation every write endpoint goes through |
| `calibration.py` | the model: curve fit, anchor cascade, shrinkage |
| `predict.py` | forecasts during a live session — pace, extrapolation, my own race |
| `scoring_infer.py` | recovers a tournament's scoring table from team totals by least squares |
| `db.py` | SQLite schema, migrations, every query |
| `cito.py` | the standings API client |
| `i18n.py` | English source strings, French translations |
| `analysis/` | numpy/pandas/scipy: refit, diagnostics, cross-validation, figures |
| `docs/` | [methodology note](docs/methodology.md), [user manual](docs/manual.md) |

Checks, all runnable:

```bash
python backtest.py       # walk-forward error on the tournaments you have tracked
python check_forms.py    # every form field survives the round trip to the database
python fuzz_api.py       # 2,856 malformed requests, 0 server errors
python selfcheck.py      # all of the above plus both languages, in one pass
python cleanup.py --list # what in the folder is no longer reachable from the code
```

`check_forms.py` exists for a reason worth repeating: a form field was once silently dropped
between the browser and the database, and an afternoon of hand-entered results went with it.
It now posts a recognisable value into every field of every form and reads them all back.

---

## What I would fix next

In the order the numbers justify:

1. `b` is not identified. 214 of 225 observations sit at `q < 0.25`; restricting to that range
   moves `b` from 0.46 to 0.29. It is a working value, not a measurement.
2. `calibration.fit_curve` normalises by the *observed* rank-20 threshold instead of treating
   the level as a free parameter, which attenuates `a` by 8 % and `b` by 18 %.
3. Three rank-50 thresholds look like truncated standings pages read as results. They carry
   the entire left tail — without them the shape's working error is 4 %, not 6 %.
4. The live models are effectively untested: only two tournaments were genuinely tracked
   minute by minute.

---

MIT licensed. Fortnite is a trademark of Epic Games; this project is unaffiliated with them
and uses no game assets.
