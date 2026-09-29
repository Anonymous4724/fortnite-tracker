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
| `update.bat` | everything — a harvest pass for the new tournaments, the model rebuilt and verified, the week's calendar, the page, the push of both repositories (the site's and this one); ten minutes |
| `site.bat` | the page and the week's calendar only, and the same push; seconds |
| `harvest.bat` | the long download of past tournaments, a night |
| `tracker.bat` | the local tracker app |

**Measured as a forecast — each of the newest 600 tournaments predicted from everything
that had finished before its day, nothing seeing the future: 2.8 % median error where the cup
has run before, 5.2 % over every threshold, 94 % of real thresholds inside the quoted range.**
Where the cup has never run in its region, recent boards of its format are replayed under
its own scoring table and the error is 4 to 5 % at ranks 1–250, about 6 % to rank 1,000,
against 8 to 11 % for the scoring-table rung it replaces (102 cups since 15 August, each read from
boards played before it). The training set is 7,632
tournaments and 94,286 thresholds read from Osirion's public API (22 September 2026; the
figures are re-measured every three days by the refresh, `python -m analysis.validate`, and
travel with the model).
[`docs/methodology.md`](docs/methodology.md) says how that was measured and where the model
stops being believable.

![Fitted curve over the observed thresholds](analysis/figures/curve.png)

---

## The model

A cascade, most direct reading first. Each rung answers only when the one above it cannot.

1. **The previous edition, read straight.** Same cup, same region, same format, same rank,
   last time. The band is the 80th percentile of how much that rank moved between consecutive
   editions of that cup, measured rather than assumed. A cup that has never run in its format
   — the first Duo week after a Trio season, a Reload week of a Battle Royale cup — reads its
   last edition in another one with the band widened by half, unless it is a single lobby,
   which rung 4 prices better than a lobby of another size does. This rung is the carry-forward baseline promoted
   to the model, and it is first because it kept winning: a strong evening lifts rank 1 and
   rank 20 together, and any forecast that multiplies one edition's level by a ratio taken
   from others throws that correlation away. Three corrections since: the edition read is the
   last one with the same entry bar as the cup coming up (`competition.entry`, read off
   Epic's `currentRanking` requirement — a cup admitting Unreal alone is another field than
   one open from Diamond) when it was played in the same season as the latest — a season
   back, a same-bar edition reads worse than last week's under the other bar, measured —
   the band widened by half otherwise; a known field that is not the edition's moves the
   value along the curve's quantile term, capped at a fifth, which on the newest 600
   tournaments cuts this rung's error at those rows from 6.3 % to 4.7 % — the reference-rank
   term must stay out of it, since taken as a ratio it says a smaller field lifts the top
   ranks, and the data refuses that; and an edition read across a turn of season is moved by
   what the season's earlier cups showed at that band of rank (the median move, shrunk by
   n / (n + 10)), its band widened by what the move leaves — a new season lifts every cup at
   once, +5 % at the top and +11 % past rank 500 at season 42, nothing at all at season 41,
   and it cannot be known before the season's first cups have run. Each rank of the table
   carries the season and date of the edition it was read off, because the latest edition
   is read as deep as it was harvested and a rank past that comes from an older one; the
   page says which. See `docs/methodology.md`, "The first edition of a season". And since 22
   September the edition is smoothed: the run of editions played the same way as the latest
   — same entry bar, season, number of games and scoring table — averaged in log terms with
   the latest weighing 0.7 and each one before it 0.7 of what is left, six at most. The
   median error does not move and the tail shrinks: on the 1,711 thresholds it changes in
   the rolling validation, mean 7.24 → 6.59 %, 90th percentile 14.0 → 13.4 %.
2. **Level times measured shape.** The cup's threshold at rank 20 from its previous edition,
   times what that rank was worth relative to rank 20 across the cup's editions — a lookup
   table, not a curve, because the table halved the error where it applies. Category first,
   then the family across regions.
