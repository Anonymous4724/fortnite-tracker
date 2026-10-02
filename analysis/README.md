# analysis/

The research layer. The app forecasts point thresholds for Fortnite tournaments;
this folder asks whether it deserves to be believed.

It is kept separate on purpose. `calibration.py`, `predict.py`, `db.py` and
`app.py` are standard library only, so the app runs wherever Python does. Nothing
here is imported by any of them — the arrow points one way, `analysis` reads the
app, never the reverse.

```
pip install -r analysis/requirements.txt

python -m analysis.data          # what is actually in the database
python -m analysis.fit           # refit a and b, with honest intervals
python -m analysis.diagnostics   # residuals, normality, clustering
python -m analysis.validate      # leave-one-tournament-out vs two baselines
python -m analysis.anchor        # is rank 20 the right rank to anchor on?
python -m analysis.shape         # is the shape a function of rank, or rank/field?
python -m analysis.figures       # PNG + SVG into analysis/figures/
python -m analysis.live          # replay the boards game by game -> pace.json, carried into the model
python -m analysis.rescore       # a cup nobody has seen: recent boards replayed under its table, against the cold rungs
python -m analysis.blend         # the weight of a cup's readings against its history -> blend.json, carried into the model
python -m analysis.coldbench     # the week's list against what each cup then scored, from the predictor's git history
python -m analysis.bench         # every cup of the last days priced again from the model online before it, against its final standings (--cold --weeks 6)
python -m analysis.bench --live  # every cup the live feed followed, replayed reading by reading through the page's live forecast, against its final standings
python -m analysis.bench_compare data/base.json data/variant.json   # two runs of the bench, forecast against forecast, and the gain rule's verdict (also bench --compare, with the same options)
```

The database is opened read-only. `analysis.figures` takes about 35 seconds
because the learning curve refits the pipeline a few thousand times; everything
else is a few seconds.

## The model under test

    threshold(rank) = level * exp(-a * (q**b - q_ref**b)),   q = rank / field

`level` is the tournament's threshold at rank 20, read as a median over ranks
5/10/20/25. `a = 1.255` and `b = 0.370` ship as constants in `calibration.py`.

## What each script answers

