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

   A run of the feed is not one snapshot, and treating it as one was worth more than any of
   the above. Every page of a board is a separate request; the copies the API returns are
   not the same age, and `MAX_PAGES` means a rank can be missing from a run altogether. So
   the reading of a rank could be minutes stale while the first page was fresh, and the rank
   asked about could drop in and out of the runs. Both moved the answer: a stale page priced
   its rank at the wrong minute, and a missing one sent the forecast back to the ratio
   carried over from other ranks, which at the deep end of an open queue runs 10–15 % high.
   The feed now stamps a reading with its own page's time when that differs from the first
   page's, and the predictor keeps an evening's *standing* — the reading of each rank that
   stands now, whichever run it arrived in, the richer of two of the feed's own (a threshold
   never falls, so the poorer is the older copy), a rank unread for 25 minutes let go — and
   prices each reading at the minute it was taken. Replaying six evenings from the feed's
   own history, at the rank each cup qualifies on, through the page's own code:

   | evening (rank) | median move between readings | largest move | median error after 30 % |
   |---|---|---|---|
   | Mobile LCQ BR (160) | 6.6 % → 2.2 % | 33.9 % → 13.2 % | 3.3 % → 2.8 % |
   | Solo Victory Cup NAW (800) | 3.2 % → 2.3 % | 16.2 % → 8.0 % | 1.6 % → 1.4 % |
   | Mobile LCQ EU (160) | 3.0 % → 1.8 % | 21.5 % → 7.1 % | 3.0 % → 3.0 % |
   | Mobile LCQ ME (160) | 2.3 % → 1.2 % | 9.7 % → 9.7 % | 1.6 % → 1.6 % |
   | Reload Duos OCE (500) | 1.8 % → 1.1 % | 11.5 % → 8.8 % | 6.4 % → 6.1 % |
   | FNCS Div 1 final NAW (20) | 15.2 % → 2.9 % | 28.8 % → 15.2 % | 16.7 % → 19.5 % |

   All as a share of the final threshold. The sealed final is the same story from the other
   side: a run handed a copy of the board from before the last game un-played a game, and
   the answer fell with it. Its error does not improve — after two of six games the model
   read that lobby 19 % high whatever the timing — which is the honest reading: this fixed
   the feed's clock, not the model's aim.

   The window's close is not the end of the cup, and the curve stopped there. `analysis/live.py`
   measured the share at tenths of the session, τ = 0.1 … 1.0; at τ = 1 an open queue's threshold
   sits at 0.93 of its final value, because the games under way when the clock stops keep landing
   for another quarter of an hour. The page then priced *every* reading taken after the close as a
   reading at the close and added that 7 % again to a board that had already collected it, so the
   answer climbed with the standings instead of converging on them: on the Reload ZB Duos EU cup of
   6 September the top 20 read 559 at the close (forecast 600) and 607 eighteen minutes later
   (forecast 651), against a board that ended at 607. The same replay now continues past the close,
   in minutes rather than in tenths — the games still in flight last a game, whatever the window
   was — and gives a second, short curve (23 open boards on the machine this was written on; the
   harvest re-measures it at every run of the module):

   | minutes past the close | 0 | +5 | +10 | +15 | +20 |
   |---|---|---|---|---|---|
   | share of the final board | 0.93 | 0.95 | 0.97 | 1.00 | 1.00 |
   | spread | ±0.05 | ±0.05 | ±0.03 | ±0.02 | ±0.01 |

   A reading taken after the close is priced on that curve and carries its spread, which is why the
   band closes as the board settles rather than staying at the close's ±5 %. Two smaller rules go
   with it, both found by the same replay: between two readings of a rank with the same points the
   later one stands (it says the board has stopped moving, which the earlier one could not), and a
   standing the feed reads again is clocked at the minute it was last read rather than the minute
   it first stood. On the five open-queue evenings of the feed's history, the last number the page
   shows — the one a player reads when the cup is over — was 7.0 to 7.5 % above the final board
   before, and is 0.0 to 1.3 % from it now.

   What is still unvalidated is the precision blend itself: the weights are principled, not
   measured on held-out cups. On the one real test so far — a sealed final read at rank 5 at
   half time — the blend landed 3.9 % from the final threshold where history alone was 14.0 %
   out and the readings alone 6.3 % out. That is one tournament.

## Audit, 10 September 2026

A pass over the whole cascade against the held-out evenings, looking for where the error
came from rather than how much of it there was. Six things were found. Every figure in this
section was measured on the database as it stood on 8 September — 7,329 tournaments after
the Ranked Cups are set aside, the newest 600 held out — and `analysis/validate.py` prints
the current ones; the export carries them into the page.

**The validation was measuring the split, not the model.** The forecasting split froze the
pool at the first held-out day: all 600 targets, six weeks of them, were forecast from what
came before 25 July. A weekly cup's fifth week was priced off the season before, and every
edition of a cup born inside the window — thirty-three Arena test cups, twenty-one Solo
Reload Victory Cups — counted as a cold start. The app never works that way: on the night,
last week's edition is in the database. The validation is now a rolling origin — each target
is forecast from the pool *and* from every held-out tournament that started on an earlier
day, the curve fitted once on the pool so no target shapes it — and the baselines are held to
the same rule. On the same 600 evenings, with the same code, the change of split alone
takes the median error from 12.9 % to 6.8 % and the cold share from 58 % to 34 %: those were
numbers about the split. The day is the grain — an evening's other regions never inform it —
so the split still errs on the pessimistic side. `--frozen` reproduces the old one.

**A cup was the same cup in any format.** The narrow circle the app calibrates on has always
keyed on team size and game mode; the wide tables the cascade reads — the previous edition,
the level, the shape, the field — keyed on the name and the region alone. So the first Duo
week of FNCS Division 1 Practice read the last Trio edition, a year older and a third the
field, as its previous edition, straight; the fifty-team Major finals read the thirty-three-
team finals of the season before (30 % error, 58 % inside the band); and the Performance
Evaluation, which alternates Battle Royale and Reload weeks, read whichever ran last. The
tables are now keyed on the format too. A cup that has never run in its format reads its last
edition in another one with the band widened by half, like a different entry bar — except a
single lobby, which the closed-lobby rung prices at 5 % where the other lobby size priced it
at 30 %. 185 of the 1,859 cup-and-region groups in the database have run in more than one
format; the Performance Evaluation in EU has switched seven times.

**The last places of a closed lobby were forecast.** Rank 50 of 50 was priced at thirty
points where the board said eight, twenty-one times out of twenty-one: the last places are
teams that left after a game or two, and no level, shape or share of the lobby says how many
left. The cascade now refuses them (past 90 % of the lobby) unless the previous edition's own
reading is there, which is the only honest number.

