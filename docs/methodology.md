# Forecasting Fortnite tournament point thresholds

A two-parameter curve, a borrowed level, and an account of what the two are worth.
Every number below is printed by a script in
[`analysis/`](../analysis/README.md), named beside the claim; where one has no
script behind it, I say so.

---

## Revision, September 2026 — the harvested training set

Everything below this section was written for the original study: 66 tournaments and 290
thresholds entered by hand. It is kept because it is how the curve was found and it is still
right about the curve. The figures it quotes are superseded by these.

### Data

7,232 tournaments and 79,891 thresholds, every one read from Osirion's public Fortnite API:
the calendar, each window's scoring rules, and the top 300 of each leaderboard, across eight
regions and every format Epic ran from December 2025 to September 2026. Ranked Cups — 1,086
of them — are set aside: Epic runs one per ranked division under one title, so their editions
are not comparable and their category level describes no tournament anyone plays.

Four defects in the harvest were found, three by the model losing to a baseline that does
not use the defective field and one by reading the calendar the predictor shows, and each is
now guarded against:

- The field size recorded the harvest's depth, not the event's — 300 rosters for every
  tournament. The model's only positional variable is `rank / field`. Now read from the
  board's page count; refused at export when the sizes are flat.
- Zero thresholds — teams that registered and never played — were stored, and a
  multiplicative model cannot express them. Dropped.
- Round 1 was read as a numbered stage from the window id and as no stage from the label, so
  the same cup was split in two. Collapsed on both paths.
- The name was the first line of Epic's two-line title, and the stage fell back on Epic's
  `round` number. The first merged every cup that shares a first word — three FNCS divisions
  under "FNCS", a weekly cup under "Fortnite" — and the second is a week counter, which filed
  week 2 of a weekly cup as its "Round 2", one category per week. Both title lines now make
  the name; the stage comes from the window id alone; the database carries the version of
  these rules that built it and re-derives everything when they change. Under the corrected
  names the previous-edition error went from 5.7 % to 5.0 % and its mean from 46 % to 12 %,
  the category-median baseline from 8.4 % to 5.6 %, and the share of cold starts from 50 % to
  53 % — some "previous editions" had been a different cup.

The field remains censored at 9,950 rosters, the API's hundredth page.

### The model, revised

The single curve `level · exp(−a(q^b − q_ref^b))` is now the third of five rungs. Measured on
the harvest it fits within 1.6 % near rank 20 and drifts to 16 % at rank 1 and 22 % past
rank 110, with a systematic bias of +12 % at rank 1 — one exponent cannot bend enough for
three decades of rank. A lookup table of what each rank was worth relative to rank 20, per
category, halved that error (4.0 % against 9.2 %) and is rung 2. Rung 1 is the previous
edition of the same cup at the same rank, read whole: a strong evening lifts every rank
together, and any forecast that multiplies one edition's level by a ratio from other editions
throws that correlation away. Rung 5, the scoring table alone, is keyed on the kind of cup,
platform and stage rather than on game mode and team size only.

Rung 4 was added when the name matching was tightened and a Reload final of twenty teams
stopped borrowing, by accident, the previous edition of a differently named cup: without
that accident it fell through to rung 5, whose anchor sits at rank 20 and whose curve is
fitted on fields of thousands, and rank 5 came out at 1,667 points in a format where 300 is
the most a team can score. Single-lobby finals now have their own table — every threshold
of every such final in the training set, as a share of the most a team could score over
the games played, by share of the lobby the rank is, per game mode and team size — read
log-linearly between buckets, the last places left out because a team that left after two
games is not a rung of the ladder. On the 103 such thresholds in the chronological sample
the error went from 124 % to 13 % (6 % without one cup whose leaderboard records 100 points
for every finalist), with 84 % inside the band.

`analysis/shape.py` is the diagnostic that established all of this, with a negative and a
positive control for each claim.