| file | question |
|---|---|
| `data.py` | The only module that runs SQL. Tidy frames: one row per (tournament, rank, threshold), with field size, scoring, region, mode, stage and date. |
| `fit.py` | Are `a` and `b` right? Profiled least squares in log space, Jacobian standard errors, a bootstrap over **tournaments** rather than rows, and a sensitivity table. |
| `diagnostics.py` | Where is the curve wrong? Residuals against fitted value, `q`, field size and date; normality; heteroskedasticity; and the intra-class correlation that tells you how much the naive standard errors understate. |
| `validate.py` | Does it beat carrying last edition forward, or the category median? Leave-one-tournament-out over the whole pipeline, error by rank band, band coverage against the claimed 80 %, and error against the size of the history. |
| `anchor.py` | Is rank 20 the right rank to read the level off? Dispersion of the pace between editions at each candidate rank, then the leave-one-tournament-out forecast re-run with the anchor moved, and a bootstrap on the margin. Also checks the figures `calibration.py`'s comments assert. |
| `shape.py` | Is the ladder's shape a function of rank, or of rank divided by the field? The shipped form makes the whole shape's amplitude proportional to `(20/field)**b`, so a database whose field sizes all agree cannot tell the two apart — and one whose fields span two orders of magnitude can. Fits both readings with the same estimator on the same within-tournament gaps, and checks whether the residual still carries the field. |
| `live.py` | What is a reading worth mid-cup? Rebuilds every harvested board at any moment of its session from each roster's own game times, and measures the share of the final threshold reached — on the wall clock for open queues, on games played for lobbies, per game count where enough lobbies played it — plus how much of a reading at one rank belongs to another. Then the part the harvest cannot see: how far Osirion's *published* board can still be from the final, measured on the feed's own evenings, because that is the board the page reads. Also the pace of each kind of cup — its family, per kind of cup and platform within it and pooled (an FNCS practice and a skin cup of the same format do not queue alike: 45 % against 56 % of the final at mid-session), its own recent editions, replayed from the harvest and, where the feed has followed it, read from the feed, which sees the mid-session board the harvest's first pages miss, each row with the format it was measured in — how far the deep end of a queue runs from the top by depth into the field, and how wide a live answer has to be, the last two on the feed's evenings. Writes `pace.json`. |
| `figures.py` | The four figures: fitted curve over the observed points, the residual panel, the coverage plot, the learning curve. |
| `blend.py` | How much should a cup's own readings weigh against the forecast made before it? The page weighs the two by the inverse of their widths squared, and the widths are not in the same units: a band is the width of a bad week, the pace's width the spread of a board at a given minute. On the evenings the feed followed, measures each side's typical error in units of its own width — by rung for the history, from the rolling validation's forecasts — and writes the scales the page applies before weighing (`blend.json`). A rung needs twenty cups to be scaled. |
| `coldbench.py` | How far off was the forecast the week's list gave before each cup started? Reads the predictor's own history — every version of `calendar.js` and `model.json` — and pairs the last forecast listed before each window opened with what the cup then scored, read from the first model made once its board had settled (an update that ran during the cup exported the board of that minute) and taken back out of the averaged previous edition the model stores. Signed and absolute error and the ranges' coverage by rank band, region, cup family, mode, entry bar, field, season, closed lobby and day of a round played over several days, and the groups that lean one way ranked by the error they cause. `--replay` prices the same windows again with today's code, `--calibrate` measures the later-day move on the windows over before a moment and prices the ones after it with and without it. Needs no database: a full clone of the predictor is enough. |
| `bench.py` | How far off is the forecast made before each cup, measured on the database? Where `coldbench` reads what the list published, this makes the forecasts again with today's code: each cup priced from the model of the last daily update online before it (18:00 Paris time, online half an hour later; `--update` tries other hours or two updates a day, `--cutoff day` gives the model `export_model.py --before` writes for the cup's day), as the page prices its row of the list — the row rebuilt from the Osirion catalogue on disk (cuts, entry bar, day of a round, the field a cut of the round before feeds), the replay of earlier boards for a cup new to its region, `calendar_tournament` and `predict_from_model` — at ranks 10, 25, 100, 250, 500 and 1,000, at the cut and at every deeper rank the final standings hold. Signed and absolute error and the ranges' coverage by rank band, the cut, region, mode, team size, the field the page used and where it guessed it from, rung, entry bar, day of a round, first week of a season, closed lobby and family, and the groups that lean one way ranked by the most error one factor on their forecasts would take away (an in-sample upper bound). A lean counts once per family and local day, so one event played in seven regions is one draw; the field a cup drew, known only once it was over, is shown but never offered as a bias. With no argument, the last seven days; `--cold --weeks 6` the last six weeks. Each model is kept in a cache keyed on the database, the code and the measured tables. The replays read the raw leaderboard pages the harvest keeps (`--leaderboards` points elsewhere); a run without them says so, and does not compare with one that had them. `--jobs N` prices the cutoffs in N processes, each with its own read-only connection and its own models (about 0.5 to 1.4 GB a process, depending on their size), and writes the same file one process writes; a run starts no more processes than it has cutoffs or the system has processors (61 at most on Windows), and says how many. `--variant FILE.py` prices the same cups a second time with the replacements the file's `apply()` makes to the app's functions, from the same database, catalogue and pages, writes that run beside the first (`out.variant.json`) and sets the two against each other; the variant's models are kept under a key that adds the file's SHA-1, unless it says `CHANGES_MODEL = False`, which is checked on the last model of the span. The file is read once, as the run starts, and that text is what runs, in every process, whatever becomes of the file meanwhile; it never runs until today's run is over and written: a variant that fails, exits or is refused leaves it written, with no `out.variant.json` beside it, and ends with an error code. `--cutoff published` prices each cup from the model the page served when it started, dated by the history of model.json in the page's repository (`--ladder`) and rebuilt with today's code from the rows the database held by then, from 5 September 2026, when the database began dating its rows. |
| `bench_live.py` | How far off is the forecast the page shows while a cup is running? `bench --live` replays every cup the live feed followed, reading by reading, from the model online before it and the cold bench's own cold forecast, through a port of the page's live forecast (the log of readings, the standing of each rank, the weighing of the readings against the history, the floor the board sets, the ranges) with the pace a rank is expected to have reached taken from `live.expected_share`. At a fifth, two fifths and so on to the close, then ten and twenty minutes past it, the answer the page would have shown with only the readings that had reached it by then (`--arrival`: two minutes after the feed's pass that could read them, the default, or at their stamp) - none called final before the window closed, no field but the one the list, the board's own count so far or the model gave - is set against the final standings, at every rank the feed read, ranks 10 to 1,000 and the cut. Error and the coverage of the live ranges by point of the session, region, depth into the field, rank band, the basis of the answer and family; a forecast where `live.py` and the page part ways is flagged with the page's own number beside it. The cold bench's options, models and cache, but --cutoff published, --jobs and --variant, which it refuses. |
| `bench_compare.py` | Is a change to the forecast better, on the same cups priced twice? Takes two runs of `bench` written with `--json` (also `bench --compare a.json b.json`) and refuses them unless they were made from one snapshot: the same database and -wal, catalogue, raw pages (as many events on disk, and the variant has read every board the base read, with the same content), span, update rule, ranks, range multipliers and, for a run that says it, rule of arrival; runs of the live bench are refused, not paired yet. Pairs the forecasts by cup and rank, refuses a pair whose two sides do not aim at the same result, cup, day, region and cut, and gives each side and the change: signed, absolute and log error (mean and median), the ranges' coverage, the results under ten points apart; the 90 % interval of the paired change from 1,000 draws of the cups with a fixed seed; the change by half of the span, by season, region, rank band and rung; and the verdict of the gain rule (the mean and median absolute error and the mean log error down overall and in each half; the median and the log down in most seasons), with the conditions a change fails. `--strawman run.json` writes the run corrected by the naive walk-forward correction every idea has to beat (the median signed error of its rung, rank band and region over the cups over before each model, shrunk by n / (n + 50); the cups' end times come from the app's database, or the one `--db` names) and sets it against the run. |
| `rescore.py` | Is a cup nobody has seen better priced by replaying recent boards under its table than by the scoring rung? Takes the cups since mid-August that the rolling validation had to price off the scoring table or the family, replays each from the boards of its region played before it (`src/rescore.py`), and compares both with the truth by rank band — the replay read straight where the boards reach, continued along the ladder past their depth. |

Above `validate.SAMPLE` tournaments the cross-validation holds out the newest
600 and forecasts each of them from everything that started on an earlier day —
the pool before the window plus the held-out tournaments already played, the way
the app has last week's edition on the night — with the curve fitted once, on
the pool alone, so no target ever shapes the curve used to predict it.
`--frozen` forecasts all 600 from the pool alone instead; on the same evenings
that reads about twice the error, because a weekly cup's fifth week is then
priced off the season before and every edition of a cup born inside the window
counts as a cold start. `--random` is the old leave-one-out.

## What came out of it

Measured on the 66 tournaments and 290 hand-entered thresholds in
`data/tracker.db`.

**The shipped constants survive.** Refitting with a free level per tournament
gives `a = 1.346` (95 % CI `[1.225, 1.507]`) and `b = 0.460` (`[0.350, 0.574]`),
bootstrapped over tournaments. The shipped `1.255` and `0.370` are both inside,
`b` near the lower edge. The estimator in `calibration.fit_curve` — which divides
by the *observed* rank-20 threshold instead of treating the level as a free
parameter — sits 7.8 % low on `a` and 18.2 % low on `b`, which is the attenuation
you get from dividing by a noisy measurement.

**But `b` is not really identified.** 214 of 225 observations sit at `q < 0.25`,
and the exponent is what governs the curve deep in the standings. Restricting to
`q <= 0.25` moves `b` from 0.460 to 0.285; to `q <= 0.10`, to 0.242. That is a
wider swing than any single confidence interval admits. `b = 0.370` is a working
value, not a measurement.

**Out of sample, before the first reading:** median absolute error 6.4 %, mean
11.7 %, over 290 thresholds in 66 tournaments. By rank band the median runs 8.7 %
in the top 5, 5.7 % in the top 6-25, 9.5 % from 26 to 100, 6.0 % from 101 to 500,
2.2 % beyond 500. The mean is roughly double the median everywhere: the error
distribution has a tail.

**Against the baselines** (206 rows where all three answer): model 5.68 %,
carry-forward 6.03 %, category median 7.56 %. Bootstrapped over tournaments, the
gap to carry-forward is +0.36 points with a 95 % interval of `[-3.24, +4.07]` —
not a win. The gap to the category median is +1.88 `[-1.74, +4.64]`. Where the
model does earn its keep is beyond rank 500: 2.0 % against 9.5 %, because the
curve extrapolates to ranks a carry-forward has never seen — and on the 84 of 290
rows where no other edition of the category exists at that rank, it is the only
thing that answers at all.

**The bands are honest, slightly wide.** 86 % of thresholds land inside a band
that claims 80 %; the standardised error has sd 0.83.

**Rank 20 is defensible but not special.** Re-running the whole cross-validation
with the level read off one rank at a time: rank 1 gives 9.18 % median error,
rank 5 7.30 %, rank 10 6.65 %, rank 20 6.50 %, rank 25 6.32 %, rank 50 8.04 %.
The shipped median over ranks 5/10/20/25 gives 6.42 %. Bootstrapped over
tournaments, ranks 10, 20 and 25 are within half a point of the shipped setting
and of each other — the choice among them does not matter. Only rank 1 is beaten
outright, by 2.8 points with the whole interval below zero. Rank 100 cannot be
tested at all: two tournaments carry it, and `fit_curve` silently reverts to the
shipped constants. These numbers reproduce what `calibration.py`'s comments
assert (9.1 %, 6.4–6.6 %, 6.0 %) to within a few tenths.

**Two things are genuinely fragile.** The cold start — no comparable edition, so
the level comes from `REFERENCE_SHARE = 0.58` — runs at 34.6 % median error on 15
observations. `calibration.py` already says that constant rests on three
readings; this is the size of the bill. And the live models are effectively
untested: 64 of 66 tournaments carry a single "reading" that is a copy of their
own final result, so backtesting an extrapolator on them measures nothing. Two
evenings were genuinely tracked.

**Standard errors on any of this are worth 1.9x what they look.** Once the level
has to be borrowed from comparable tournaments, 80 % of the forecast variance is
between tournaments rather than between ranks — every rank of one tournament is
wrong in the same direction. 290 thresholds are 66 evenings.