**A later round was priced as its open round.** The cold start keyed on the kind of cup, the
platform and whether it was a final; "· Round 2" and up fell in with the open round, and a
few hundred qualified teams playing a shorter session score another share of the maximum
than ten thousand did. Split, the cold-start error on those rows went from 24.8 % to 20.3 %
and its coverage from 77 % to 83 %.

**Sixteen-player heats were setting the mobile cold start.** A heat has no rank 20, so its
winner's score was read back through the curve to a "level" a tenth of it, and the share of
the maximum it contributed was 0.09 where every open mobile cup sits near 0.5. The two
populations were nearly equal in number; the week the heats tipped the median, the cold start
for an open mobile cup dropped to a tenth of the maximum overnight (Shadow Cup Mobile, 21
August: 54 points forecast at rank 20 against 343). Single lobbies have their own table and
are now left out of that one.

**One curve did not fit every field.** The same `q = rank / field` is a different place in a
division of three hundred qualified teams, where everyone plays every game, and in an open
queue of ten thousand, where half the field plays one. Fitted on all boards the exponent `b`
lands on 0.88; fitted by band of field size it wants 1.24 below 300 teams, 1.25 to 1,000,
1.12 to 3,000, 0.55 to the API's ceiling and 0.15 above it — the two ends sat on the edges
of the old search grid, which stopped at 0.2 and 1.2. Where no edition of the cup existed,
the pooled curve ran a fifth low at rank 1,000 and a third low at rank 2,500 of the big
queues, and a quarter low at ranks 250–500 of the smaller fields. The curve is now
fitted once per band (five bands, each needing 200 boards; the pooled fit answers otherwise)
and the cascade reads the band the cup falls in, for the shape, the field move and the level
read back from an edition that published no rank 20.

Together, on the audit's own replay of the same 600 evenings under the rolling split (the
replay also kept the Ranked Cups, so its figures sit a little above the validation's):

| | code of 7 September | code of 10 September |
|---|---:|---:|
| median error, all rows | 6.8 % | **5.8 %** |
| mean error | 17.8 % | 13.3 % |
| inside the 80 % band | 85 % | 90 % |
| cup has run before (previous edition) | 3.0 % | 2.9 % |
| cup has never run (scoring table) | 22.2 % | 14.4 % |
| single-lobby final never run | 11.4 % | 7.4 % |
| ranks past 500 | 16.8 % · 66 % covered | 14.2 % · 74 % covered |

The steps, measured one at a time on that replay: the format keys and the fallback
6.80 → 6.63, the later-round stage → 6.37, the banded curves → 6.05, the heats out of the
share table → 5.78. `analysis/validate.py` itself, on the same database, reads 5.6 % median
and 12.9 % mean error with 91 % inside the band, the previous-edition rung at 2.8 %, the
cold start at 14.4 % on 35 % of the rows, the closed-lobby rung at 7.4 %, and the model
0.27 points ahead of the previous edition read straight (interval +0.11 to +0.47, whole span
above zero) where the frozen split with the old code read 11.1 %. A page showing 50 % and
90 % ranges takes them from the quantiles this validation writes, so they follow the split:
the 50 % range narrowed from −0.31 … +0.46 to −0.19 … +0.27 half-widths in log terms.

What the audit did not change, and why: the share of the maximum a cold start rests on is
a median over the whole history, and a median over the last thirty editions tracked recent
metas better on the console and Reload cups (13.6 → 6.3 %, 21.7 → 11.3 %) but blew up the
mobile ones when a bad month landed in the window (33 → 78 %). Left pooled. The Arena test
cups are a format the model cannot price from the scoring table — the number of games is
not fixed, and one edition's winner scored 1,200 where ten games of 60 allow 600 — and stay
cold until their second week, when the previous edition takes over.

**The live model, replayed.** The feed's own history — 39 evenings by 8 September — was
replayed through the page's code with a model exported as of the morning of 5 September, so
nothing read its own result. In open queues the error at the qualifying rank runs from 5.0 %
at a third of the session to 2.2 % at the close, with a bias of +2.9 % early and −1.1 % late;
the 50 % range holds 54–63 % of the finals and the 90 % range 91–94 %. Sealed lobbies were
worse — 7.5 to 11.6 %, biased 4 to 10 % low, the ranges holding 18–44 % and 42–82 % — for two
reasons the replay separated: the feed's old rule declared a board final after five games of
six, and the pooled closed-lobby curve read "game 2 of 6" at 0.363 of the final where a
six-game lobby sits at 0.33. The curve is now measured per game count (`pace.games_curve`,
519 six-game Battle Royale finals, 136 eight-game Reload, 84 six-game Zero Build) and the
page reads the one that matches; a result is declared only when the board is sealed and owes
no game, or twenty minutes past the close, and has been read twice ten minutes apart
unchanged. The feed keeps reading for 45 minutes past the close rather than 25: three-hour
practice cups were still 3–7 % short of their final board at +25.

## The end of a cup, 10 September 2026

A forecast that sat under the live board. At the close of the Performance Evaluation EU of
10 September the page said 261 points for the top 50 — the rank that cup qualifies on — while
the standings on osirion.gg already showed 270. Replaying that evening's feed history through
the page reproduces it exactly: the reading of 18:18, eighteen minutes past the buzzer, was
261, and the page projected 261 with a 90 % range of 258–265. The board went on to 272 by
18:23 and was still rising when it got there.

**The board the page holds is not the board the model was measured on.** `by_tail` measures
the settling from each roster's own session end times: by twenty minutes past the buzzer every
game has ended, so it reports the board final — 1.000 of the final threshold — with a spread
of ±0.008. The page reads none of that. It reads Osirion's published board, through the feed,
and Osirion lands minutes behind the games. Measured on the feed's own history — 1,078
readings past the close over 34 tracked open queues, against the finals harvested afterwards —
the published board can be short of the final long after the games are in, and the one signal
that separates the two states is whether the standing has been read again unchanged:

| the standing the page holds | readings | median | p90 | p95 | worst |
|---|---:|---:|---:|---:|---:|
| younger than ten minutes | 924 | 0.998 | 1.035 | 1.046 | 1.098 |
| unchanged for ten minutes or more | 154 | 0.993 | 1.000 | 1.000 | 1.000 |

(the ratio of the final threshold to what the page projected). A board that has held still for
ten minutes is final, exactly, in every one of the 154 readings; a fresher one is 3 % short
once in ten and 5 % short once in twenty. The page was quoting ±1 % for both. So the width
past the close is now measured on the feed and split on that signal (`pace.tail_feed`, written
by `analysis/live.py`): ±3.8 % while the standing is still moving, ±1.6 % once it has held.
Only the width changes — the curve keeps the replay's median, which the feed agrees with to
within a few thousandths and which rests on 2,895 boards rather than 34.

