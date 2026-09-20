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
