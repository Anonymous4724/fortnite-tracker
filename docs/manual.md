[Version française](manual.fr.md)

# Fortnite Comp Tracker

Tracks and predicts the point thresholds of Fortnite competitions, with automatic standings
retrieval.

Launch the app, see the tournaments currently running, click the one you want to track. The app
reads the standings on its own every 10 minutes and tells you where the thresholds will land. The
interface is bilingual and starts in English; the **EN/FR** switch sits at the top right of every
page.

---

## Installation

Python 3.10 or later.

```bash
pip install -r requirements.txt
python src/app.py
```

The browser opens on `http://127.0.0.1:5000`. On Windows: double-click **`tracker.bat`**.

On first launch, the app asks for your **Cito API key** — a free account at citoapi.com, 500
requests a month. It's saved to `cito_key.txt`; keep it to yourself.

A database created by an earlier version is upgraded automatically, without losing anything.

---

## How it works

### Home

Two sections: **live tournaments**, pulled from the API, and **tracked tournaments**, grouped by
category and region.

One click on *Start predicting* on a live tournament is enough: the app creates the competition
with its schedule, region and format, reads the full standings, and opens the tracking view. One
request.

### The tracking view, three panels

**On the left, the format.** Schedule, collection end, number of games, sealed or maximum format,
games already played, and the **scoring table inferred automatically** from the games in the
standings. This is also where you set your **objectives** — see below.

**In the middle, the standings.** Every team, with its score, eliminations and games played. Rows
matching your objectives are highlighted and stay visible even deep in the table. A search box
finds a team.

**Click a team** to unfold its full history: every game with its time, placement, eliminations and
points. That detail arrives in the same response as the standings, so it costs **no extra
request** — on a 1,271-team tournament, that's more than 5,000 games recorded in one go.

**On the right, the predictions.** The estimated final threshold for each tracked rank, your
objectives first, with the range and the model breakdown. Plus the progress curve with its
predicted extension.

The standings refresh **every 10 minutes** on their own, with a visible countdown. A button forces
an immediate reading right after a game.

### The starting estimate

The moment you start tracking, the app computes an estimate of the thresholds **before reading a
single standings snapshot** — from the scoring table, the number of games, and the history of
tournaments in the same category alone. This estimate is **frozen** and stays displayed at the top
of the predictions panel for the whole tournament, then gets compared to the real result at the
end.

This is the measurement that matters for predicting a competition that has never been run. The
*History* page charts its error tournament after tournament: the more history a category builds
up, the lower that curve gets.

When no tournament in the exact category exists yet, the app says so and widens the range honestly
rather than pretending.

### Training the estimate without waiting

**Train** tab. The cold-start estimate needs past editions of the same category; rather than
waiting weeks, read the final thresholds off Fortnite Tracker and type them in: one row per
edition, one column per rank.

Give the category, region, number of games and scoring table — pulled automatically if the
category is already known — then the thresholds. On save, the app immediately shows what it will
predict for the next tournament in that category, before and after your entry, with the tightened
range.

Three or four editions are enough to get out of the "no reference" regime. No API request is
spent: it's pure typing.

These entries don't show up in the list of tracked tournaments — only in the history, where they
serve as a reference.

### Comparing like with like

A **Division 1** and a **Division 5** carry almost the same name but have neither the same level
nor, sometimes, the same scoring table; a **Reload Zero Build** cup has nothing to do with a Battle
Royale. So the app looks for comparable tournaments in this order:

1. same exact category, same region;
2. same category, any region;
3. same series and same stage, if the tournament comes from a manual series;
4. as a last resort, same region and same mode — flagged as approximate.

The category is the tournament's name stripped of its week number and region, which groups
successive editions of the same tournament without mixing divisions.

### Objectives

The scoring table is the same everywhere, but **the rank that qualifies or wins the skin changes
by region**. So for each tournament category and each region, you set the ranks that matter:
"Qualification top 500", "Skin top 1000". The app remembers them and reuses them automatically
next time.

These ranks become the main targets of the predictions; the app also records reference ranks (1,
5, 10, 20, 50, 100, 250, 500, 1000) to feed the calibration.

### The quota

Top right: requests used this month, and how many tournaments you can still track. Refreshing
every 10 minutes over a 3-hour session costs 18 requests — about **27 tournaments a month** on the
free tier.

The list of live tournaments is cached for a few minutes: reloading the page costs nothing.

### Cleanup