**The centre was already right, and every attempt to raise it made it worse.** The obvious
reading of "the forecast is behind the board" is that the projection should anticipate the
rise still to come, so the board's own rate of climb was tried as the projection, alone and
as a floor, over one game length and over the tail; and the settling curve was tried capped
and time-shifted while the board was seen to move. On the 1,127 post-close readings the
shipped projection has a median error of 1.40 % and a bias of +0.28 %; the best of those
variants read 1.83 % and the rest 2.0–5.4 %, all of them adding bias (+0.7 to +8.5 %) and
turning 71 readings more than 5 % high into 244 to 436. Splitting by whether the board was
rising does not rescue them: those rows are not systematically under-forecast either
(bias +0.17 %). The evening that started this was a genuine tail case — the board gained 18 %
after the buzzer where the median cup gains 7.6 % — and the honest answer to a tail case is a
range that contains it, not a centre moved to meet it.

**A threshold never falls.** Whatever the weighing of readings against history says, the final
threshold at a rank cannot be below what the board already shows there: a team's points only
rise, and the k-th score of a rising set only rises. That floor now holds the answer and the
low end of both ranges, and where the rank itself was not read, the nearest deeper rank that
was serves as the bound. Replaying the 39 evenings, the blend landed under the board it was
built on in 82 of 10,182 rows, across 23 of the 39 evenings — small, and impossible, which is
the kind of error worth removing by construction rather than by tuning.

On those 39 evenings the two changes leave the post-close centre where it was (median error
1.20 % against 1.21 %, bias −0.08 % against −0.05 %) and widen the coverage where it was
wrong: the 50 % range holds 42 % of the finals against 40 %, the 90 % range 80 % against
78 %. At the 18:18 reading of the evening in question the page now says 263 points, 50 %
261–268, 90 % 261–274, and says in words that the last games are still landing and the board
can only rise from here. Two caveats it also earns: the width is measured on the same 34
evenings it is used on — split in half by evening it reads ±4.1 % and ±3.5 %, so it is at
least stable, and `analysis/live.py` re-measures it at every run as the feed accumulates — and
the live ranges still under-cover overall (the 50 % range holds 27–40 % and the 90 % range
78–85 % across the session), because the multipliers that turn a half-width into those ranges
were measured on cold forecasts and have never been measured on live ones. That is the next
thing to fix, and it needs the replay harness to become a module rather than a scratch file.

**Does the top of the board settle later than its bottom?** A fair question about all of the
above, and the honest answer is that the shipped tail cannot tell: `curve` pools it over
ranks 1 to 25 — the rungs whole on every board — and the page reads that one number at every
rank, so the qualification cut and the top 2,500 are told the games land at the same pace.
Two stories pull opposite ways. The last game is played harder at the cut, where
qualification is decided, than at the bottom of the board, where a team with nothing left to
play for logs off — so the top should gain more. But it is at the bottom that teams are still
*starting* games at the buzzer, the casual half of an open queue, so the deep end should have
more still to land.

Measured on the feed's own 26 evenings, at the last reading at or before the buzzer against
the final:

| rank | 1 | 3 | 5 | 10 | 20 | 25 | 50 | 100 | 250 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| median gain after the close | +9.4 % | +8.2 % | +8.8 % | +9.3 % | +8.5 % | +8.6 % | +8.9 % | +7.6 % | +7.6 % |

Paired inside each cup, which is the test with the power — the same evening's gain at a
shallow rank against a deep one — the top 20 gains 1.6 points more than the top 250 (median
over 14 cups, 9 of them in that direction) and the top 50 gains 0.6 points more (10 of 14).
So the gradient has the sign the pressure story predicts, a size of one point in ten, and no
support at fourteen cups; and the ranks the question is really about — 500, 1,000, 2,500 —
cannot be seen at all here, because the feed only reads that deep when it has pages to spare.

So `analysis/live.py` measures the tail per band of rank (1–25, 26–100, 101–500, 500+) on
the harvest's own replay, where every rank the harvest keeps can be reached and the boards
number in the thousands. Run over the whole harvest three pages deep — 25,642 readings —
it answers the question:

| band | +0 | +5 | +10 | +15 | +20 | readings |
|---|---:|---:|---:|---:|---:|---:|
| top 1–25 | 0.924 | 0.946 | 0.968 | 0.993 | 1.000 | 17,736 |
| top 26–100 | 0.921 | 0.939 | 0.960 | 0.982 | 1.000 | 5,333 |
| top 101–500 | 0.928 | 0.951 | 0.971 | 0.989 | 1.000 | 2,573 |
| ± top 1–25 | 0.069 | 0.058 | 0.045 | 0.027 | 0.007 | |
| ± top 26–100 | 0.048 | 0.049 | 0.036 | 0.021 | 0.007 | |
| ± top 101–500 | 0.098 | 0.090 | 0.076 | 0.055 | 0.006 | |

**The medians say no and the spreads say yes.** The board settles at the same pace at every
rank: the remaining gain at the close is 8.2 % at the top 25, 8.6 % at the top 100 and 7.8 %
at the top 500, a spread of one point across bands where the spread *within* a band is seven
to ten, and not even monotone in rank. Neither the pressure story nor the late-queue story
survives that. What does survive is the second moment: the deep end is half again to twice
as unpredictable as the top at the same minute (±9.8 % against ±6.9 % at the close, ±5.5 %
against ±2.7 % at fifteen minutes). How much the board has left to give is the same
everywhere; how *sure* that is falls away with depth.

That is worth shipping on its own, and it is where the earlier band was wrong for a deep
rank. Both widths are now carried and the page takes the wider of the two, because each is
blind to half the shortfall: the feed's is the total — its board is short because games are
still landing and because Osirion has not published them — but pooled over ranks on the
evenings tracked, while the games' clock is per rank on thousands of boards. Adding them
would count the games twice. Replaying the 39 tracked evenings past the close, the 90 %
range holds 89 % of the finals against 83 % pooled, and holds evenly across ranks —
89 %, 89 % and 92 % by band, where the pooled width held 83 %, 87 % and 76 % and the deep
band was the one that missed. The centre does not move (median error 1.19 % against 1.20 %,
bias −0.05 % against +0.02 %).

The 500+ band is still empty: three pages read three hundred rosters, so the deepest rung
measured is the top 250. `--pages 10` reaches the top 1,000, `--pages 25` the top 2,500 —
all local reading, no network — and the bands fill in as they are reached.

**The lag itself.** None of the above shortens the ten minutes between one reading and the
next, and at the end of a cup the board moves about a point a minute at the ranks a cup
qualifies on: a reading ten minutes old is several points behind what a player sees. The feed
now runs every five minutes instead of ten, and the extra pass reads only the first page —
the top hundred, where an open queue's cuts sit — and only of the windows inside the last
twenty minutes of their session or still settling. That is one request per window rather than
four, so the worst-case staleness falls from about fifteen minutes to about seven for a few
hundred extra API calls a day rather than double. The page polls every two minutes over the
same window instead of every five.

