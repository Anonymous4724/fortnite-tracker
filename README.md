# Fortnite Comp Tracker

*[Version française](README.fr.md)*

Forecasts the point thresholds of Fortnite competitive tournaments — how many points a
given finishing rank will require — before the tournament starts and while it is running.

Thresholds are published only after the fact. A player deciding whether to keep playing,
or how aggressively, is guessing. This estimates the answer from the tournament's scoring
table, its field size, and what earlier editions of comparable tournaments did.

Four launchers at the root are the whole day-to-day; everything else is in `src/`:

| | |
|---|---|
| `update.bat` | everything — a harvest pass for the new tournaments, the model rebuilt and verified, the week's calendar, the page, the push; ten minutes |
| `site.bat` | the page and the week's calendar only; seconds |
| `harvest.bat` | the long download of past tournaments, a night |
| `tracker.bat` | the local tracker app |

**Measured as a forecast — the newest 600 tournaments predicted from the 6,632 that came
before, nothing seeing the future: 4.2 % median error where the cup has run before, 85 % of
real thresholds inside the quoted range.** Where the cup has never run, the forecast rests on
the scoring table alone and the error is 20 %; half of a new season's tournaments start out
that way. The training set is 7,232 tournaments and 79,891 thresholds read from Osirion's
public API. [`docs/methodology.md`](docs/methodology.md) says how that was measured and where
the model stops being believable.

![Fitted curve over the observed thresholds](analysis/figures/curve.png)

---

## The model

A cascade, most direct reading first. Each rung answers only when the one above it cannot.

1. **The previous edition, read straight.** Same cup, same region, same rank, last time. The
   band is the 80th percentile of how much that rank moved between consecutive editions of
   that cup, measured rather than assumed. This rung is the carry-forward baseline promoted
   to the model, and it is first because it kept winning: a strong evening lifts rank 1 and
   rank 20 together, and any forecast that multiplies one edition's level by a ratio taken
   from others throws that correlation away. Two corrections since: the edition read is the
   last one with the same entry bar as the cup coming up (`competition.entry`, read off
   Epic's `currentRanking` requirement — a cup admitting Unreal alone is another field than
   one open from Diamond), the band widened by half when no such edition exists; and a known
   field that is not the edition's moves the value along the curve's quantile term, capped at
   a fifth, which on the newest 600 tournaments cuts this rung's error at those rows from
   6.3 % to 4.7 % — the reference-rank term must stay out of it, since taken as a ratio it
   says a smaller field lifts the top ranks, and the data refuses that.
2. **Level times measured shape.** The cup's threshold at rank 20 from its previous edition,
   times what that rank was worth relative to rank 20 across the cup's editions — a lookup
   table, not a curve, because the table halved the error where it applies. Category first,
   then the family across regions.
3. **Level times the curve**, for ranks no edition has measured:

       threshold(rank) = level · exp(−a · (q^b − q_ref^b)),    q = rank / field

   `a` and `b` are fitted on every finished tournament. The curve is right within a few
   percent near rank 20 and drifts outward from there — 12 % low at rank 1, 5 % at the deep
   end — which is exactly why it is third.
4. **The closed lobby**, for a final played in a single lobby that no edition has been seen
   of — the usual case for a Round 2 whose Round 1 the model knows. Every threshold of every
   single-lobby final in the training set is divided by the most a team could score over the
   games played and bucketed by the share of the lobby the rank is, per game mode and team
   size; the estimate is read off that table, log-linearly between buckets, with the spread
   across finals as its band. This rung exists because the two below it are measured on open
   queues of thousands: in a lobby of twenty, rank 20 is the last team, and the curve made
   the winner worth ten times the anchor.
5. **The scoring table alone**, for a cup nobody has seen. The threshold at rank 20 is a
   share of the most a team could score, with the share read from cups of the same kind,
   platform and stage — an FNCS final on PC is not a creator's mobile cup — and shrunk toward
   a prior with weight `n / (n + 2)`. This is the weak rung and the results below price it.