3. **Level times the ladder**, for ranks no edition has measured: what each rank is worth
   relative to rank 20 across every open queue of the same band of field size (eleven
   bands, finer than the curve's: rank 1,000 is the last place of an 1,100-team queue and
   the middle of a 2,900-team one) and game mode — one median per rank over at least twenty
   boards, the game mode's row first and the band's row across game modes as the fallback.
   It replaced the fitted curve

       threshold(rank) = level · exp(−a · (q^b − q_ref^b)),    q = rank / field

   at the deep end, where the curve ran 9 % low at rank 500 and 13 % low at rank 1,000 out
   of sample (two parameters cannot bend both ends of a ladder) and the ladder runs within
   1 %; the curve, still fitted per band of field size, answers for queues under three
   hundred teams and for a band and rank the ladder has too few boards at.
4. **Recent boards replayed under the cup's table**, for a cup with no finished edition in
   its region. Every harvested board carries each roster's placement and eliminations in
   each game it played; re-scored under the new cup's table — the first *n* games where the
   cup allows *n* — the same games give the standings that table would have produced, and
   rank 20 of them is a forecast for rank 20 of the new cup. Donors: the six most recent
   boards of the same region, team size, game mode and platform, open queues, those played
   to the same number of games first and the others scaled by the per-game exponent; the
   median across donors, trusted down to a third of the rosters loaded and continued along
   the ladder past that. Where the cup has run in other regions, half of that reading and
   half of this one, in log terms, beat either alone. `calendar_snapshot.py` writes the
   replay beside the week's rows, a dozen numbers per cup; the page reads it as this rung.
5. **The closed lobby**, for a final played in a single lobby that no edition has been seen
   of — the usual case for a Round 2 whose Round 1 the model knows. Every threshold of every
   single-lobby final in the training set is divided by the most a team could score over the
   games played and bucketed by the share of the lobby the rank is, per game mode and team
   size; the estimate is read off that table, log-linearly between buckets, with the spread
   across finals as its band. This rung exists because the two below it are measured on open
   queues of thousands: in a lobby of twenty, rank 20 is the last team, and the curve made
   the winner worth ten times the anchor. The last places of a lobby — past 90 % of it — get
   no number unless the previous edition published them: they are teams that left after a
   game or two, and every rung priced them four times too high.
6. **The scoring table alone**, for a cup nobody has seen and no board to replay. The
   threshold at rank 20 is a share of the most a team could score, with the share read
   from cups of the same kind, platform and stage — an FNCS final on PC is not a creator's
   mobile cup, and a cup's second round, a few hundred qualified teams in a shorter session,
   is not its open round — and shrunk toward a prior with weight `n / (n + 2)`. Single
   lobbies do not feed that share: a heat of sixteen has no rank 20, and read through the
   curve it once set the mobile cold start at a tenth of what it is. This is the weak rung —
   13 % low on Battle Royale skin cups, 17 % high on Reload — and the replay above exists
   because of it.

Two things the data settled on the way:

- **Field size barely matters** to the level: doubling the number of teams moves a threshold
  about 3.5 %. But it matters a great deal to the deep end of the ladder, which is why a
  cup's own shape table stops at rank 500 and the pooled ladder, keyed on the field, takes
  over.
- **The level is one edition old, not a median.** Read from the previous edition, the level
  forecasts at 5.4 %; the median of the last three, 5.7 %; of the last eight, 6.4 %. History
  older than the last edition counts only while it was played the same way, and then only
  with the latest edition weighing most — the smoothing of rung 1, which keeps the median
  and trims the tail; a trend carried forward lost everywhere: cups wobble, they do not drift.

---

## Results

A rolling forecast, not a random split: the newest 600 tournaments — everything from 17 August
2026 on — each predicted from the 7,032 before the window and from every held-out tournament
that started on an earlier day, which is what the app has on the night. Nothing sees the
future, not the model and not the baselines. Forecast cold, before any reading of the live
standings. (The split used to freeze the pool at the first held-out day, so a weekly cup's
fifth week was priced off the season before and every edition of a cup born inside the
window counted as a cold start; on the same evenings and the same code that read 12.9 %
against 6.8 % — a number about the split. `--frozen` still prints it.)