---

## The pace of a kind of cup, 13 September 2026

A forecast that was five points short an hour into a cup. At 09:59 on 13 September, fifty-nine
minutes into the Solo Victory Cup Battle Royale OCE, the page said 157 points for the top 800 —
the rank that cup qualifies on — with a 50 % range of 154–161 and a 90 % range of 144–173, while
the board showed 78. The cup ended at 163. Replaying the feed's history of that window through
the page reproduces the reading exactly (157.7), and the question is whether an hour of readings
could have said more.

**Where the five points went.** The page extrapolates a reading by the share of its final a
threshold has typically reached at that minute: at 0.49 of the window the pooled curve says
0.49, so 78 points became 157. This cup's top 800 was at 0.478 — the extrapolation was 2 % low
on the pace — and then landed at 0.896 of its final on the hour where the curve says 0.924,
another 3 %. Its top 50 did the opposite: 0.513 at the same minute, five points *ahead* of the
curve, and the forecast there ran 5 % high. Inside one cup the top of the board and the
qualification cut went opposite ways, which is what the 90 % range is for, and 163 sat inside
it. But the second shortfall — a two-hour Battle Royale cup landing at 0.90 on the hour, not
0.92 — is not noise, and it is the thread the rest of this pulls.

**The material.** The database now holds 145 evenings the feed followed with a final to
score them against, 110 of them open queues, 13,959 readings over nine days; and 512 boards of
the harvest since August replayed game by game from each roster's own session times, 369 of
them first-round open queues of at least fifty rosters. Everything below is measured on those
two, and every change is scored on the feed's evenings by a rolling backtest: the model of the
morning of 5 September, and for each day only the boards replayed and the evenings followed
before that morning. The page code is the page's own, driven by a headless browser.

**Cups of a kind do not run alike.** Pooled over every open queue, the curve is a median of
cups whose pace differs by a tenth. Replayed per family — game mode, team size, window length,
game cap — the share reached at half time runs from 0.42 (Battle Royale Duos, two hours, eight
games) to 0.53 (Zero Build Duos, three hours, eleven), and the share on the hour from 0.865 to
0.938: a two-hour Battle Royale cup has a thirty-minute game still in the air at the buzzer, a
Reload cup capped at ten short games has nothing left to play in its last half hour. Asked the
page's own question — a reading at share *s* extrapolated by the curve, against the final,
each cup scored with a curve measured on the others — the family's curve is wrong by a third
less than the pooled one at the top 25 (median error 2.98 % against 4.56 % at half time, 1.82 %
against 3.37 % at seven tenths; ninetieth centile 7.6 % against 13.3 %) and carries no bias
where the pooled curve carried −2 to −3 %. A cup's own recent editions are the sharper unit
still, because a kind of cup drifts by the width of a family inside a month: the Console Zero
Build Solo Victory Cup ran at 0.45 at half time in early August and 0.49 in September. So the
model now carries the curve and the tail of each family that has thirty boards, and of each cup
that has four editions, both from the most recent, and the page reads the cup's own row first,
its family's next, the pooled curve last. Each is read from three tenths of the session on and
ramps in over the tenth before: earlier the board holds a game or two, the curves are a few
hundredths apart and the spread of a reading already says more — read from the start, they
made the first third of the session worse.

**What the harvest cannot see.** Compared cup for cup on the same evenings, the feed's board and
the harvest's replay of it agree at the top 25 to within a point at every tenth — and disagree
by 4 to 9 % at half time in one kind of cup. The harvest replays a board from the rosters that
finish in its first three pages, and mid-session that is not the board there was: in a casual
Solo cup the players at the top at half time who then stop playing finish outside those pages
and are not on disk, so the replayed 25th place at half time runs low. In a divisional practice
where every team plays every game there is no such turnover and no such gap. So a category's
curve is also measured on the feed's own evenings, from the top 25 the feed read, and the page
prefers that row once four evenings of the cup have been followed; the harvest answers for the
cups the feed has not followed yet, and for the families. On the evenings of 8 to 13 September
this is the change that mattered most: the same code with the harvest's category rows alone
took the median error at half time from 7.2 % to 6.7 %, with the feed's rows to 6.4 %.

**The deep end runs on its own clock, and it is the depth into the field that sets it.** The
curve is measured on the top 25, the rungs whole on every harvested board, and the harvest
cannot measure a deep rank mid-session at all — with three pages read, the 250th best at half
time is the 250th of three hundred rosters, a third low. The feed can, and on its evenings the
ratio of a band's share to the top 25's at the same reading is not a property of the rank:
the thousandth of a field of two thousand is the casual half, players who play their few games
early and stop, and its threshold is a fifth further along at two thirds of the session than
the top 25 and all but done by the last fifth; the thousandth of a field of ten thousand is the
top tenth and keeps the top's pace to within a few per cent. Measured per band of
*q* = rank / field on 110 evenings:

| q | 0.3 | 0.4 | 0.5 | 0.6 | 0.7 | 0.8 | 0.9 | 1.0 | evenings |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.02–0.05 | 0.96 | 0.98 | 0.99 | 1.00 | 1.00 | 1.01 | 1.00 | 1.00 | 90 |
| 0.05–0.10 | 0.92 | 0.96 | 0.99 | 1.00 | 1.01 | 1.01 | 1.02 | 1.02 | 76 |
| 0.10–0.20 | 0.90 | 0.98 | 1.01 | 1.02 | 1.04 | 1.04 | 1.04 | 1.04 | 56 |
| 0.20–0.50 | 0.88 | 0.95 | 1.06 | 1.05 | 1.07 | 1.09 | 1.07 | 1.04 | 36 |
| 0.50–1.00 | 0.84 | 0.98 | 1.09 | 1.16 | 1.18 | 1.20 | 1.11 | 1.10 | 23 |

The page multiplies the curve by it from four tenths on, ramping in over the tenth before,
where the ratio crosses one steeply and a reading's own spread says more. Which needs the
field — and a first round's field is not in the calendar. The cold forecast guesses it from the
cup's editions, and on the evenings followed that guess was out by half or more for one cup in two.
The board's own page count is a better number: a hundred rosters a page, so the count of who
has played so far, which the feed already read and now keeps with every reading, exact to the
roster on a full pass since the feed reads the last page too — up to the API's hundredth page, past which the board reads "ten thousand or more", the same ceiling every
harvested field of that size records and the depth ratio was measured against (Osirion's own
site counts players from the games it parses and can say fifteen thousand where the
leaderboard stops at ten). On the
harvest's replays nine rosters in ten have a game in by a third of the window, so from there
the count stands in for the field, and the page shows it under the progress bar. That is the
answer to a question asked this week — whether it is worth knowing how many played at each
reading. It is: the depth of a rank is measured against it, the size of the field is what the
cold forecast is most often wrong about, and the arrival itself — when the field fills — is a
clock the pace curve may be told by, once enough evenings carry the count to measure it.