Two things the data settled on the way:

- **Field size barely matters** to the level: doubling the number of teams moves a threshold
  about 3.5 %. But it matters a great deal to the deep end of the ladder, which is why the
  measured shape table stops at rank 500 and the curve, which knows the field, takes over.
- **The level is one edition old, not a median.** Read from the previous edition, the level
  forecasts at 5.4 %; the median of the last three, 5.7 %; of the last eight, 6.4 %. Cups
  drift week to week, and history older than the last edition is evidence about last month.

---

## Results

A forecasting split, not a random one: the newest 600 tournaments — everything from 25 July
2026 on — predicted from the 6,632 before them. Nothing sees the future, not the model and
not the baselines. Forecast cold, before any reading of the live standings.

**Where the cup has run before** (2,286 thresholds over 307 tournaments):

| rank band | this model | previous edition | category median | n |
|---|---:|---:|---:|---:|
| 1 – 5 | 5.3 % | 5.4 % | 5.5 % | 876 |
| 6 – 25 | 3.6 % | 4.4 % | 4.8 % | 726 |
| 26 – 100 | 2.5 % | 3.6 % | 5.0 % | 261 |
| 101 – 500 | 4.1 % | 5.2 % | 7.3 % | 347 |
| beyond 500 | 7.3 % | 8.7 % | 9.8 % | 76 |
| **all** | **4.2 %** | **5.0 %** | **5.6 %** | **2,286** |

The model starts from the previous edition — that is rung 1 — and until it corrected that
edition for the field it *was* the previous edition wherever one existed at that rank, 5.0 %
against 5.0 %, a tie by construction. The field is what separates the two now: a known
number of teams that is not the edition's moves the value along the curve's quantile term,
and the model beats the carry-forward it is built on by 0.8 points, the whole interval
above zero (+0.47 to +1.34), and the category median by 1.45. What carry-forward still
cannot do it also does — a band, an answer for ranks last week did not publish, and an
answer for cups that have no last week.

These figures moved when the naming was fixed (both title lines, stage from the id): the
previous-edition error went from 5.7 % to 5.0 %, its mean from 46 % to 12 %, because the
"previous edition" is now the right cup rather than the one that shared a first word. The
category median went from 8.4 % to 5.6 % for the same reason, which is why the model's lead
over it shrank from 2.7 points to 0.6.

**Where the cup has never run** (2,897 thresholds over 283 tournaments — half the sample,
because a new season brings new formats): 20 % median error. Half of these are brand-new
cups, Solo Reload and mobile and creator events, forecast from their scoring table alone.
This is the rung to improve next.

**Finals in a single lobby the model had never seen** (103 thresholds over 23 tournaments)
used to go through that same scoring rung and its open-queue curve: 124 % median error, the
winner of a twenty-team Reload final priced at 2,900 points where 300 was the most anyone
could score. Read off the finals of the same format by share of the lobby, they come out at
13 % — 6 % once one cup whose leaderboard records 100 points for every finalist is set
aside — with 84 % of them inside the band.

**The band is honest.** 83 % of thresholds land inside a band that claims 80 %; at the 89 %
nominal level the empirical coverage is 90 %.

**The first comparable edition is worth 8.6 points** of median error (15.1 % with none,
6.5 % with one); the next five are worth 1.6 between them.

Measured on a random split instead — every tournament held out in turn with the rest as
history — the same model reads 7.0 % against 4.2 % for the nearest edition. That figure is
easier to like and wrong to publish: the nearest edition is often next week's, and next
week's result is not available on the night.

---

## Running it

Python 3.10 or later. No dependencies beyond Flask.

```bash
pip install -r requirements.txt
python src/app.py             # opens http://127.0.0.1:5000
```

Windows: double-click `tracker.bat`.