On launch, tournaments that are **finished and that you never started tracking** are deleted. Ones
you tracked are kept and serve as a reference for future estimates. Manually entered competitions
are never touched.

### What gets inferred without typing anything

| Information | Where it comes from |
|---|---|
| **Starting estimate** | The scoring/threshold ratio learned from past editions of the same category, applied to this tournament's scoring table and number of games. |
| **Scoring table** | Every game gives a placement, eliminations and points scored: two equations are enough to recover the full table. Verified 100% correct on test data. |
| **Sealed or maximum format** | If every team starts the same game in the same minute, it's sealed; the interval between games follows from that. |
| **Number of games** | The observed maximum, backed up by earlier editions of the same tournament so it isn't underestimated early in the session. |
| **Team mode** | The team size read off the standings: Solo, Duo, Trio or Squad. |
| **Games played** | The median across the top of the standings, more reliable than estimating from the clock. |

### Manual entry

Always available under the *Manual entry* tab, with series, saved scoring tables, and readings
typed by hand. Useful when the API doesn't respond mid-tournament, or for a tournament missing
from the list.

---

## Running a tournament

### 1. Create the competition

Name, region, mode (Solo / Duo / Trio / Squad), game type, start time and **official end time**.
Three more settings, pre-filled from the mode:

| Setting | What it's for |
|---|---|
| **Game length** | 30 min in Battle Royale and Zero Build, 18 in Reload, 12 in Blitz. |
| **Tracker lag** | How long Fortnite Tracker takes to publish a finished game (5 min by default). |
| **Number of games** | How many games count in the session (10 for a cash cup, 6 for a final). |
| **Game counting** | *Maximum* or *sealed* — see just below. |

#### Maximum or sealed?

Two formats, predicted differently:

**Maximum** — everyone plays whenever they want, up to N games. This is the case for opens and
cash cups. The app can only estimate how many games have been played so far from the clock,
assuming they run back-to-back at a steady pace. You can correct that estimate by entering the
exact game count in each reading.

**Sealed** — every game starts at a fixed time and everyone plays the same number of them. This is
the format of finals. You give the number of games and the interval between two starts, and the
app works out the full schedule:

```
Game 1: starts 19:00 · standings updated 19:35
Game 2: starts 19:35 · standings updated 20:10
…
Game 6: starts 21:55 · standings updated 22:30
```

Here, nothing is estimated any more: the app knows exactly how many games are already in the
standings at any moment, and the collection end is that of the schedule's last game — not the
displayed closing time. The banner at the top shows a countdown to the next game, and the schedule
marks which row is already accounted for: that's the right moment to take a reading.

### 2. Enter readings

The *Now* button fills in the time. Type the thresholds read off Fortnite Tracker, and if you
want:

- **Games played** — the counter increments itself after each entry. This is what makes
  prediction possible from the first game onward (see below).
- **My points** — your own points, for the *My race* card.

Double-click any value in the table to correct it.

### 3. Read the predictions

They recompute on every entry: estimated final threshold per top with its range, what's left to do
for each objective, and the trend of upcoming competitions of the same type.

### 4. Finish the tournament

The **Finish the tournament** button does three things at once:

1. it marks the tournament as finished;
2. it takes the last reading as the definitive results (editable right after);
3. it saves the tournament to its own file, in the `data/tournaments/` folder.

The file name is built so the folder stays sorted and readable:

```
2026-08-28_1900_EU_Solo_battle-royale_solo-cash-cup-2.json
   date     time  region  mode      game type          tournament name
```