**How wide a live answer has to be.** The multipliers that turn a half-width into a range were
measured on cold forecasts and applied to live ones, and the live 50 % range held a quarter of
the finals mid-session where it claimed half. They are now measured on the feed's evenings too,
in units of the reading's own half-width, by share of the session and past the close, and apart
for the deep end, where the answer is both wider and, so far, skewed: the extrapolation lands
high more often than low there, which the ranges now say.

**Four ideas measured and left out.** The games' clock: the share of the final against the
games played by the rosters standing around the rank, which would adapt to any format by
itself, is *less* telling than the wall clock (median error 6 to 8 % against 2 to 3 % at the
top 25), because the rosters at a rank at half time are the ones who queued fastest, which is
not the population's pace. The trend of the extrapolation: a cup's deviation from its curve
persists through the session (0.77 between three tenths and half time, 0.59 between half time
and nine tenths), but the slope of the extrapolated forecast over the readings so far explains
a twentieth of what is left (standard deviation 0.038 to 0.036 at six tenths). The same day's
other regions: the earlier region's deviation predicts the later one's with a correlation of
0.36 at half time and 0.06 at nine tenths. The exact field for the cold prior: handing the page
the harvested field instead of the calendar's nothing moved the median error by a tenth of a
point. And two fallbacks tried and withdrawn: a family read by game mode and window length
alone (the Arenas ten-game cups priced the Solo Victory Cup's eight-game one at 173 for a final
of 163), and a family's own spread in place of the pooled one (measured on thirty boards it is
narrow early, the readings outweighed the history before they were worth it, and the 90 %
range held a third of the finals).

**What it comes to.** Rolling backtest on the 110 open-queue evenings, every rank with a
final, the page's blended answer, median absolute error and the share of finals inside the
two ranges (before → after):

| share of the session | median error | 50 % range holds | 90 % range holds |
|---|---:|---:|---:|
| 0.1–0.3 | 3.5 % → 3.6 % | 62 → 61 % | 98 → 98 % |
| 0.3–0.5 | 5.0 % → 4.2 % | 37 → 42 % | 85 → 85 % |
| 0.5–0.7 | 4.7 % → 3.6 % | 22 → 29 % | 73 → 78 % |
| 0.7–0.9 | 3.0 % → 2.5 % | 33 → 39 % | 85 → 87 % |
| 0.9–1.0 | 2.1 % → 1.8 % | 44 → 48 % | 89 → 90 % |
| 0–10 min past the close | 2.1 % → 1.4 % | 34 → 45 % | 82 → 88 % |
| 10–20 min past the close | 1.3 % → 1.0 % | 39 → 47 % | 85 → 89 % |

By rank, the top 26 to 100 — where most cuts sit — gain the most: 5.2 → 3.6 % at three to five
tenths, 4.9 → 3.2 % at five to seven, 2.6 → 1.9 % at seven to nine, 1.5 → 1.0 % in the ten
minutes after the close. The ranges are still short of what they claim mid-session, for a
reason the numbers above show rather than hide: on the last three days the cups the feed had
followed fewer than four times — the Weezy Icon Cups, the Mega Man cups — ran 4 to 10 % ahead
of the families they fell back on, and no width fixes a centre. That gap closes by itself as
the feed follows them, which is what the feed-measured rows are for. Beyond the top 500 the
answer is still 7 to 9 % high mid-session on the evenings so far, on a q measured against a
guessed field; the board's own count should do better from here on, and cannot be replayed yet
because the readings before today did not carry it.

**And the cup that started this.** With every table above measured on the boards and evenings
before that morning, the page would have said 155 for the top 800 at fifty-nine minutes rather
than 157 — this cup's deep end ran behind its editions' — and 158 at the buzzer, 162 ten minutes
on, 164 at a quarter past, where it had said 157, 157 and 159. The five points at the hour were
the tail of a two-hour cup, and that part is measured now. The two at fifty-nine minutes were
this cup's own; on 362 replayed cups nothing the board shows at half time — its trend, its
leaders' game count, its sister regions' evening — tells more than a third of that, and a
forecast that pretended otherwise would be a narrower range that holds fewer finals.

`analysis/live.py` measures all of it and `pace.json` carries it; `pull_live` files the page
count with each reading; the worker keeps it in the day's history.

## The first edition of a season, 13 September 2026