**Where the cup has run before** (3,469 thresholds over 369 tournaments, the rows where the
model and both baselines answer; 22 September 2026):

| rank band | this model | previous edition | category median | n |
|---|---:|---:|---:|---:|
| 1 – 5 | 4.3 % | 4.2 % | 4.8 % | 1,148 |
| 6 – 25 | 2.1 % | 2.3 % | 3.7 % | 975 |
| 26 – 100 | 2.2 % | 2.4 % | 6.1 % | 496 |
| 101 – 500 | 2.6 % | 3.1 % | 8.6 % | 648 |
| beyond 500 | 5.6 % | 9.2 % | 12.0 % | 202 |
| **all** | **2.8 %** | **3.1 %** | **5.6 %** | **3,469** |

The model starts from the previous edition — that is rung 1 — and beats the carry-forward it
is built on by 0.28 points, the whole interval above zero (+0.05 to +0.53), and the category
median by 2.8; the top five ranks are the one band where last week read straight is as good. The lead is the field: a known number of teams that is not the edition's
moves the value along the curve's quantile term. What carry-forward still cannot do it also
does — a band, an answer for ranks last week did not publish, and an answer for cups that
have no last week.

**Where the cup has never run** (1,950 thresholds over 159 tournaments, the first day of
every new cup in every region): 10.7 % median error from the scoring table alone, 96 % inside
the band. The validation does not replay boards, so this is the rung the replay above
replaces on the site, at 4 to 5 %.

**Finals in a single lobby the model had never seen** (166 thresholds over 34 tournaments)
used to go through that same scoring rung and its open-queue curve: 124 % median error, the
winner of a twenty-team Reload final priced at 2,900 points where 300 was the most anyone
could score. Read off the finals of the same format by share of the lobby, they came out at
7 % then, and at 13.5 % on the newest 600 (175 thresholds over 29 finals, 79 % inside the
band); the last places of a lobby are refused rather than priced.

**The band is wide.** 94 % of thresholds land inside a band that claims 80 %; at the 89 %
nominal level the empirical coverage is 97 %. The page's 50 % and 90 % ranges are measured
directly, as quantiles of the error in units of that band, so they are the widths they claim.

**The first comparable edition is worth 0.9 points** of median error (6.8 % with none, 6.0 %
with one, on the tournaments with at least six peers); the next five are worth 0.5 between
them.

Measured on a random split instead — every tournament held out in turn with the rest as
history — the nearest edition is often next week's, and next week's result is not available
on the night. That figure is easier to like and wrong to publish.

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

A window read before its cup has finished is read again. Asked about a cup that has not
started, the API answers with an empty board and `totalPages: 0`; asked while it runs, with
a board that is still moving. Either one, kept on disk, was taken by every later pass as
"this window is done", so a calendar harvested a day ahead froze every cup in it as empty
and none of them ever entered the database. A page written before the window's end plus
half an hour is now treated as no answer at all, and the window is downloaded again from
page zero.

The page each cut falls on — the qualification rank, the prize ranks — is fetched with the
first pass, one request per cut, and the cut's rank is recorded beside the fixed fifteen:
the cut is the rank people ask the model about, and a first pass three pages deep never
reached a cut at four thousand, so the model read that rank off whichever older edition held
it. A window already in the database is derived again, in place, when more of its pages are
on disk than the last derivation read — the deeper passes, the cuts' pages — and a rebuild
derives every window in place too: the row stays, and with it the live feed's readings,
which an earlier version deleted along with the row.

```bash
python src/harvest_osirion.py --check              # three calls, says what it sees
python src/harvest_osirion.py --passes 3,10        # the calendar, then the standings — hours
python src/refresh.py --fetch --publish            # catch up, rebuild the model, update the site
python src/refresh.py --page --publish             # the page and the week's calendar, in seconds (site.bat): no model
python src/pull_live.py                            # the live feed's readings of the last days, into the database
python src/rescore.py --event ID --window ID       # a finished cup replayed from the boards before it, against its board
python -m analysis.rescore                         # the replay against the cold rungs, out of sample, since mid-August
python src/import_session.py session-*.json        # an evening followed on the predictor, into the database
python src/import_session.py --list                # which tournaments carry enough readings to test on
```