The training set comes from [Osirion](https://osirion.gg)'s free public Fortnite API — the
tournament calendar, each event's scoring rules, and the leaderboards. `harvest_osirion.py`
downloads it, restartably, and `data/` is git-ignored: the API's terms do not allow its
output to be republished, so the database is something you build, not something you clone.

```bash
python src/harvest_osirion.py --check              # three calls, says what it sees
python src/harvest_osirion.py --passes 3,10        # the calendar, then the standings — hours
python src/refresh.py --fetch --publish            # catch up, rebuild the model, update the site
python src/refresh.py --page --publish             # the page and the week's calendar, in seconds (site.bat): no model
python src/pull_live.py                            # the live feed's readings of the last days, into the database
python src/import_session.py session-*.json        # an evening followed on the predictor, into the database
python src/import_session.py --list                # which tournaments carry enough readings to test on
```

`refresh.py` is the day-to-day command: a shallow harvest pass for new windows, the
derivation, the export (refused unless it reproduces the model), this week's calendar, the
site build, and a push. Its docstring has the one-line Task Scheduler entry that runs it every
morning. Once a week is the floor, whatever else is skipped: the calendar the site ships
covers seven days, and the live feed reads it to know which cups are under way, so a calendar
older than that leaves the feed following nothing.

How a tournament is named decides which editions count as the same cup, so the rules are
few and written down. The name is both lines of Epic's title — "FNCS | Division 2" is not
"FNCS", and "Fortnite | Performance Evaluation" is a weekly cup, not the game. The stage
comes from the window id alone: Epic's `round` number beside it is a week counter, and
reading it as a stage once filed week 2 of a weekly cup as its "Round 2", one category per
week. When these rules change, the next build notices — the database carries the version
that built it — and re-derives every harvested tournament, about seven minutes, so old and
new editions never sit under different names. What a cup pays out on is read from the same
catalogue: rank tables count from 1, percentile tables are a share of the field, and
score-valued tables are not positions at all. The predictor shows those cuts and asks for
them first. Who may enter is read there too — Epic's `currentRanking:<ladder>:<n>` requirement,
kept as `competition.entry` — because the same cup admitting Unreal alone one week and Diamond
upwards the next is two fields of different sizes, and the first rung of the model reads the
previous edition with the same bar.

Live standings during a tournament come from the [Cito](https://citoapi.com) API — a free
key allows 500 requests a month, kept in `cito_key.txt`, which is git-ignored. Everything
except the live import works without a key.

The interface is English by default with a French switch in the header
([`i18n.py`](i18n.py) holds the translations).

### The research layer

```bash
pip install -r analysis/requirements.txt
python -m analysis.fit           # refit a and b, with intervals
python -m analysis.diagnostics   # residuals, clustering, heteroskedasticity
python -m analysis.validate      # forecast the newest tournaments against two baselines
python -m analysis.anchor        # why the level is read at rank 20
python -m analysis.shape         # is the shape a curve, a table, a function of the field?
python -m analysis.figures       # regenerate the figures
python -m analysis.live          # replay the boards game by game: what a threshold is worth mid-cup
```

`analysis/` uses numpy, pandas, scipy and matplotlib. The app does not: nothing under
`analysis/` is imported by `app.py` or the model, so the app still runs on a bare Python
install. The arrow points one way.

---

## Layout

Launchers, the database and the notes at the root; the code in `src/`, run by
path (`python src/refresh.py`); the research layer in `analysis/`, run as a
module from the root (`python -m analysis.validate`).

| | |
|---|---|
| `update.bat`, `site.bat`, `harvest.bat`, `tracker.bat` | the four things a person runs; `refresh.bat` is `update.bat` with options, and what the scheduled task calls |
| `data/` | the database, the harvested pages, the keys, the log — never committed |
| `src/app.py` | Flask routes, and the input validation every write endpoint goes through |
| `src/calibration.py` | the model: the five-rung cascade, the measured shape, level and closed-lobby tables, the curve fit |
| `src/predict.py` | forecasts during a live session — pace, extrapolation, my own race |
| `src/harvest_osirion.py`, `src/osirion.py` | the training set, from Osirion's public API, restartably |
| `src/refresh.py` | the one command: harvest pass, derive, export, calendar, build, push |
| `src/pull_live.py` | the live feed's readings — every cup it watched, read every ten minutes — filed against the tournaments the harvest built, matched on Epic's ids; `refresh.py` runs it |
| `src/import_session.py` | an evening followed on the predictor, read back into the database as readings and a result — readings typed by hand and those the site's live feed took (marked `auto`) alike |
| `src/calendar_snapshot.py` | the week ahead, written beside the predictor as `calendar.js` |
| `src/export_model.py` | `model.json` for the predictor, refused unless it reproduces the Python model |
| `src/scoring_infer.py` | recovers a tournament's scoring table from team totals by least squares |
| `src/db.py` | SQLite schema, migrations, the taxonomy, every query |
| `src/cito.py` | the live-standings API client |
| `src/i18n.py` | English source strings, French translations |
| `analysis/` | numpy/pandas/scipy: refit, diagnostics, cross-validation, figures; `validation.json` is what the last validation measured, carried into the model so the site quotes nothing it did not earn |
| `docs/` | [methodology note](docs/methodology.md), [user manual](docs/manual.md) |

Checks, all runnable:

```bash
python src/backtest.py       # walk-forward error on the tournaments you have tracked
python src/test_osirion.py   # the API reader, against payloads shaped like the real ones
python src/check_forms.py    # every form field survives the round trip to the database
python src/fuzz_api.py       # 2,856 malformed requests, 0 server errors
python src/selfcheck.py      # all of the above plus both languages, in one pass
python src/cleanup.py --list # what in the folder is no longer reachable from the code
```

`check_forms.py` exists for a reason worth repeating: a form field was once silently dropped
between the browser and the database, and an afternoon of hand-entered results went with it.
It now posts a recognisable value into every field of every form and reads them all back.

---

## What I would fix next

In the order the numbers justify:

1. **The cold start.** Half of a new season's tournaments have no previous edition, and they
   are forecast at 20 %. The signature that keys them — kind of cup, platform, stage — cut the
   share error from 12.0 % to 9.9 % in isolation and moved nothing in the forecasting split,
   because a new format's signature group is usually empty too. A nearest-neighbour over
   scoring table, field and games is the obvious next thing to try.
2. **The field is censored at 9,950.** Osirion's leaderboard stops at page 100, so every
   event above ten thousand rosters records the same size. The Cito endpoint returns whole
   boards and would lift the ceiling.
3. **`b` is not identified.** Across the day's refits it ran from 0.38 to 0.90 on the same
   kind of data. It matters less than it did — the curve is third in the cascade now — but a
   number that unstable is a working value, not a measurement.
4. **The live refinement is measured in one half and not the other.** Every harvested
   board carries each team's per-game history, so `analysis/live.py` can rebuild the
   standings at any moment of the session; it finds a threshold at half the games sits at
   half its final value, ±15 % from one cup to the next, and the predictor now scales its own
   ladder by what the readings say against that curve. Its first version pushed readings
   through the open-queue curve in a twenty-team lobby and turned 153 points at half time
   into 506. The same replay measures how far a reading travels along the ladder, and the
   answer differs by format: in an open queue the whole board moves together (slope 0.86
   between ranks) so one reading prices every rank; inside a closed lobby the ranks move
   independently (slope 0.00, correlation −0.09) because the same twenty teams share out a
   fixed pot, so a reading now refines its own rank and leaves the rest alone. What is still
   unmeasured is the blend of readings and history: it is precision-weighted, which is
   principled, and unvalidated on held-out cups, which is the next replay to run.

---

MIT licensed. Fortnite is a trademark of Epic Games; this project is unaffiliated with them
and uses no game assets.