A forecast that was wrong before the cup began. At 17:00 on 13 September, as the Solo Victory
Cup Battle Royale EU opened, the page said 156 points for the top 4,000 — the rank that cup
qualifies on — and the previous edition, on 5 September, had ended above 206 at that rank (the
feed's last reading, seventeen minutes after the close, with the board still rising). Nothing
about the cup was unknown: the week before was in the database. The number came from a
hand-typed row of 25 July, the last edition of season 41, and the reasons it did are three,
each of them structural rather than a bad night.

**The rank the cup is played for was not a rank the harvest recorded.** The harvest reads
fifteen fixed ranks — 1 to 250 on the first pass, three pages deep, 500 and beyond on the later
passes that take days to come round — and the qualification cut of a Solo Victory Cup sits at
4,000, on page forty. The 5 September edition sat in the database with its top 250 and nothing
below, so the model's first rung, which reads "this cup, this rank, last time", read rank 4,000
off the last edition that held it: four rows typed by hand in June and July, 143 to 178 points,
the latest 156. The rung's band said 22 % either way, which is what four editions a season apart
disagree by, and the centre was a season old.

**Epic renamed the cup, and the model's categories are keyed on the name.** "Solo Victory Cup"
became "Solo Victory Cup Battle Royale" at season 42, "Console Solo Victory Cup (ZB)" became
"Console Zero Build Solo Victory Cup", "FNCS Division 2" became "FNCS Division 2 Practice"; the
event id's series key — `SoloVictoryCup`, `ConsoleVCC_SolosZB`, `FNCSDivisionalCup_Division2` —
did not move. Under the new name the cup had one harvested edition and the four typed rows;
under the old one, 142 editions across seven regions that nothing on the calendar would ever
match again. On this database 2,790 rows sat under 23 former names. Every first edition of a
renamed cup in season 42 started cold, at the scoring-table rung and its 14 %, with a season of
history a name away.

**The model had no notion of a season.** Read across a turn of season, the previous edition
was read as if nothing had changed. Measured on every category that ran in two consecutive
seasons (its last edition of the one against its first of the next, same number of games, ranks
worth less than a quarter of rank 20 left out as the bottom of a small board):

| turn of season | pairs | rank 20 | rank 100 | rank 250 | rank 500 | rank 1000 | rank 2500 |
|---|---|---|---|---|---|---|---|
| 39 → 40 | 18 | +4.9 % | +4.6 % | +6.1 % | +8.4 % | +13.2 % | +22.8 % |
| 40 → 41 | 43 | −0.4 % | −0.4 % | −0.5 % | −0.5 % | +0.0 % | −3.2 % |
| 41 → 42 | 41 | +4.4 % | +5.8 % | +6.7 % | +9.3 % | +11.2 % | +11.6 % |

Medians of the move; within a season, consecutive editions of a cup move by 2.5 % at rank 20
and 4.4 % at rank 1000 (median absolute deviation). A turn of season is a shift shared by
every cup — at 41 → 42, 78 % of the categories moved up at rank 20 and 91 % past rank 500, in
every region — and larger at depth, where a bigger crowd lifts the deep ranks more than the
top. Or nothing at all, as at 40 → 41. It cannot be known before the season's first cups have
run; it can be read off them for the cups that come after.

**What changed.** The harvest fetches the page each cut falls on with its first pass — one
request per cut — and records the cut's rank beside the fixed fifteen, so the latest edition
carries the rank it was played for; a window is derived again, in place, when more of its pages
are on disk than the last derivation read, so the deeper passes and the cuts' pages reach the
database at the next build rather than at the next change of naming rules, and a rebuild no
longer deletes the rows — an earlier version did, and took the feed's readings of every cup
with them. Every edition of a series is filed under the name Epic gives the cup now, keyed on
the series id, hand-typed rows under a former name following; the objectives entered under the
old name move with it. The feed's settled board — twenty minutes past the close, the rule the
pace analysis already used — is filed as the final at the ranks the harvest holds nothing for,
which is the cut most weeks, until the harvest's own page replaces it. And the model dates every
edition it reads into its season: each rank of the previous-edition table carries the season
and date of the edition it was read off, the model carries the seasons the history spans and,
per turn of season and per band of rank (to 100, to 500, deeper), the median move shrunk by
n / (n + 10) readings and the 80th percentile of what the move leaves; a reading carried across
a turn of season is moved by the shift and its band widened by that spread, and a turn no cup
has crossed yet moves nothing and widens by the spread every past turn showed (15 to 18 %). The
level rung moves the same way. One more rule was measured on the way: the edition read under
the same entry bar as the cup coming up is read only when it was played in the same season as
the latest — on the 58 editions where the two differ, a same-bar edition a season back reads
worse than last week's under the other bar at every band (4.8 / 4.7 / 6.3 / 14.7 % against
4.7 / 4.4 / 5.8 / 13.8 %), and better only within the season (2.7 % against 6.4 % at the top 25).
The page says which edition a rank was read off when it is not the latest, and what the turn of
season did to it.

**Measured.** The season shift alone, out of sample on the last three turns of season — each
first edition of the new season forecast from its last edition of the old, the shift known only
from the categories whose first new-season edition fell on an earlier day, 470 readings: the
top hundred 3.8 → 3.6 % median error, ranks 101–500 5.3 → 4.8 %, deeper 8.7 → 8.1 %; at 41 → 42,
5.0 → 4.2, 8.1 → 4.9 and 11.1 → 8.1 %; at 40 → 41, which did not move, 2.6 → 2.7, 2.8 → 3.9 and
6.1 → 7.2 %, the cost of a shift measured on noise, which the shrinkage bounds. The whole cold
model, `analysis/validate.py`'s rolling forecast of the newest 600 tournaments on this
database: median error 4.7 → 4.1 %, coverage 93 → 92 % of an 80 % band, by band 5.9 → 5.3 %
(ranks 1–5), 3.7 → 3.1 (6–25), 3.3 → 3.2 (26–100), 4.6 → 4.0 (101–500), 10.8 → 10.5 (deeper).
Most of that is the renamed history: the same code on the old names reads 4.7 % and on the
unified names 4.2 %, with 322 more rows answered by a previous edition and 204 fewer by the
scoring table alone. The season shift is the rest, and it lands where it should: on the 62
tournaments that were the first edition of their cup in season 42, 607 rows, 3.83 → 3.45 %
with the bias −1.9 → −0.6 % and the band's coverage 82 → 93 %; by band 5.9 → 3.8 % (101–500,
bias −2.6 → 0.0) and 10.4 → 8.9 % (deeper, −8.7 → −4.3); on 29 August, the Console cups' first
day, 8.8 → 6.1 %, on 5 September 4.3 → 2.5 %, and on 25 August, the first day with anything to
measure a shift on (two categories), 3.4 → 3.6 %. Everything else is unchanged to the decimal.

**And the cup that started this.** With the history unified and the seasons in, the page would
have opened at 169 for the top 4,000 rather than 156 — the 25 July row moved by the +8.5 %
the season's deeper ranks had shown — and, with the 5 September edition read at that rank, which
is what the cut's page gives, at 212 with the band widened for the other entry bar. The board
read 179 at the buzzer and was still climbing.

**Left.** The first day of a season still carries the previous one forward, band widened; the
deep band's move is the noisiest and the shrinkage bounds it rather than fixes it. A cup
renamed keeps its whole history under its newest name, which files last season's real FNCS
division cups under "Practice" — right for the model, odd on a list. The cuts' pages of past
windows arrive with the next shallow pass, one request per window that has a cut, and the
build derives those windows again as they land.

`harvest_osirion.py`, `pull_live.py` and `calibration.py` carry it; `export_model.py` packs the
seasons and the page reads them; `analysis/validate.py` measures the whole.

---

## A cup nobody has seen, 20 September 2026

Two complaints, both right. A skin cup that pays the win 60 and the kill 2 was priced at 161
points before it ran, at a rank where 150 is what three hours of ordinary games make; and the
forecast of a cup under way drifts, down through the middle of the session and back up towards
the end, or the reverse, by more than the readings move. The first is the cold start: the
model's last rungs read a *level* off the scoring table — a share of the maximum game score,
measured on cups of the same kind — and a share is one number for a whole family of tables.
On the 46 cups since mid-August that had no edition anywhere, the scoring rung ran 13 % low at
ranks 1–5 and 8 % low at 26–100 on Battle Royale, 15 to 22 % low on mobile, 17 % high on Reload,
and at depth it collapsed: 423 at rank 20 and 37 at rank 1,000 for a Trio cup whose board read
546 and 331. The second is the pace: which curve says how far along a board is at a given
minute, and for which cups.