*Reopen the tournament* cancels the closing. *Save to tournaments/* rewrites the file on demand
(for instance after correcting a threshold). *Download this tournament* gives you the same file in
the browser, and it can be re-imported from the home page.

### 5. Chain into the next one

**Duplicate for next time** recreates the competition with the same settings (region, mode,
durations, ranks, scoring table) at the date you give, without the readings. The number in the
name gets incremented when there is one: "Cash Cup #3" becomes "Cash Cup #4".

---

## Series, stages and scoring

### Saved scoring

**Scoring** tab: create a points system under whatever name you like ("FNCS Duo 2026", "Cash Cup
R2"...), then pick it from a dropdown when creating a tournament or a series. One row per tier
(`1 = 60`, `11-15 = 20`) plus points per elimination.

Editing a scoring table doesn't affect tournaments already created: each one keeps the copy it
started with, so the history stays accurate.

### Series

**Series** tab: a series is a tournament that comes back — an FNCS with its open and its final.
You define its stages **once**:

| Stage | Day | Time | Duration | Tracked ranks |
|---|---|---|---|---|
| Open | D+0 | 19:00 | 3h | 100, 500, 1000, 10000 |
| Final | D+1 | 19:00 | 3h | 1, 3, 5, 10, 20 |

Then, every week, you give a date and the **Create the stages** button generates the full edition:
both tournaments, at the right times, with the right ranks and the right scoring table.

**Make one series per region.** EU and NAC thresholds have nothing in common; keeping them
separate keeps each one's history clean and stops predictions from cross-contaminating.

---

## The algorithm learns from your history

Every time you finish a tournament, the app measures three things on comparable competitions and
feeds them back into the next predictions. It's visible in the **What the app has learned** card.

**1. The exponent of the "per game" model.** How much a top's threshold rises when the number of
games doubles. Starts at 0.93, then gets measured on your own curves, rank by rank.

**2. How reliable each model is.** The app replays every finished competition at 25%, 50% and 75%
of the session, compares what each model predicted against the real result, and works out which
one deserves to be trusted. These weights replace the defaults from the first finished competition
onward.

**3. The relationship between the scoring table and the thresholds reached.** The app computes the
value of a "reference game" in your scoring table (a top 10 plus two eliminations), multiplies it
by the number of games in the session, and measures what multiple of that total it takes to land
in each top. This ratio is **independent of the scoring table**: if your placement points go up
50%, the predicted thresholds go up 50% too.

It's this third point that enables **prediction before the first reading**: from the moment a
tournament is created, you get an estimate of every threshold, computed from the scoring table and
the number of games. Those two settings drive it directly — a scoring table multiplied by 1.5
gives thresholds multiplied by 1.5, and going from 10 games to 6 brings them down to 60%. In sealed
format, since the number of games is a certainty, the range is a bit tighter.

Measured, not guessed: leaving one tournament out at a time and forecasting it cold, the median
error is 6.4% and 86% of real thresholds fall inside the quoted range. It is best deep in the
standings (2.2% beyond rank 500) and worst in the top five (8.7%), where one team having a good
night moves the number. `docs/methodology.md` has the full table and says what the figures are
worth.

### Check that it's improving

```bash
python src/backtest.py --learning-curve
python src/backtest.py --learning-curve --stage Final
```

Replays your history in chronological order and shows the prediction error against how many
competitions have already been learned from, at 0, 1, 3 and 5 readings. This is the honest measure
of what the learning actually buys you on *your* data.

### Limits

The scoring/threshold ratio assumes the relative difficulty stays stable. If Epic changes the
*shape* of the scoring table (a lot more points for the win, say), the transposition stays
approximate until you've finished a tournament with the new table. And on a format you've never
played, the app has nothing to learn from: it falls back to the defaults, and says so.

---

## Points land after the end time

A game started just before the tournament closes still counts. If the session closes at 22:00, a
game started at 21:59 can run until 22:29, and Fortnite Tracker still takes ~5 min to display it:
**the thresholds actually keep moving until 22:35**.

The app calls this the *collection end*:

```
collection end = official end + game length + tracker lag
```

Every prediction targets that time, and the chart runs out to it — the vertical line marks the
official end. The banner at the top of the page shows both live countdowns.

Without this, predictions systematically underestimate the final thresholds: everyone's last game
is missing from the count.

---

## How the predictions work

### During the competition

Five models run in parallel on the series of readings for each rank (six counting the scoring-only
estimate, used before the first reading):

| Model | Idea |
|---|---|
| **Per game played** | `threshold × (total games / games played)^b`. Works **from the first reading onward**. The exponent `b` is below 1 (no one strings together ten top 1s in a row); it defaults to 0.93 and recalibrates on your data once there are 3 readings. |
| **Historical shape** | Across past competitions of the same type, at 50% of the session you'd on average reached X% of the final total. Apply that ratio to the current points. Also works from the first reading onward. |
| **Power curve** | `points = a × progress^b` over time: captures a rise that accelerates or tails off. |
| **Average pace (linear)** | Steady pace since the start. |
| **Recent pace** | Same, over the last 4 readings: reacts if the pace changes. |

Every model is **backtested on your own readings** (it's given the first k, asked for the (k+1)th,
and the error is measured). What's shown is the average weighted by the inverse square of that
error, and a model clearly worse than the best one is simply **dropped** — visible in "model
breakdown". With fewer than three readings, no backtest is possible: models are then weighted by a
prior confidence that favors *Per game played* and *Historical shape*.

The low-high range comes from the spread between the retained models and their backtest error,
widened by the time remaining, with a wider floor when there are only one or two readings.

### My race

If you enter your own points, the app shows your estimated rank (interpolated between known
thresholds), your pace per game, and for each top: the target threshold, the gap, the points per
game you need to hold over the remaining games, and **what that actually looks like** — "top 15
with 2 eliminations, or top 10 with none". This translation uses the tournament's scoring table.

### Entering a scoring table

The "placement points" field accepts a table **pasted as-is**, from osirion.gg, a rules page or a
spreadsheet:

```
1st	65 (+9)	32          1 = 65              Top 1: 65
2nd	56 (+4)	28          2-3 = 55            Top 2-3: 55
```

Ordinals (`1st`, `21st`, `1er`, `2e`), the `#` prefix, and gaps in parentheses are all handled.
When the table has **several points columns**, the app detects it and lets you pick which one to
use, with a preview of the first values — then cleans the field up. A "Eliminations: 2" line in
the pasted block also fills in the points per elimination.

### The scoring table

Chosen from a dropdown (see *Saved scoring* above) or edited directly in *Tournament settings*.
Three scoring tables ship as a starting point — **check them against the tournament's official
rules, they change from one season to the next**.

### Between competitions

The final thresholds of comparable finished competitions (definitive results first, last reading
otherwise) are combined into three readings: average of the last 5, median, and linear trend. The
*History & trends* page filters them by region / mode / game type.

A competition counts as finished if it was closed with the button, if its definitive results are
entered, or failing that if its last reading covers 90% of the session.

### Check reliability on your own data

```bash
python src/backtest.py
python src/backtest.py --region EU --team-mode Solo
```

Replays every finished competition as if it had stopped at 15%, 25%, 50%, 75% and 90%, and
compares the prediction to the real final threshold. Shows the average error per top and the
coverage rate of the range. This is the best way to know from what point you can actually trust
the numbers.

---

## Data

Everything is local, next to the script:

```
data/tracker.db        the full database
data/tournaments/      one JSON file per finished tournament
```

- **CSV export**: one row per reading and per rank, columns `type` (`reading` / `final_result`),
  `games`, `my_points` (fixed column names, not translated).
- **JSON export**: full backup, re-importable from the home page. A single tournament file can
  also be re-imported on its own.
- `FNT_DB` environment variable to change where the database lives.

---

## Automatic threshold retrieval

There's no free, keyless public API for tournament standings:

- Epic's official API
  (`events-public-service-live.ol.epicgames.com/api/v1/events/Fortnite/leaderboards/...`) requires
  an OAuth token tied to an Epic account and isn't publicly documented.
- Fortnite Tracker doesn't expose tournament thresholds in its public API.
- Third-party services offer a standings endpoint with an API key and a limited free tier (e.g.
  `api-fortnite.com`: `/api/v2/events/:eventId/windows/:eventWindowId/leaderboard`, or
  `citoapi.com`), on the order of a few hundred requests a month.

### The wiring itself

To wire in one of these APIs later, everything goes through one function:

```python
import db

with db.session() as conn:
    db.add_snapshot(conn, comp_id=3, ts="2026-08-28 20:45",
                    points={1: 158, 20: 129, 50: 119, 100: 112, 500: 91, 1000: 75},
                    games=6, my_points=104, note="auto import")
```

And for definitive results: `db.set_finals(conn, comp_id, {1: 268, 20: 214, ...})`.
A script run every 10 minutes during the competition is enough; nothing else in the app changes.

---

## Files

```
update.bat, site.bat, harvest.bat, tracker.bat   the launchers, at the root
src/app.py           Flask server + JSON API
src/cito.py          access to the Cito API (live tournaments, standings)
src/tracking.py      importing a tournament and refreshing its tracking
src/scoring_infer.py inferring the scoring table from the games
src/db.py            SQLite data model
src/predict.py       prediction models and personal tracking
src/calibration.py   what the app learns from the history
src/backtest.py      reliability and learning curve on your own tournaments
src/cleanup.py       housekeeping in the folder (double-click: src/cleanup.bat)
src/templates/       HTML pages
src/static/          CSS + home-grown SVG chart engine
data/            your database and your archived tournaments
```

Only dependency: Flask. The charts are drawn in SVG with no external library, so the app works
with no internet connection.