Rung 1 learned two things from a Solo Victory Cup whose forecast opened at 156 and closed
at 210. The previous edition had admitted Unreal players alone and drawn 21,000 of them;
the cup coming up admitted Diamond upwards and drew 33,000, and rank 4,000 of 33,000 is a
stronger team than rank 4,000 of 21,000. First, the entry bar is now read off Epic's
`currentRanking:<ladder>:<n>` requirement and stored with every harvested edition
(`competition.entry`; the older ladders stop at Unreal = 17, the "combined" ones split
Elite and Champion in three and put Unreal at 21 — read off the catalogue, where a Diamond
test cup asks 12 on both, the Elite ranked cups 15 on the old one, the Unreal cup 17 on the
old one, and the Unreal-only week 21 on the combined one). The edition read is the last one
with the same bar; when none exists the band widens by half. Second, a known field that is
not the edition's moves the value along the curve: `exp(−a((r/F_now)^b − (r/F_then)^b))`,
the quantile term alone. The first attempt took the ratio of two `shape_ratio`s, whose
reference-rank term also moves with the field, and that ratio claims a smaller field lifts
the top ranks; on the newest 600 tournaments read from the 6,632 before them, the 1,499
rows where both fields are known counts (the API pages a board a hundred pages deep, so a
harvested field of 9,900 or more is a ceiling, not a count) went from 6.3 % median error
uncorrected to 8.2 % with that ratio. With the quantile term alone the same rows go to
5.0 %, and to 4.7 % with the move capped at a fifth in log terms — the fitted slope of
truth on move is 0.81, and beyond a fifth the curve extrapolates past where the data goes.
Three-tenths of the move is added to the band in quadrature for the same reason. Over all
warm rows the model's median went from 5.00 % to 4.17 %, and for the first time it beats
the carry-forward baseline it is built on, by 0.83 points with the whole interval above
zero.

### Validation, revised

Random leave-one-out was replaced by a forecasting split. The random split let the
carry-forward baseline read the *nearest* edition in time — often next week's — and let the
model read the newest editions in the database rather than the newest before the target. It
is the standard mistake for a forecasting model and it biased the comparison in both
directions. The newest 600 tournaments (from 25 July 2026) are now forecast from the 6,632
before them; nothing sees the future.

| | median error | n |
|---|---:|---:|
| cup has run before — model | **4.2 %** | 2,286 |
| cup has run before — previous edition | 5.0 % | 2,286 |
| cup has run before — category median | 5.6 % | 2,286 |
| cup has never run — model, from the scoring table | 20 % | 2,794 |
| single-lobby final never run — model, from the closed-lobby table | 13 % | 103 |