**Replaying the boards.** What a harvested board says is finer than a level: every roster's
placement and eliminations in every game it played, with the time each game ended. Re-scored
under the new cup's table — this placement worth that many points, these kills at that rate,
capped where the table caps them, the first *n* games in the order they were played where the
cup allows *n* — the same games give the standings the new table would have produced, and rank
20 of those standings is a forecast for rank 20 of the new cup. This is the "average tournament
of a top-x roster", replayed rather than modelled: the same evenings, the same lobbies, another
table, so a table that rewards placement over kills or the reverse is priced by what those
players actually did, not by a share. It also answers the objection to any average: a roster
that takes second place eight times and one that takes a win and seven top-hundreds are two
rosters, and each is re-scored as itself. Donors are the six most recent boards of the same
region, team size, game mode and platform (mobile, console, PC — read off the name, as the cold
signature reads it), open queues only, the one-lobby formats left out (`arena`, `evaluation`,
`test cup`, `ranked cup`: a first version took them in and priced a Battle Royale cup off an
Arena test lobby, 35 % high); those played to the same number of games first, the others scaled
by the measured per-game exponent, 0.93. The median across donors is the reading; its band, the
80th percentile of its error on the cups below, is 12 %.

Two limits, both measured. A donor board is loaded three pages deep — three hundred rosters —
and the re-ordering pulls up rosters from below the pages held: on the same cups, ranks 101–250
of a 300-roster replay ran 9 % low. So the table is trusted down to a third of the rosters
loaded, rank 100 for three pages, and the harvest now reads the last three weeks' windows ten
pages deep on every pass — seven more requests per window, the boards the replay is made of,
which puts rank 250 within reach. Past that depth the forecast continues from the deepest rank
read along the ladder below. And a replay under another table than the form's is no reading:
the table carries the signature of the scoring it was replayed under and the page reads it only
while the form matches.

**The ladder.** The shape that continued the level past the ranks a cup's own editions reach was
the fitted curve, `exp(-a q^b)` per band of field size. Measured on 416 open-queue boards played
after 15 August and read from the ones before, its ratio to rank 100 ran 9 % low at rank 500,
13 % low at 1,000 and 8 % low at 2,500 — a fifth to a third low for fields of one to three
thousand. Two parameters cannot bend both ends of a ladder. The ladder is the pooled shape that
carries the field: every open-queue board of the same band of field size and game mode, its
thresholds relative to rank 20 at the harvest's fifteen fixed ranks, one median per rank, at
least twenty boards a rank, the game mode's row first and the band's row across game modes as
the fallback, none below three hundred teams where the curve fitted on those small queues reads
better. The bands are finer than the curve's four — eleven, edges at 300, 450, 650, 1,000,
1,400, 2,000, 3,000, 4,500, 6,500 and the API's ceiling — because rank 1,000 is the last place
of a 1,100-team queue and the middle of a 2,900-team one, and one median for both read 0.21 of
rank 20 where the two ends read 0.10 and 0.40, with a spread of 87 % to say so. On the same
split, 472 boards, relative to rank 20: rank 100 4.7 → 2.3 % median error, rank 250 7.1 → 3.7,
rank 500 12.2 → 5.6, rank 1,000 18.6 → 9.6, rank 2,500 19.2 → 17.6, the bias within a point
of zero where it was −4 to −15. On the rolling validation of the whole model (5,929 thresholds)
the ladder changes 1,849 of them and the medians barely move — 6.5 → 6.1 % at ranks 1–5,
10.4 → 10.2 deeper, 4.79 → 4.85 over everything, the coverage 92 → 93 % — because a shape is
only half of a threshold: on the cups priced off the scoring table the shape error halves
(6.9 → 3.5 % at 26–500, bias −4.2 → −0.1; past 500 −10.8 → +5.6) while the level's bias, which
the old curve's bias had been cancelling on Reload, now shows. The level is the replay's job.

**Together, measured.** `analysis/rescore.py` takes the cups since 15 August that the rolling
validation priced off the scoring table or the family — 102 open queues, every one of them
forecast from boards played before its day — and replays each under its own table:

| rank band | thresholds | replay, median error | replay, bias | the model's cold rungs | bias |
|---|---|---|---|---|---|
| 1–5 | 306 | 4.8 % | +1.3 % | 8.1 % | −2.1 % |
| 6–25 | 307 | 3.8 % | +1.6 % | 9.0 % | −1.8 % |
| 26–100 | 230 | 3.7 % | +0.2 % | 10.0 % | −3.9 % |
| 101–250 | 225 | 4.2 % | −0.5 % | 10.2 % | −2.6 % |
| 251–1000 | 99 | 6.2 % | −0.3 % | 11.4 % | +5.5 % |
| 1001 and deeper | 10 | 15.9 % | −12.8 % | 18.6 % | +11.5 % |

The bands past 100 are the replay continued along the ladder from the deepest rank read — a
hundred with the three-page boards this was measured on; the last row is ten thresholds of
ten cups and says little either way. On the cups that had
already run in another region, half of the family's reading and half of the replay's, in log
terms, beat either alone — 3.5 % against 5.9 and 6.5 on the seven day-two cups of the sample —
so that is the rule where both exist; the cup's own editions in its own region still come
first, untouched. The model's mobile bias goes with it: mobile cups 14.5 → 5.7 % at ranks 1–100.
`calendar_snapshot.py` writes the replay beside every row of the week whose cup has no finished
edition in its region — a dozen numbers per cup, never a board — and the page reads it as one
more rung between the cup's own editions and the scoring table, saying which boards it rests
on and to what depth. `calibration.replay_reading` and the export's port agree with the page to
the rounding on every branch, the same check as the rest.

**The pace, by kind of cup.** The feed's own evenings answer the second complaint. On the week
of 14 September, replayed through the page with the tables of the week before, the forecast at
mid-session was 3.9 % off in median and unbiased on aggregate — no dip and no rise across the
86 evenings together — and wrong in two places by construction. A mobile Reload cup read its
own pace off its June editions, replayed from the harvest, and that row said the board was
complete at seven tenths of the session: the forecast ran 27 % under the final at that point
and climbed back. And within one family of format, the cups do not queue alike: at half the
session, ranks 1–25, the FNCS Duo practice cups had reached 45 % of their final where the skin
cups of the same 180-minute, 11-game format had reached 56 %, so a family pooled over both —
fifty FNCS evenings to seven — priced a new skin cup high at mid-session, 7 % on the Kingdom
Hearts cups, and let it fall for two hours. The family rows are now kept per kind of cup and platform as well as
pooled, the page reading the kind's row first; a category's replayed pace counts as the cup's
own only while its editions are within 45 days and of the same length and game count, else the
family's answers. Measured out of sample on the week's 67 evenings, from the evenings before:
median error at mid-session 2.8 → 2.5 %, the 90th percentile 7.9 → 7.1 %, the skin cups' bias
+1.7 → +0.4 %, the mobile Reload cup −19 → +5 %. The pace tables are replayed by the refresh
itself every three days from now on, so the feed's evenings of a cup's week reach its row before
its next week.