`refresh.py` is the day-to-day command: a shallow harvest pass for new windows — three pages
each, ten for the last three weeks' windows, which are what the replay of a new cup is made
of — the derivation, the feed's readings, every three days the pace tables, the validation
and the weights of a live answer (`analysis.live`, `analysis.validate`, `analysis.blend`), the export
(refused unless it reproduces the model), this week's calendar with the replay beside each
new cup, the site build, and a push. Its docstring has the one-line Task Scheduler entry that
runs it every morning. The calendar is what the live feed reads to know which cups are under
way, and a cup announced between two runs would run unfollowed: the predictor repository's
own workflow refreshes it from GitHub every three hours, keeping the replay cells written
here, and `refresh.py` takes those commits in before writing anything.

How a tournament is named decides which editions count as the same cup, so the rules are
few and written down. The name is both lines of Epic's title — "FNCS | Division 2" is not
"FNCS", and "Fortnite | Performance Evaluation" is a weekly cup, not the game. The stage
comes from the window id alone: Epic's `round` number beside it is a week counter, and
reading it as a stage once filed week 2 of a weekly cup as its "Round 2", one category per
week. When these rules change, the next build notices — the database carries the version
that built it — and re-derives every harvested tournament, about seven minutes, so old and
new editions never sit under different names. Epic renames cups between seasons and keeps
the id — "Solo Victory Cup" became "Solo Victory Cup Battle Royale" at season 42, "FNCS
Division 2" became "FNCS Division 2 Practice" — so every edition of a series is filed under
the name Epic gives the cup now, keyed on the series id read out of the event id; a row typed
by hand under a former name follows, and so do the cuts entered for it. Under the name of
the day, a renamed cup started every new season with no history at all. What a cup pays out on is read from the same
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
python -m analysis.blend         # how much a cup's readings weigh against its history, on the feed's evenings
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
| `src/calibration.py` | the model: the six-rung cascade, the measured shape, the pooled ladder by field size, level and closed-lobby tables, the curve fit |
| `src/predict.py` | forecasts during a live session — pace, extrapolation, my own race |
| `src/harvest_osirion.py`, `src/osirion.py` | the training set, from Osirion's public API, restartably |
| `src/refresh.py` | the one command: harvest pass, derive, export, calendar, build, push |
| `src/pull_live.py` | the live feed's readings — every cup it watched, read every ten minutes, with the board's page count and, where the API pages it whole, the exact count of rosters at each reading — filed against the tournaments the harvest built, matched on Epic's ids; the board twenty minutes past the close is filed as the final at the ranks the harvest holds nothing for; `refresh.py` runs it |
| `src/import_session.py` | an evening followed on the predictor, read back into the database as readings and a result — readings typed by hand and those the site's live feed took (marked `auto`) alike |
| `src/calendar_snapshot.py` | the week ahead, written beside the predictor as `calendar.js`, with the replay table beside each cup that has no edition in its region; without the database (the repository's workflow) it keeps the tables the last run wrote |
| `src/rescore.py` | a cup nobody has seen, priced by replaying recent boards of its format under its own scoring table |
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

1. **The cold start, what is left of it.** A cup with no edition in its region is now
   replayed from the region's recent boards under its own table, at 4 to 5 % where the
   scoring rung read 8 to 11 %. What remains: the donors are the season's own boards, so
   the first cold cups of a new season replay the season before, unmoved by the shift the
   direct rung applies; a format with no board on disk — Arena cups, whose number of games
   is not fixed — still falls to the scoring table and its 14 %; and the replay is trusted a
   third as deep as the boards are loaded, which is why the harvest reads the newest
   windows ten pages deep.
2. **The field is censored at 9,950.** Osirion's leaderboard stops at page 100, so every
   event above ten thousand rosters records the same size. The Cito endpoint returns whole
   boards and would lift the ceiling.
3. **`b` is not one number.** Fitted by band of field size it runs from 1.25 under a
   thousand teams to 0.15 above the API's ceiling, which is why the curve is now fitted per
   band. Within a band it is a working value rather than a measurement — the bands were
   drawn by hand, and the ceiling band's `q` is a convention.
4. **The live refinement, measured.** Every harvested
   board carries each team's per-game history, so `analysis/live.py` can rebuild the
   standings at any moment of the session; it finds a threshold at half the games sits at
   half its final value, ±15 % from one cup to the next, and the predictor now scales its own
   ladder by what the readings say against that curve. Its first version pushed readings
   through the open-queue curve in a twenty-team lobby and turned 153 points at half time
   into 506. The same replay measures how far a reading travels along the ladder, and the
   answer differs by format: in an open queue the whole board moves together (slope 0.86
   between ranks) so one reading prices every rank; inside a closed lobby the ranks move
   independently (slope 0.00, correlation −0.09) because the same twenty teams share out a
   fixed pot, so a reading now refines its own rank and leaves the rest alone. The blend
   of readings and history is precision-weighted, and since 22 September the two widths are
   put in the units of each side's typical error, measured every three days on the feed's
   own evenings (`analysis/blend.py`): replayed through the page on a held-out week, the
   median error at mid-session went from 3.4 to 3.1 %, the forecast of a cup with a
   previous edition travelled 12 % over its evening instead of 17 %, and the ranges — now
   measured on live answers (`pace.live_bands`) — held 46 % and 88 % of the finals where they
   claim 50 % and 90 %. The 90th percentile late in the session is half a point worse, which
   is what the next replay should look at. One piece measured apart is the end of a cup: the settling read off the games' own end
   times says the board is final and certain twenty minutes past the buzzer, while the board
   the page actually reads is Osirion's published copy, which lands minutes later and can be
   5 % short of the final. That width is measured on the feed's own evenings instead
   (`pace.tail_feed`), split on whether the standing has been read again unchanged.
5. **The pace is the pace of a kind of cup, not of every cup pooled.** Replayed per family —
   game mode, team size, window length, game cap — the share of its final a threshold has
   reached at half time runs from 0.42 to 0.53, and on the hour from 0.87 to 0.94: a two-hour
   Battle Royale cup has a thirty-minute game in the air at the buzzer, a cup capped at ten
   short Reload games has nothing left to play. `analysis/live.py` now measures the curve and
   the tail per family — and per kind of cup and platform within it, because at half the
   session the FNCS practice cups had reached 45 % of their final where the skin cups of the
   same format had reached 56 %, and a family pooled over both priced every new skin cup high
   for two hours — and per cup (its most recent editions, within 45 days and of the same
   format, else the family's), and — because the harvest
   replays a board from the rosters that finish in its first pages, and in a casual Solo cup
   the players at the top at half time who then stop are not on disk, so the replay runs 4 to
   9 % low there — per cup as the feed itself read it, which the page prefers once four
   evenings have been followed. The deep end of an open queue runs on its own clock, set by
   the depth into the field rather than the rank: the thousandth of a field of two thousand is
   done by the last fifth of the session, the thousandth of ten thousand keeps the top's pace.
   That ratio is measured per band of rank / field on the feed's evenings (`pace.depth`), and
   the field it needs is the board's own page count, which the feed keeps with every reading
   now. Two corrections come from the first day of the FNCS Solo qualifiers: at the API's
   ceiling (9,950 counted for ten thousand or more) no rank is read as the casual half, and
   in an FNCS qualifier, whose deep end plays for the cut, a rank is read no deeper than the
   0.1–0.2 band — an ordinary cup's top fifth — of a table now measured on the other cups
   only. The ranges of a live answer have their own multipliers (`pace.live_bands`), measured
   on the same evenings. Rolling backtest on 110 evenings: median error 5.0 → 4.2 % at three
   to five tenths of the session, 4.7 → 3.6 % at five to seven, 2.1 → 1.4 % in the ten minutes
   after the close. See `docs/methodology.md`, "The pace of a kind of cup".

---

MIT licensed. Fortnite is a trademark of Epic Games; this project is unaffiliated with them
and uses no game assets.