The model is 0.8 points ahead of the previous edition read straight, an interval whose
whole span is above zero (+0.47 to +1.34, P = 1.00), and 1.45 points ahead of the category
median. Before the field move of rung 1 it *was* the previous edition wherever one existed
at that rank — 5.0 % against 5.0 %, the tie noted in the earlier section — so the field is
what separates the model from its own baseline. Band coverage is 85 % against a claimed
80 %. (Before the naming fix the same split read 5.7 %, 5.7 %, 8.4 % and 19 %, with the
model 2.7 points ahead of a category median that was being fed other cups' editions.)

The first comparable edition is worth 8.6 points of median error; the next five are worth
1.6 between them. The level is read from the previous edition alone: the median of the last
three costs 0.3 points, of the last eight 1.0. Cups drift.

These headline figures are written to `analysis/validation.json` by the validation and
carried into `model.json` by the export, so the predictor quotes the measurement rather
than a number typed into its source.

### What would move the needle now

1. The cold start, at 20 % on half of a new season's tournaments. A nearest-neighbour over
   scoring table, field and games is the obvious candidate.
2. The field ceiling. Cito's endpoint returns whole boards.
3. The live refinement's blend. Its pace curve is now measured — `analysis/live.py` rebuilds
   the standings of every harvested board at any moment from the per-game histories the API
   returns, and on the first 44 boards a threshold at half the games sits at 0.50 of its
   final value (p10–p90 0.46–0.55 at rank 5), open queues running linearly on the wall clock
   and closed lobbies linearly on games played. The predictor scales its own cold ladder by
   what the readings say against that curve and combines the two by precision. The first
   version de-shaped readings through the open-queue curve and, in a twenty-team lobby where
   that curve is five times too steep, turned 153 points at half time into 506.

   How far a reading travels along the ladder is measured by the same replay. Write
   `rho_r = observed_r / (share × final_r)` for the error the pace curve alone makes at rank
   r, and regress `log rho_b` on `log rho_a` across boards. In an open queue the slope is
   0.86 pooled and ~1.00 between any of ranks 3 to 25: the board moves as one, and a reading
   anywhere prices everywhere. In a closed lobby it is 0.00 (correlation −0.09 over 20
   boards): the same twenty teams share out a fixed pot, so a runaway leader takes the points
   that would have landed at rank 10, and the ranks move independently. The predictor carries
   the measured slope and nothing else, so a reading in a sealed final now refines its own
   rank and leaves the rest of the ladder alone.

   Two warnings on that measurement. The pooled slope is computed on ranks 1–25 only: with
   one page of rosters read per board, rank 100 of a two-thousand-team cup is the last row
   downloaded rather than the last row there is, and including it dragged the open-queue
   slope from 0.86 to 0.34 on an artefact of harvest depth. And an earlier version of the
   function centred each board on its own median, which removes exactly the common factor
   being looked for and reported no carry anywhere — including for the open queues where it
   is nearly perfect.

   A rank read on both sides is priced between the two readings, log-linear in rank, before
   any of that: the standings themselves are the evidence there. Measured on the harvest's
   final boards, that interpolation is off by 1–3 % at the median between neighbouring
   rungs down to the top 250 (100→120→250: 0.8 %; 50→100→250: 2.3 %) and 5–7 % deeper
   (250→500→1000: 4.6 %, 100→250→1000: 7.3 %), with heavier tails where the field runs out.
   The predictor gives such a rung the pace's own uncertainty plus 4 % per unit of log-gap
   between the two readings, and marks it on the ladder. Before this, an unread rank between
   two read ones took the cold ladder times the carried ratio, and in a mobile qualifier
   with the top 100 read at 157 and the top 500 at 120 that priced the top 160 below the
   top 500 (the ladder's monotone walk then pinned it to 120).

   What is still unvalidated is the precision blend itself: the weights are principled, not
   measured on held-out cups. On the one real test so far — a sealed final read at rank 5 at
   half time — the blend landed 3.9 % from the final threshold where history alone was 14.0 %
   out and the readings alone 6.3 % out. That is one tournament.

---

## 1. The problem

Fortnite tournaments pay out by rank: the top 500 qualify, the top 1,000 get the
cosmetic. The point total a rank required is published only after the evening is
over; a player wants it beforehand, to know whether to keep queuing.

Reading off last week's number does not work. The level moves between editions.
A rank means nothing without a field size — 50th of 500 and 50th of 20,000 are
different achievements, and the field is not announced in advance. And the rank a
player cares about is often one no edition ever published: 84 of the 290
thresholds below. The target is a function, not a number.

## 2. Data

`analysis/data.py` is the only module that touches SQL. The database holds
**66 tournaments and 290 final thresholds** from 2026-06-08 to 2026-09-01, over 13
categories, 7 regions and three game/team modes. Field sizes run 342 to 85,000,
median 1,714.

| source | tournaments | thresholds | what it is |
|---|---:|---:|---|
| `import` | 30 | 240 | full Epic leaderboards pulled through the Cito API |
| `history` | 34 | 36 | typed in by hand, mostly one rank each |
| `cito` | 1 | 10 | one tournament tracked live, read every 10 minutes |
| `manual` | 1 | 4 | entered by hand |

Four things are wrong with it, and they cap everything after.

**It is 33 tournaments wearing 66 hats.** The imported leaderboards carry the whole
shape; 33 of the 66 tournaments contribute one threshold, which fixes a level and
says nothing about the curve. Only **225 rows over 33 tournaments** pass the filter
a shape fit needs: a known field size, two ranks, rank 1 dropped.

**It is shallow.** `q = rank / field` spans 0.0007 to 0.714, but **214 of the 225
rows sit below q = 0.25**. The exponent governing the curve deep in the standings
is fitted on 11 observations.

**Three rows are probably not thresholds.** Three of the six largest residuals in
`analysis/diagnostics.py` are the rank-50 threshold of a lower FNCS division, each
24–31 % below the curve after a 30–40 % one-step drop from rank 25 where comparable
tournaments lose about 5 % — what a truncated standings page looks like read as a
threshold.

**The live models have no data.** `validate.tracked_only` counts **2 tournaments
genuinely tracked** and **63 whose single stored "reading" is a copy of their own
final result**: extrapolating from a reading that contains the answer scores
nothing, so nothing here covers the in-session models.

## 3. The model

    threshold(rank) = level * exp(-a * (q**b - q_ref**b)),   q = rank / field

`level` is the threshold at `REFERENCE_RANK = 20`; the shipped constants are
`a = 1.255`, `b = 0.370` (`calibration.py`).

Three requirements pick the shape. The threshold falls monotonically in rank; the
fall is steep among the first teams and flattens deep in the standings, which rules
out a power law in rank and anything log-linear; and it must be scale-free in field
size, since the same category runs at 1,000 teams in one region and 20,000 in
another. A stretched exponential in the quantile is the smallest family doing all
three: `a` sets the depth of the drop, `b` how fast it flattens.

Working in the quantile also buys the result that makes the tool usable:

    d ln(threshold) / d ln(field) = a * b * (q**b - q_ref**b)

which is **about 0.05** for a typical row — 0.0500 at rank 300 of 36,000, median
0.048 across the 290 validation rows (`calibration.field_sensitivity`). Doubling
the field moves the threshold **3.5 %**; being wrong by half costs 3.4 %. The field
size need only be guessed, which is the position the app is in before a tournament
opens, and why anything works before the first reading.

**Why rank 20 anchors the level.** Rank 1 is one team's good night. The comment
block in `calibration.py` records level dispersion across editions of 7.5 % at
rank 1, 5.6 % at rank 5, 4.2 % at rank 20, and cross-validated error of 9.1 %
anchoring on rank 1 against 6.4–6.6 % at rank 20. **These are the only numbers here
with no script behind them**: a recheck on the current database reproduces the
ordering, not the levels, on three categories. Ordering established, percentages
folklore.

**The cold start.** With no comparable edition the level comes from the scoring
table alone, as a share of the theoretical per-game maximum, shrunk toward
`REFERENCE_SHARE = 0.58` with weight `n / (n + 2)`. That 0.58 rests on three
readings from one format, as `calibration.py` says. Section 5 prices it.

## 4. Estimation

`analysis/fit.py` fits in log space, where the model is linear in `a` once `b` is
fixed. `b` is profiled on a grid then polished by Levenberg–Marquardt, `a` solved
exactly at each step — the profile is not convex in `b`, and a plausible start
lands in a local minimum on this data. Uncertainty comes from a bootstrap that
**resamples tournaments, not rows**.

| estimator | a | b | residual sd (log) | n |
|---|---:|---:|---:|---:|
| within — free level per tournament | 1.346 (se 0.038) | 0.460 (se 0.028) | 0.063 | 225 |
| ratio — as implemented in `calibration.fit_curve` | 1.241 (se 0.038) | 0.376 (se 0.030) | 0.075 | 190 |
| bootstrap 95 % CI (2,000 draws, 33 tournaments) | [1.225, 1.507] | [0.350, 0.574] | | |
| shipped constants | 1.255 | 0.370 | | |

The within estimator matches what the model claims: a shape shared across
tournaments, a level that varies. Both shipped constants sit inside its bootstrap
interval, `b` near the lower edge.

The estimator the app runs is not that one. `calibration.fit_curve` divides every
threshold by its tournament's *observed* rank-20 threshold instead of treating the
level as free. That denominator is a noisy measurement, and dividing by it
attenuates both parameters: **`a` 7.8 % low, `b` 18.2 % low** — classical
regression dilution. It reproduces the shipped constants to within 1.1 % and 1.6 %,
which is where `1.255` and `0.370` came from. Nothing is broken; they are
attenuated estimates, not the fit.

![Fitted curve against observed thresholds](../analysis/figures/curve.png)
*Figure regenerated on the September 2026 harvest (`python -m analysis.figures`); the text of this section describes the original 66-tournament study.*


## 5. Validation

`analysis/validate.py` runs **leave-one-tournament-out over the whole pipeline** —
holding out a single rank would measure interpolation, which the app is never
asked to do. The target leaves the comparable circle, the wide curve-fitting
sample and the level measurements; the prediction tested is the cold one, made
before any reading.

**290 of 290 thresholds predicted over 66 tournaments: median APE 6.4 %, mean
11.7 %, band coverage 86 % against a claimed 80 %.**

| rank band | n | median APE | mean APE | coverage |
|---|---:|---:|---:|---:|
| 1–5 | 94 | 8.7 % | 12.2 % | 85 % |
| 6–25 | 94 | 5.7 % | 12.6 % | 82 % |
| 26–100 | 47 | 9.5 % | 12.7 % | 81 % |
| 101–500 | 38 | 6.0 % | 10.9 % | 97 % |
| > 500 | 17 | 2.2 % | 3.3 % | 100 % |

The mean is roughly double the median in every band: the distribution has a tail,
so quote the median.

**The cold start is expensive.** By the anchor the cascade landed on: a comparable
edition of the same category gives 5.9 % median error over 243 rows; the scoring
table and `REFERENCE_SHARE = 0.58` give **34.6 % median, 42.0 % mean, on 15
observations from 3 tournaments** — the price of the constant, on too few
tournaments to call it a measurement either.

**Against the baselines**, on the 206 rows where all three answer:

| method | median APE | mean APE | gap to model (95 % CI, bootstrapped over tournaments) |
|---|---:|---:|---|
| model | 5.68 % | 9.23 % | — |
| carry last edition forward | 6.03 % | 10.87 % | +0.36 pt [−3.24, +4.07] |
| category median | 7.56 % | 11.99 % | +1.88 pt [−1.74, +4.64] |

**Against a carry-forward this is not a win.** The gap is a third of a point inside
an interval twenty times as wide, and the model wins 58 % of rows; the gap to the
category median is larger and still straddles zero. Fifty tournaments cannot
separate these methods.

It earns its keep where the baselines are silent: beyond rank 500 it runs at 2.0 %
against 9.5 %, because the curve extrapolates to ranks no edition published — and
on 84 of the 290 rows no other edition of the category reaches that rank at all,
so the baselines answer nothing.

**The bands are slightly conservative.** The standardised error has sd 0.83:

| nominal coverage | 51 % | 80 % | 89 % | 94 % |
|---|---:|---:|---:|---:|
| empirical | 76 % | 85 % | 88 % | 93 % |

Too wide in the middle, about right in the tails — errors fatter-tailed than the
normal the band assumes.

![Band calibration](../analysis/figures/coverage.png)
*Figure regenerated on the September 2026 harvest (`python -m analysis.figures`); the text of this section describes the original 66-tournament study.*


**More history helps, once.** On a balanced panel of 48 tournaments that all reach
six peers, median error runs 8.40 % with no comparable edition, 6.14 % with one and
5.73 % with six. **The first comparable edition is worth 2.3 points; the next five
are worth 0.4 between them.** Coverage falls from 100 % to 82 % across the same
range — the band tightening as the anchor firms up, past four peers further than
the errors justify.

![Learning curve](../analysis/figures/learning_curve.png)
*Figure regenerated on the September 2026 harvest (`python -m analysis.figures`); the text of this section describes the original 66-tournament study.*


## 6. What the numbers are worth

From `analysis/diagnostics.py`.

**290 thresholds are 66 evenings.** The forecast residual's intra-class
correlation is **0.801**: 80 % of the variance is shared by every rank in a
tournament, which share a field, a lobby and a guessed level. Design effect 3.72,
so **any standard error computed on 290 rows is understated by ×1.93** — read every
n in section 5 as roughly a third of what it says. The *shape* residual, level read
off the tournament's own standings, has ICC 0.044: once the level is known the
shape travels. The difficulty is the level.

**`b` is not identified, and this is the most important line here.** The exponent
governs the curve deep in the standings, where 11 of 225 observations live.
Restricted samples move it far outside any confidence interval:

| sample | a | b | n |
|---|---:|---:|---:|
| headline | 1.346 | 0.460 | 225 |
| q ≤ 0.25 only | 1.159 | 0.285 | 214 |
| q ≤ 0.10 only | 1.145 | 0.242 | 198 |
| rank 1 kept | 1.338 | 0.376 | 257 |
| practice events dropped | 1.274 | 0.408 | 219 |

`b = 0.370` is a working value, not a measurement. The interval [0.350, 0.574]
describes sampling noise around one modelling choice; it does not cover the range
defensible samples produce.

**The variance is not constant, and it tilts the exponent.** Breusch–Pagan
LM = 17.8 on 3 df, p = 0.0005:

| rank band | 1–5 | 6–25 | 26–100 | 101–500 |
|---|---:|---:|---:|---:|
| residual sd, log points | 0.035 | 0.035 | 0.094 | 0.114 |

Unweighted least squares in log space therefore gives the deep, noisy ranks more
say in `b` than their precision earns — the same rows `b` already depends on.

**The functional form is fine.** The residual has no tilt against `q` (p = 0.98),
field size (p = 0.52), fitted value (p = 0.94) or date (p = 0.12). Nothing
systematic is left: the problem is the tails and the identification, not the
shape.

**Three rows carry the headline.** The shape residual has skew −3.06 and excess
kurtosis +16.2, entirely from the left tail of section 2. Drop the five residuals
beyond ±20 % and the sd falls from 0.064 to 0.039, skew +0.35. **The honest working
error of the shape is about 4 %; the 6 % headline is 4 % plus three probable
data-entry artefacts.** They stay in because I have not verified them; dropping
unverified inconvenient rows is how a backtest starts lying.

![Residual panel](../analysis/figures/residuals.png)
*Figure regenerated on the September 2026 harvest (`python -m analysis.figures`); the text of this section describes the original 66-tournament study.*


## 7. What would move the needle, in order

1. **Collect thresholds at q > 0.25.** The binding constraint on `b`, and a
   collection choice rather than a data limit: the Cito endpoint returns full
   standings — 1,271 rows on one Division 2 event — so the deep ranks are in hand
   and simply not stored. Nothing else here is worth as much.
2. **Verify the three rank-50 rows**, and have the importer flag a one-step drop
   far outside neighbouring steps. They are the entire left tail.
3. **Weight the shape fit** by band variance, or fit a robust loss. Free, and it
   breaks the interaction between the heteroskedasticity and the rows identifying
   `b`.
4. **Price the cold start.** 34.6 % on 3 tournaments is a warning, not a
   measurement; `REFERENCE_SHARE` should be per game mode, with an interval.
5. **Track evenings live.** Two tournaments cannot test four in-session models;
   until there are twenty, half the app is unevaluated.
6. **More tournaments.** Separating the model from a carry-forward at a gap of
   0.36 points needs far more than 50.
7. **Re-derive the rank-20 anchor choice in `analysis/`**, so the last script-less
   numbers here acquire a script.

## Reproducing this

```bash
pip install -r analysis/requirements.txt

python -m analysis.data          # what is in the database
python -m analysis.fit           # a and b, bootstrapped over tournaments
python -m analysis.diagnostics   # residuals, clustering, heteroskedasticity
python -m analysis.validate      # leave-one-tournament-out vs two baselines
python -m analysis.figures       # the four figures above, PNG + SVG
```

The database is opened read-only; bootstraps are seeded, so reruns on an unchanged
database reproduce exactly. `analysis.validate` and `analysis.figures` take about
half a minute each.

**Every number here moves as the database grows.** These were printed on 66
tournaments and 290 thresholds; at that size a few new evenings shift the medians
and can move `b` further than its interval suggests. Where this note and the
scripts disagree, the scripts are right.