**Cups the feed never followed.** The feed reads the cups on the published calendar, and the
calendar was written by the machine that harvests, when it ran: a cup Epic announced between two
runs ran unfollowed, and an evening the feed did not sit through cannot be recovered. The
predictor repository's own workflow now refreshes the calendar every three hours — Osirion's
public calendar, eight requests, the same derived rows — keeping the replay cells the machine
wrote for the rows still listed, and the refresh on the machine takes those commits in before
writing, settling a conflict on the generated files by keeping its own. The page says, on a cup
that is over with nothing from the feed, that the cup was not on the calendar while it ran.

**Left.** The replay's donors are the region's own boards; at the start of a season the first
cold cups replay boards of the season before, unmoved by the season shift the direct rung
applies — a week of the new season and the donors are new too. The ladder past rank 2,500
rests on the few hundred boards read that deep. The kind's pace row needs eight boards of its
kind and format before it exists; a new format's first evenings read the pooled row.

`rescore.py` and `calendar_snapshot.py` carry the replay, `calibration.py` the ladder and the
rung, `analysis/live.py` the pace by kind, `analysis/rescore.py` the measurement.

---

## History and readings, weighed again, 22 September 2026

Two changes to how the model weighs what it knows, one before the cup and one during it, and
two ideas measured and left out.

**The previous edition, smoothed.** The first rung read a cup's latest edition straight, and at
the top of a board one edition is one team's great night. The editions before it say the same
thing with more nights behind them when they were played the same way, so the rung now reads an
exponentially weighted average of the run of editions with the same entry bar, season, number of
games and scoring table as the latest - in log terms, the latest weighing 0.7 and each one before
it 0.7 of what is left, six editions at most. A run of one is the latest edition, as before. This
is not the median of the last few editions that lost to the latest one in the level's early
days (5.4 % against 5.7 % for three, 6.4 % for eight): the run stops at the first edition played
another way, and the latest keeps most of the weight. Through the rolling validation of the
newest 600 tournaments, the smoothing changes 1,711 thresholds of 193 cups: the median error does
not move (2.84 %), the mean goes from 7.24 to 6.59 % (the gain's 95 % interval, resampling cups,
0.33 to 1.02 points) and the 90th percentile from 14.0 to 13.4 %. A weight of 0.8 gains less on
the mean (6.71 %), 0.6 and 0.5 start to cost the median (2.96 and 3.06 %). A trend - the last move
carried forward, even damped - lost everywhere: cups do not drift, they wobble.

**Readings against history.** During a cup the page weighs its readings against the forecast
made before it by precision: each weighs the inverse of its width squared. The two widths were
not in the same units. The history's is the width of a bad week (the 80th percentile of the moves
between editions) and the readings' the spread of a board around its usual share of the final at
that minute. On the evenings of 14 to 20 September, between a third and two thirds of the
session, the forecast of a cup with a previous edition was 2.8 % off in median where the forecast
made before the cup, left alone, was 2.35 % off: the readings had taken half the answer by
mid-session and dragged it with every early reading, and it travelled 17 % over its evening.

`analysis/blend.py` now measures, on the evenings the feed followed, each side's typical error
in units of its own width - the median of |log(forecast / final)| over the band for the history,
by rung, and the median of |log(reading extrapolated / final)| over the pace's width for the
readings - and the page scales the history's width by the ratio of the two before weighing them.
A rung is scaled only when twenty cups or more measure it. The range drawn around the answer keeps
the widths as they stood: the multipliers that turn a width into a range were measured on those,
and a range drawn through the scaled width held fewer finals than it claimed. Fitted on the
evenings to 13 September and replayed through the page on the 14th to the 20th (34,892 answers):

| share of the session elapsed | median error before | after | 90th percentile before | after |
|---|---:|---:|---:|---:|
| 0.20 - 0.35 | 4.2 % | 4.0 % | 15.1 % | 16.2 % |
| 0.35 - 0.50 | 4.0 % | 3.6 % | 11.6 % | 11.4 % |
| 0.50 - 0.65 | 3.4 % | 3.1 % | 9.7 % | 9.6 % |
| 0.65 - 0.80 | 3.0 % | 2.9 % | 9.4 % | 9.6 % |
| 0.80 - 0.90 | 2.4 % | 2.4 % | 9.4 % | 10.0 % |
| 0.90 - 1.00 | 2.3 % | 2.2 % | 7.9 % | 8.3 % |
| whole evening | 2.21 % | 2.13 % | 9.8 % | 9.9 % |

The ranges held what they claimed as before (46 % in the inner one, 88 % in the outer), and the
forecast of a cup with a previous edition travelled 12 % over its evening instead of 17 %, its
worst moment 4.2 % off instead of 5.2 %. The tail is the price: late in the session a forecast
from history that was badly off now holds on a little longer. Measured again on the database of
22 September - 95 more evenings, and a more complete harvest of the ones before - the history of
a cup with a previous edition typically misses by 0.93 of what the readings do rather than 0.67,
so the page leans on it less than in the test above; the refresh measures the scales again every
three days, with the pace tables and the validation.

**Tried and left out.** Trusting the readings more when they disagree with the history by far
more than both widths allow - the signature of a stale history: on both weeks it made the tail
worse (between a fifth and a third of the session, 90th percentile 16.4 → 18.3 % with the threshold at two widths),
because a large disagreement early in a session is more often the readings' noise than the
history's mistake. And the regions that already played a cup this week: how far a cup moves
against its previous edition does line up across regions (correlation 0.43 to 0.63 by band of
rank), but correcting last week's reading by the earlier regions' move lost where it matters
most - fitted before July and applied after, ranks 1-25 went from 2.11 to 2.53 % median error,
the other bands gained under a tenth of a point.

`calibration.py` carries the smoothing (`DIRECT_ALPHA`, `DIRECT_RUN`), `analysis/blend.py` the
scales, `export_model.py` carries them into the model as `blend`, and the page's
`coldWeightScale` reads them.

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

So the page no longer assumes it. `validate.py` now reports the quantiles of the
standardised error itself — u = log(truth / forecast) / rel, on every held-out
threshold — and the model carries them as `quality.bands`, in units of the band the
forecast quotes:

| share of cups inside | interval, in units of the quoted band |
|---|---|
| 50 % | −0.32 … +0.45 |
| 80 % | −0.81 … +0.85 |
| 90 % | −1.28 … +1.09 |

(5,290 held-out thresholds, September 2026.) The 80 % row recovers the band the page
used to draw single-handed — ±0.83 of itself — which is the same statement as "85 %
coverage for a claimed 80 %", read from the other end. The page shows the 50 % and the
90 % rows: a range that is right half the time is worth more to a player than one that
is right nine times in ten and four times as wide, and quoting both says which is which.
They are asymmetric because the error is: log-normal-ish with a right tail, a threshold
can double and cannot go below zero. The multipliers are measured on the cold forecast,
where the held-out test lives; the live blend quotes its own `rel` and the same shape is
applied to it, which is an assumption the live models cannot yet test.

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
