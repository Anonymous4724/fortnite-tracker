"""Replay the harvested boards through time: what a threshold is worth mid-cup.

    python -m analysis.live              # every board with verified scoring
    python -m analysis.live --pages 3    # read deeper than the top 100 rosters
    python -m analysis.live --limit 200  # a quick look

Every leaderboard entry Osirion returns carries its per-game history — each
session's end time, placement and eliminations — and the scoring table has
been verified against the published totals. So the standings at any moment of
the session can be rebuilt exactly, for every tournament in the harvest, and
the question the live page asks can be measured rather than assumed: *at this
point of the session, what share of its final value has the threshold at rank
r reached?*

Two clocks, because the formats keep two kinds of time. A closed lobby plays
its games one after another, everyone in the same match, so the natural clock
is games played: the k-th of n. An open queue has thousands of teams playing
at their own pace, so the clock is the wall clock: elapsed over the window's
length. Both are reported; the page uses games for sealed formats and minutes
for open ones, which is what a player can actually read off the screen.

It also answers the question that decides what a reading is worth: does a
threshold read at one rank say anything about another? Write

    rho_r = observed_r / (share x final_r)

for the multiplicative error the pace curve alone makes at rank r. If a board
runs hot everywhere at once, rho is one number per board and a reading at rank
5 prices rank 20 as well; if the ranks wander independently, carrying the
reading across the ladder adds noise instead of removing it. The regression
slope of log rho_b on log rho_a is exactly how much of a reading to carry, and
it comes out very different for the two formats — see `carry`.

Writes `analysis/pace.json`, which export_model.py carries into model.json as
`pace` — the share curve, its dispersion, how much of a reading carries across
ranks, how many boards it all rests on; and, since the curve of every open
queue pooled is the median of cups that do not run alike, the curve and tail
of each kind of cup (its own recent editions, else its family), how far the
deep end runs from the top of the board, and how wide a live answer has to be
- the last two measured on the evenings the feed followed, which are in the
database once `pull_live` has filed them.
"""
from __future__ import annotations

import argparse
import collections
import re
import gzip
import json
import math
import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

import calibration
import db
import osirion
import harvest_osirion
from harvest_osirion import slug

PACE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pace.json")
# The rungs measured. The deep ones only answer when `--pages` reached them:
# a board read one page deep holds a hundred rosters, so rank 250 needs three
# pages and rank 2,500 needs twenty-five. They are here because the tail past
# the close is the one place the deep end may keep its own pace - see
# TAIL_BANDS - and a rank the rosters do not reach is simply skipped.
RANKS = (1, 3, 5, 10, 20, 25, 50, 100, 250, 500, 1000, 2500)
# The ranks the pooled numbers rest on. One page of rosters is the top 100, so
# on a board of two thousand teams the "final" threshold at rank 100 is the
# last row read rather than the last row there is, and at half time hardly any
# of those teams have finished: rank 100 correlates at -3 with everything,
# which is an artefact of the depth read, not a fact about tournaments. Ranks
# 1 to 25 are whole on every board.
POOLED_RANKS = (1, 3, 5, 10, 20, 25)
STEPS = tuple(round(0.1 * i, 1) for i in range(1, 11))     # τ = 0.1 … 1.0
# Minutes past the window's close. An open queue stops taking entries at the
# hour, but the games already under way keep landing for another quarter of
# an hour and more, and the board rises with them. A reading taken then is
# not a reading at the close, and the page needs to know the difference.
TAIL = (0, 5, 10, 15, 20, 30, 45, 60)
# One lobby: Battle Royale seats 100 players, Reload 40. A board with no more
# rosters than a lobby holds is a closed final; the rest is an open queue.
CLOSED_MAX = 50

# The tail the page actually meets, and why it needs its own measurement.
# `by_tail` above reads each roster's own session end times: twenty minutes
# past the buzzer every game has ended, so it reports the board final and
# reports it with no spread at all. The page reads none of that - it reads
# Osirion's published board through the feed, and Osirion lands minutes behind
# the games. Its copy can therefore be short of the final long after the last
# game is over, which the harvest cannot see and the feed's own history can:
# `pull_live` files every reading the feed took, and the final result is
# harvested later.
#
# Split by the one signal the page holds at the time: whether the standing it
# is looking at has been read again unchanged. Measured on the evenings the
# feed tracked, a standing younger than FEED_HELD minutes is up to five per
# cent short of the final, and one that has held that long never was - the
# difference between a board still being published and a board that is done.
FEED_HELD = 10                # minutes a standing must hold to count as still
FEED_MIN_ROWS = 60            # below this the page keeps the games' own clock

# The tail, rank by rank. `curve` pools it over POOLED_RANKS - the rungs whole
# on every board - and the page reads that one number at every rank, so the
# qualification cut and the bottom of the board are told the games land at the
# same pace. Whether they do is a question worth asking rather than assuming,
# and two stories pull opposite ways: the last game is played harder at the
# cut, where qualification is decided, than at the bottom, where a team with
# nothing left to play for logs off; and yet it is at the bottom that teams
# are still starting games at the buzzer, so the deep end may have more left
# to land. Measured per band and shipped where the boards are there; the page
# reads the band of the rank it is asked about and the pooled table otherwise.
#
# The deep bands carry a caveat of their own. A board's "final" here is the
# rosters read, so with three pages read the 250th best of the whole board is
# the 250th best of three hundred rosters: past the close, when nearly every
# game is in, that is the same team; mid-session it is not, which is why the
# session curve stays pooled over the top 25.
TAIL_BANDS = ((1, 25), (26, 100), (101, 500), (501, 0))
TAIL_BAND_MIN = 30            # readings a band's minute needs before it ships

# The share a threshold has reached at half the session, used only to turn the
# observed standings into rho for `carry`. Close to 0.5 on both clocks — the
# pace is linear — and the slope `carry` measures is insensitive to it, since
# dividing every rank of every board by the same number moves nothing.
CURVE_AT_HALF = {"open": 0.50, "closed": 0.504}

# The pace of a cup's own kind. One curve pooled over every open queue is the
# median of cups that do not run alike: a two-hour Battle Royale cup has a
# thirty-minute game still in the air at the buzzer and lands at 88 % of its
# final on the hour, a three-hour one at 92 %; a Zero Build cup queues faster
# than a Battle Royale one; a cup capped at ten short Reload games has nothing
# left to play in its last half hour. So the curve and the tail are also
# measured per family - game mode, team size, window length, game cap - and
# per category, the cup's own recent editions whatever the region: the same
# cup queues and plays the same way from one week to the next, and its kind
# drifts by the width of a family inside a month, so both rest on the most
# recent boards rather than on every board there is. Asked the page's own
# question - a reading at share s, extrapolated by the curve, against the
# final - the family's curve is wrong by a third less than the pooled one at
# half time on the harvest, and the two together cut the page's own error on
# the evenings the feed followed by a sixth from a third of the session on
# and by a third in the ten minutes after the close. Twentieths rather than
# tenths, because the early curve bends and the page interpolates linearly.
FAMILY_STEPS = tuple(round(0.05 * i, 2) for i in range(1, 21))
FAMILY_MIN, FAMILY_RECENT = 8, 30        # boards a family needs; the most recent it rests on
CAT_MIN, CAT_RECENT = 4, 12              # editions a category needs; the most recent it rests on
FAMILY_MINUTES = 5                       # a window's length is rounded to this many minutes
# The harvest replays a board from the rosters that finish in its first three
# pages, and mid-session that is not the board there was: the players at the
# top at half time who then stop playing finish outside those pages and are
# not on disk, so the replayed 25th place at half time runs low - by four to
# nine per cent in the casual Solo cups, where the turnover at the top is
# highest, and not at all in a divisional practice where everyone plays every
# game. Compared cup for cup on the same evenings, the feed's board is the
# real one. So a category's curve is measured on the feed's own evenings as
# well, from the top 25 the feed read, and the page prefers it once enough
# editions have been followed; the harvest's answers for the cups the feed
# has not followed yet, and for the families.
FEED_CAT_MIN = 4                         # evenings a feed-measured category needs
# A category's pace replayed from the harvest counts as the cup's own only
# while its editions are recent: a mobile Reload cup read off its June
# editions said the board was complete at seven tenths of the session, and
# the week's evenings ran a fifth under the forecast for it. Older than this
# the family's pace answers; the feed's own reading of a cup is always recent.
CAT_MAX_DAYS = 45

# How far ahead of, or behind, the top of the board a deeper rank runs at
# each tenth of the session. The pace curve is measured on the top 25 - the
# rungs whole on every harvested board - and the harvest cannot see a deep
# rank mid-session at all: with three pages read, the 250th best at half time
# is the 250th of the three hundred rosters that finish best, not of the
# thousands that played, and that number runs a third low. The feed sees the
# real board, and on the evenings it followed the deep end does not keep the
# top's pace - and it is the depth into the field that decides it, not the
# rank: the thousandth of a field of two thousand is the casual half, players
# who play their few games early and stop, and its threshold is a fifth
# further along at two thirds of the session than the top 25 and all but
# done by the last fifth; the thousandth of a field of ten thousand is the
# top tenth and keeps the top's pace to within a few per cent. So the ratio
# is measured per band of q = rank / field, as the band's share of its final
# over the top 25's at the same reading, one median per evening first so a
# three-hour cup read every five minutes does not outvote a short one, and
# shipped where enough evenings carry the band. Above the first band the top
# of the board is its own reference and the ratio is one.
DEPTH_BANDS = ((0.02, 0.05), (0.05, 0.1), (0.1, 0.2), (0.2, 0.5), (0.5, 1.0))
DEPTH_STEPS = tuple(round(0.1 * i, 1) for i in range(1, 11))
DEPTH_MIN = 15                           # evenings a band's tenth needs before it ships
LATE_MINUTES = 20                        # a feed reading this long past the close stands in for a final

# Two cases the table does not describe, met on the first day of the FNCS
# Solo qualifiers (28 September 2026, seven regions, read deep: at the cut
# in five regions, the 2,000th to the 8,000th, and at the 1,000th in Oceania
# and Asia). The page reads both the same way (DEPTH_CEILING_Q, DEPTH_FNCS_Q
# there).
#   A board at the API's ceiling counts 9,950 for any field of ten thousand
#   or more, so rank / 9,950 only bounds a rank's depth. The last band is the
#   casual half of a field, and only a board that shows its whole field can
#   place a rank in it: at the ceiling the page reads no deeper than the band
#   before, and the table is not measured past it. Europe's 8,000th read as
#   0.8 of 9,950 and kept within 5 % of the top's pace, where the casual
#   half's ratio put the reading carried to the end 9 % under its final.
#   An FNCS qualifier's deep end plays for the cut. At those ranks the share
#   of the final against the top 25's went from 0.97 at four tenths of the
#   session to 1.04 at the close - the course of the 0.1-0.2 band, an
#   ordinary cup's top tenth to fifth, to within 0.025 at every tenth - where
#   the band their depth gave, 0.2-0.5, runs to 1.10. Carried to the end on
#   the qualifier's own curve, the readings ran 4.3 % under their final with
#   the table and 0.4 % under with that band (1.8 % off on average). So the
#   table is measured on the other cups, an FNCS qualifier's rank is read no
#   deeper than that band, and past the close it carries the lead it had at
#   the buzzer into the games still landing, until its board is whole.
FIELD_CEILING = 9950
DEPTH_CEILING_Q = 0.49
DEPTH_FNCS_Q = 0.19

# The page reads a family's curve from FAMILY_FROM on and a depth ratio from
# DEPTH_FROM on, each ramping in over the tenth before: earlier the board
# holds a game or two, the curves are a few hundredths apart, the ratio
# crosses one steeply, and the spread of a reading already says more. The
# same numbers live in the page (FAMILY_FROM, DEPTH_FROM), which reads these
# tables exactly as `expected_share` below does.
FAMILY_FROM, DEPTH_FROM = 0.3, 0.4

# How wide a live answer has to be. The page quotes one half-width - the pace
# curve's own spread at the minute of the reading - and how wide the range
# really has to be for half the finals, or nine in ten, to fall inside it is
# a measurement, not a guess: `quality.bands` makes it for the cold forecast,
# in units of that half-width, and applying those units to a live answer
# left the 50 % range holding a quarter of the finals mid-session and the
# 90 % range three in four. So the same multipliers are measured here for
# the live answer, on the evenings the feed followed, by share of the
# session and past the close, and apart for the deep end, where the answer
# is both wider and, so far, skewed. In units of the reading's own
# half-width, of the extrapolation `board / e(s)` a reading makes on its own;
# the page blends that with the history, and the blend's standardised error
# was checked against these on the replayed evenings to within a tenth from
# a third of the session on.
LIVE_BINS = (("0.1-0.3", 0.1, 0.3), ("0.3-0.5", 0.3, 0.5), ("0.5-0.7", 0.5, 0.7),
             ("0.7-0.9", 0.7, 0.9), ("0.9-1.0", 0.9, 1.01))
LIVE_TAIL_BINS = (("+0-10", 0, 10), ("+10-20", 10, 20))
LIVE_RANK_BANDS = ((1, 500), (501, 0))
LIVE_BAND_MIN = 200                      # readings a bin needs before it ships


def when(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def page_file(comp: dict, page: int) -> str:
    return os.path.join(harvest_osirion.RAW, "leaderboards", slug(comp["event_id"]),
                        f"{slug(comp['window_id'])}_p{page:03d}.json.gz")


def sessions_of(comp: dict, pages: int):
    """(team -> [(session id, end time, points)]) for the rosters on disk."""
    scoring = comp["scoring"]
    if isinstance(scoring, str):
        scoring = json.loads(scoring)
    teams, disagree = [], 0
    for page in range(pages):
        path = page_file(comp, page)
        if not os.path.exists(path):
            break
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError, EOFError):
            break
        for entry in osirion.entries_of(payload):
            games = []
            for s in osirion._sessions(entry):
                end = when(s.get("endTime"))
                if not end:
                    continue
                points = osirion.points_for(
                    scoring, osirion._stat(s, osirion.PLACEMENT_STAT, 999),
                    osirion._stat(s, "TEAM_ELIMS_STAT_INDEX", 0))
                games.append((s.get("sessionId") or "", end, points))
            total = sum(p for _, _, p in games)
            if abs(total - float(entry.get("pointsEarned") or 0)) > 0.5:
                disagree += 1
            teams.append(games)
    return teams, disagree


def by_time(comp: dict, teams: list, steps=STEPS) -> dict:
    """Share of the final threshold at each step of the window, by rank."""
    begin = when(comp["start_time"].replace(" ", "T") + "+00:00")
    end = when((comp.get("end_time") or "").replace(" ", "T") + "+00:00")
    if not begin or not end or end <= begin:
        return {}
    span = end - begin
    finals = sorted((sum(p for _, _, p in games) for games in teams), reverse=True)
    out = {}
    for tau in steps:
        cut = begin + span * tau
        totals = sorted((sum(p for _, t, p in games if t <= cut) for games in teams), reverse=True)
        for rank in RANKS:
            if rank <= len(finals) and finals[rank - 1] > 0:
                out[(rank, tau)] = totals[rank - 1] / finals[rank - 1]
    return out


def by_tail(comp: dict, teams: list) -> dict:
    """Share of the final threshold at k minutes past the window's close."""
    end = when((comp.get("end_time") or "").replace(" ", "T") + "+00:00")
    if not end:
        return {}
    finals = sorted((sum(p for _, _, p in games) for games in teams), reverse=True)
    out = {}
    for minutes in TAIL:
        cut = end + timedelta(minutes=minutes)
        totals = sorted((sum(p for _, t, p in games if t <= cut) for games in teams), reverse=True)
        for rank in RANKS:
            if rank <= len(finals) and finals[rank - 1] > 0:
                out[(rank, minutes)] = totals[rank - 1] / finals[rank - 1]
    return out


def band_of(rank: int):
    """The tail band a rank falls in, as (low, high) with 0 for no high."""
    for low, high in TAIL_BANDS:
        if rank >= low and (not high or rank <= high):
            return (low, high)
    return None


def tail_by_rank(shares: dict, spread: bool = False) -> list:
    """The tail per band of rank: [[low, high, {minute: number}], ...].

    The number is the median share of the final threshold, or - with
    `spread` - half the p10-p90 band as a share of that median, the same
    width `dispersion` reports. A fourth cell carries how many readings the
    band's fullest minute rests on, for the report to print and for anyone
    reading `pace.json` to judge it by. A band's minute is left out below
    TAIL_BAND_MIN readings, and a band with no minute at all is dropped.
    """
    out = []
    for low, high in TAIL_BANDS:
        ranks = [r for r in RANKS if band_of(r) == (low, high)]
        # Rank 1 is out of the widths, as it is out of `dispersion`: a winner
        # often owes their evening to one exceptional game, and that is not
        # the uncertainty of a threshold. It stays in the medians, which a
        # single wild board cannot move.
        if spread:
            ranks = [r for r in ranks if r != 1]
        row, counted = {}, 0
        for minutes in TAIL:
            pooled = sorted(v for rank in ranks for v in shares.get((rank, minutes), []))
            counted = max(counted, len(pooled))
            if len(pooled) < TAIL_BAND_MIN:
                continue
            mid = statistics.median(pooled)
            if not spread:
                row[str(minutes)] = round(mid, 3)
            elif mid > 0:
                lo = pooled[int(0.1 * len(pooled))]
                hi = pooled[min(len(pooled) - 1, int(0.9 * len(pooled)))]
                row[str(minutes)] = round((hi - lo) / 2 / mid, 3)
        if row:
            out.append([low, high, row, counted])
    return out


def category_key(name) -> str:
    """A cup's name as a key: case and the dashes between a name and its
    round set aside, since the harvest writes "Cup - Round 2" and the
    calendar "Cup — Round 2". The page keys its own name the same way."""
    text = re.sub(r"[\u2014\u2013\-\u00b7]+", " ", str(name or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def family_key(comp: dict):
    """(game mode, team mode, window length in minutes, game cap)."""
    begin = when(str(comp.get("start_time") or "").replace(" ", "T") + "+00:00")
    end = when(str(comp.get("end_time") or "").replace(" ", "T") + "+00:00")
    minutes = (end - begin).total_seconds() / 60 if begin and end and end > begin else 0
    minutes = int(round(minutes / FAMILY_MINUTES) * FAMILY_MINUTES)
    return (str(comp.get("game_mode") or ""), str(comp.get("team_mode") or ""), minutes,
            int(comp.get("max_games") or 0))


def kind_key(comp: dict) -> tuple[str, str]:
    """(kind of cup, platform), as the cold start reads them off the name: an
    FNCS practice and a skin cup of the same format do not queue alike. On
    the feed's evenings of one week, ranks 1-25 at mid-session, the FNCS
    Duo cups had reached 45 % of their final where the skin cups of the same
    format had reached 56 %: a fifth of the forecast, one way or the other,
    for a family pooled over both. Measured out of sample on the week after,
    the rows by kind take the bias off the skin cups (+1.7 % -> +0.4 %) and
    the FNCS ones alike."""
    sig = calibration.cold_signature({"name": comp.get("name") or "", "kind": ""})
    return sig[2], sig[3]


def cup_curve(timed: dict, steps=FAMILY_STEPS) -> dict:
    """One share per step for one board, pooled over POOLED_RANKS."""
    out = {}
    for step in steps:
        vals = [timed[(r, step)] for r in POOLED_RANKS if (r, step) in timed]
        if vals:
            out[step] = statistics.median(vals)
    return out


def cup_tail(tailed: dict) -> dict:
    out = {}
    for minutes in TAIL:
        vals = [tailed[(r, minutes)] for r in POOLED_RANKS if (r, minutes) in tailed]
        if vals:
            out[minutes] = statistics.median(vals)
    return out


def kin_tables(cups: list) -> tuple[list, list]:
    """The pace of each family and each category of cup, from the most recent
    boards of each: `[[mode, team, minutes, games, curve, tail, null, boards,
    kind, platform]]` - one row per kind of cup and platform, and one pooled
    row with both blank, the fallback - and `[[name, curve, tail, boards,
    latest, "harvest", minutes, games]]`, curves keyed by step, tails by
    minute past the close. `cups` holds one dict per open board replayed,
    newest first: start, name, family, kind, curve, tail."""
    def tables(sub):
        curve, tail = {}, {}
        for step in FAMILY_STEPS:
            vals = [c["curve"][step] for c in sub if step in c["curve"]]
            if len(vals) >= 3:
                curve[f"{step:.2f}"] = round(statistics.median(vals), 3)
        for minutes in TAIL:
            vals = [c["tail"][minutes] for c in sub if minutes in c["tail"]]
            if len(vals) >= 3:
                tail[str(minutes)] = round(statistics.median(vals), 3)
        return curve, tail

    by_family, by_category = collections.defaultdict(list), collections.defaultdict(list)
    for cup in cups:
        # The family pooled, and the family by kind of cup and platform.
        by_family[cup["family"] + ("", "")].append(cup)
        by_family[cup["family"] + tuple(cup.get("kind") or ("", ""))].append(cup)
        by_category[cup["name"]].append(cup)
    families, categories = [], []
    for key, sub in sorted(by_family.items(), key=lambda kv: -len(kv[1])):
        sub = sub[:FAMILY_RECENT]
        if len(sub) < FAMILY_MIN:
            continue
        curve, tail = tables(sub)
        families.append([key[0], key[1], key[2], key[3], curve, tail, None, len(sub), key[4], key[5]])
    for name, sub in sorted(by_category.items(), key=lambda kv: -len(kv[1])):
        sub = sub[:CAT_RECENT]
        if len(sub) < CAT_MIN:
            continue
        curve, tail = tables(sub)
        # The format the editions replayed were played in, so the page reads
        # the row only for a cup of the same length and game count.
        fam = sub[0]["family"]
        categories.append([name, curve, tail, len(sub), sub[0]["start"][:10], "harvest", fam[2], fam[3]])
    return families, categories


def feed_categories(evenings: list) -> list:
    """The categories' curves and tails as the feed read them: the same rows
    as `kin_tables` makes from the harvest, from the top 25 of the evenings
    the feed followed - the median share of the final at each twentieth of
    the window over the readings within a fortieth of it, one median per
    evening, then over the last CAT_RECENT evenings of the cup."""
    by_name = collections.defaultdict(list)
    for ev in sorted(evenings, key=lambda e: str(e["comp"].get("start_time") or ""), reverse=True):
        field = ev["comp"].get("field_size") or 0
        if 0 < field <= 2 * CLOSED_MAX or str(ev["comp"].get("stage") or "").strip():
            continue
        at_step, at_tail = collections.defaultdict(list), collections.defaultdict(list)
        for share, after, points in ev["readings"]:
            shares = [p / ev["finals"][r] for r, p in points.items() if r <= 25 and ev["finals"].get(r)]
            if not shares:
                continue
            if after > 0:
                minute = min(TAIL, key=lambda m: abs(m - after))
                if abs(minute - after) <= 2.5:
                    at_tail[minute].append(statistics.median(shares))
            else:
                step = min(FAMILY_STEPS, key=lambda x: abs(x - share))
                if abs(step - share) <= 0.025:
                    at_step[step].append(statistics.median(shares))
        curve = {step: statistics.median(v) for step, v in at_step.items()}
        tail = {m: statistics.median(v) for m, v in at_tail.items()}
        if len(curve) >= 6:
            by_name[category_key(ev["comp"].get("name"))].append(
                {"start": str(ev["comp"]["start_time"]), "curve": curve, "tail": tail,
                 "family": family_key(ev["comp"])})
    out = []
    for name, subs in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
        sub = subs[:CAT_RECENT]
        if len(sub) < FEED_CAT_MIN:
            continue
        curve, tail = {}, {}
        for step in FAMILY_STEPS:
            vals = [c["curve"][step] for c in sub if step in c["curve"]]
            if len(vals) >= 3:
                curve[f"{step:.2f}"] = round(statistics.median(vals), 3)
        for minutes in TAIL:
            vals = [c["tail"][minutes] for c in sub if minutes in c["tail"]]
            if len(vals) >= 3:
                tail[str(minutes)] = round(statistics.median(vals), 3)
        # A row the page can read at the buzzer: the close is the step that
        # decides the last quarter of an hour, and a feed that only looked
        # every ten minutes can have missed it.
        if len(curve) >= 8 and "1.00" in curve and "0.50" in curve:
            fam = sub[0]["family"]
            out.append([name, curve, tail, len(sub), sub[0]["start"][:10], "feed", fam[2], fam[3]])
    return out


def days_between(earlier: str, later: str) -> int:
    """Whole days from one ISO date to another; 0 when either is unreadable."""
    try:
        a, b = date.fromisoformat(str(earlier)[:10]), date.fromisoformat(str(later)[:10])
    except ValueError:
        return 0
    return (b - a).days


def interp(table: dict, x: float, below_linear: bool = True):
    """Linear between the keys of `table` (numbers or their strings); below
    the first key linear in x itself when `below_linear`, flat otherwise;
    flat past the last. None for an empty table."""
    steps = sorted((float(k), float(v)) for k, v in (table or {}).items())
    if not steps:
        return None
    if x <= steps[0][0]:
        return steps[0][1] * (x / steps[0][0] if below_linear and steps[0][0] > 0 else 1)
    for (a, va), (b, vb) in zip(steps, steps[1:]):
        if x <= b:
            f = (x - a) / (b - a)
            return va * (1 - f) + vb * f
    return steps[-1][1]


def tracked_evenings(conn) -> list:
    """The open queues the feed followed, each with its readings clocked on
    the window and its finals: [{comp, minutes, readings: [(share, after,
    {rank: points})], finals: {rank: points}}]."""
    out = []
    comps = conn.execute(
        "SELECT id, name, region, start_time, end_time, field_size, max_games, team_mode, game_mode, stage "
        "FROM competition WHERE end_time IS NOT NULL AND start_time IS NOT NULL "
        "AND (SELECT COUNT(*) FROM snapshot s WHERE s.competition_id = competition.id "
        "AND s.note LIKE '%live feed%') >= 3").fetchall()
    for comp in comps:
        field = comp["field_size"] or 0
        if 0 < field <= 2 * CLOSED_MAX:       # a lobby is clocked on its games
            continue
        finals = {int(r["rank"]): float(r["points"]) for r in conn.execute(
            "SELECT rank, points FROM final_result WHERE competition_id = ?", (comp["id"],))
            if r["points"] and r["points"] > 0}
        if not finals:
            continue
        begin = when(str(comp["start_time"]).replace(" ", "T") + "+00:00")
        end = when(str(comp["end_time"]).replace(" ", "T") + "+00:00")
        if not begin or not end or end <= begin:
            continue
        span = (end - begin).total_seconds() / 60
        reads, late = [], {}
        for snap in conn.execute("SELECT id, ts FROM snapshot WHERE competition_id = ? AND "
                                 "note LIKE '%live feed%' ORDER BY ts", (comp["id"],)):
            stamp = when(str(snap["ts"]).replace(" ", "T") + "+00:00")
            points = {int(r["rank"]): float(r["points"]) for r in conn.execute(
                "SELECT rank, points FROM points WHERE snapshot_id = ?", (snap["id"],)) if r["points"] > 0}
            if not stamp or not points:
                continue
            elapsed = (stamp - begin).total_seconds() / 60
            reads.append((min(elapsed / span, 1.0), max(0.0, elapsed - span), points))
            # The harvest reads three pages; a cut deeper than that has no
            # final in the database, and the feed's own last looks stand in
            # for it: a threshold never falls, so the richest reading taken
            # once the board has had LATE_MINUTES to settle is the nearest
            # thing to the final.
            if elapsed - span >= LATE_MINUTES:
                for rank, p in points.items():
                    if p > late.get(rank, 0):
                        late[rank] = p
        for rank, p in late.items():
            finals.setdefault(rank, p)
        if len(reads) >= 3:
            out.append({"comp": dict(comp), "minutes": span, "readings": reads, "finals": finals})
    return out


def depth_band(q: float):
    """The band of DEPTH_BANDS a depth q = rank / field falls in, or None
    above the first: there the top of the board is its own reference."""
    for low, high in DEPTH_BANDS:
        if low <= q < high:
            return (low, high)
    return None


def fncs_qualifier(comp: dict) -> bool:
    """An FNCS qualifier - "FNCS Solos Qualifiers" and its later rounds - and
    not the skin, icon and practice cups that carry FNCS in their name: those
    are open cups like any other. The page's `fncsQualifier`."""
    name = str(comp.get("name") or "").lower()
    return "fncs" in name and "qualifier" in name


def depth_tables(evenings: list) -> list:
    """[[low, high, {tenth: ratio}, evenings], ...]: the share of the final
    the ranks of a band of q = rank / field have reached over the top 25's at
    the same tenth, median over the evenings the feed followed - the other
    cups' evenings: an FNCS qualifier reads its 0.1-0.2 band (DEPTH_FNCS_Q). On
    a field at the ceiling a rank past DEPTH_CEILING_Q is not placed in a
    band: its depth is not known."""
    per = collections.defaultdict(lambda: collections.defaultdict(list))   # (band, step) -> evening -> ratios
    for i, ev in enumerate(evenings):
        field = ev["comp"].get("field_size") or 0
        if field <= 0 or fncs_qualifier(ev["comp"]):
            continue
        top = collections.defaultdict(list)
        deep = collections.defaultdict(lambda: collections.defaultdict(list))
        for share, after, points in ev["readings"]:
            if after > 0:
                continue
            step = min(DEPTH_STEPS, key=lambda x: abs(x - share))
            if abs(step - share) > 0.05:
                continue
            for rank, p in points.items():
                final = ev["finals"].get(rank)
                if not final:
                    continue
                if rank <= 25:
                    top[step].append(p / final)
                elif field < FIELD_CEILING or rank / field < DEPTH_CEILING_Q:
                    band = depth_band(rank / field)
                    if band:
                        deep[band][step].append(p / final)
        for band, steps in deep.items():
            for step, vals in steps.items():
                if top.get(step) and statistics.median(top[step]) > 0.02:
                    per[(band, step)][i].append(statistics.median(vals) / statistics.median(top[step]))
    out = []
    for low, high in DEPTH_BANDS:
        row, counted = {}, 0
        for step in DEPTH_STEPS:
            vals = [statistics.median(v) for v in per.get(((low, high), step), {}).values()]
            counted = max(counted, len(vals))
            if len(vals) >= DEPTH_MIN:
                row[f"{step:.1f}"] = round(statistics.median(vals), 3)
        if row:
            out.append([low, high, row, counted])
    return out


def band_row(rows: list, rank: int):
    """The row of a [[low, high, ...]] table of ranks a rank falls in, or None."""
    for row in rows or []:
        low, high = int(row[0]), int(row[1])
        if rank >= low and (not high or rank <= high):
            return row
    return None


def depth_row(rows: list, q: float):
    """The row of the depth table a depth q falls in, or None."""
    for row in rows or []:
        if float(row[0]) <= q < float(row[1]):
            return row
    return None


def depth_ratio(comp: dict, rank: int, share: float, depth: list) -> float:
    """The depth ratio the page applies to a rank at a share of the session:
    the band's, ramped in over the tenth before DEPTH_FROM, read no deeper
    than DEPTH_CEILING_Q on a field at the ceiling, and no deeper than
    DEPTH_FNCS_Q in an FNCS qualifier. Mirrors `depthFor` in the page."""
    field = comp.get("field_size") or 0
    if field <= 0 or not rank or rank < 1:
        return 1.0
    q = rank / field
    if field >= FIELD_CEILING:
        q = min(q, DEPTH_CEILING_Q)
    if fncs_qualifier(comp):
        q = min(q, DEPTH_FNCS_Q)
    row = depth_row(depth or [], q)
    if not row or share < DEPTH_FROM - 0.1:
        return 1.0
    d = interp(row[2], share, below_linear=False) or 1.0
    if share < DEPTH_FROM:
        d = 1 + (d - 1) * max(0.0, (share - (DEPTH_FROM - 0.1)) / 0.1)
    return d


def expected_share(comp: dict, rank: int, share: float, after: float, pooled: dict,
                   families: list, categories: list, depth: list) -> float:
    """What the page expects a rank's board to have reached: the category's
    curve, else the family's, else the pooled one, ramped in from FAMILY_FROM;
    times the rank's depth ratio (`depth_ratio`); the matching tail past the
    close - in an FNCS qualifier times the ratio at the buzzer, up to the
    whole board. Mirrors `expectedAt` in the page, table for table."""
    kin = None
    name = category_key(comp.get("name"))
    mode, team, minutes, games = family_key(comp)
    kind, platform = kind_key(comp)
    day = str(comp.get("start_time") or "")[:10]
    # The feed's own reading of the cup first, the harvest's replay of it
    # next: the rows are listed in that order. A row of another format, or
    # a replay of editions older than CAT_MAX_DAYS, is not this cup's pace.
    for row in categories or []:
        if row[0] != name:
            continue
        if len(row) > 7 and row[6] and (abs(int(row[6]) - minutes) > 10 or int(row[7]) != games):
            continue
        if (len(row) <= 5 or row[5] != "feed") and row[4] and day and days_between(row[4], day) > CAT_MAX_DAYS:
            continue
        kin = {"curve": row[1], "tail": row[2]}
        break
    if kin is None:
        # The family by kind of cup and platform, then the family pooled.
        for want in ((kind, platform), ("", "")):
            for row in families or []:
                if row[0] == mode and row[1] == team and abs(int(row[2]) - minutes) <= 10 and int(row[3]) == games \
                        and (row[8] if len(row) > 9 else "", row[9] if len(row) > 9 else "") == want:
                    kin = {"curve": row[4], "tail": row[5]}
                    break
            if kin is not None:
                break
    if after > 0:
        tail = (kin or {}).get("tail") or {}
        if not tail:
            banded = band_row(pooled.get("tail_by_rank") or [], rank)
            tail = banded[2] if banded else pooled.get("tail") or {}
        t = interp(tail, after, below_linear=False)
        if t is not None and fncs_qualifier(comp):
            t = min(1.0, t * depth_ratio(comp, rank, 1.0, depth))
        return max(t, 0.05) if t is not None else 1.0
    base = interp(pooled.get("curve") or {}, share)
    m = interp((kin or {}).get("curve") or {}, share) if kin else None
    if m is None:
        m = base if base is not None else share
    elif share < FAMILY_FROM and base is not None:
        f = max(0.0, (share - (FAMILY_FROM - 0.1)) / 0.1)
        m = base * (1 - f) + m * f
    m *= depth_ratio(comp, rank, share, depth)
    return max(m, 0.05)


def live_bands(evenings: list, pooled: dict, families: list, categories: list, depth: list) -> list:
    """[[low, high, {bin: {"50": [lo, hi], "90": [lo, hi], "n": rows}}], ...]:
    the quartiles and the 5th and 95th centiles of log(final / extrapolation)
    over the reading's own half-width, by share of the session and past the
    close, for the top 500 and for the deep end. The extrapolation is the
    reading over the share expected of it, never under the board itself."""
    spread, tail_spread = pooled.get("dispersion") or {}, pooled.get("tail_dispersion") or {}
    feed = pooled.get("tail_feed") or {}
    rows = collections.defaultdict(list)
    for ev in evenings:
        comp = ev["comp"]
        for share, after, points in ev["readings"]:
            if share < 0.1:
                continue
            for rank, p in points.items():
                final = ev["finals"].get(rank)
                if not final or rank == 1:
                    continue
                e = expected_share(comp, rank, share, after, pooled, families, categories, depth)
                guess = max(p / e, p)
                if after > 0:
                    held = feed.get("settling")
                    banded = band_row(pooled.get("tail_spread_by_rank") or [], rank)
                    games = interp(banded[2] if banded else tail_spread, after, below_linear=False)
                    rel = max(held or 0, games or 0)
                    bin_ = next((b for b, lo, hi in LIVE_TAIL_BINS if lo <= after < hi), None)
                else:
                    rel = interp(spread, share, below_linear=False)
                    bin_ = next((b for b, lo, hi in LIVE_BINS if lo <= share < hi), None)
                if not bin_ or not rel or rel <= 0:
                    continue
                band = next(((lo, hi) for lo, hi in LIVE_RANK_BANDS if rank >= lo and (not hi or rank <= hi)), None)
                rows[(band, bin_)].append(math.log(final / guess) / rel)
    out = []
    for low, high in LIVE_RANK_BANDS:
        table = {}
        for bin_, _, _ in LIVE_BINS + LIVE_TAIL_BINS:
            vals = sorted(rows.get(((low, high), bin_), []))
            if len(vals) < LIVE_BAND_MIN:
                continue
            q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
            table[bin_] = {"50": [round(q(0.25), 3), round(q(0.75), 3)],
                           "90": [round(q(0.05), 3), round(q(0.95), 3)], "n": len(vals)}
        if table:
            out.append([low, high, table])
    return out


def by_game(teams: list) -> dict:
    """Share of the final threshold after game k of n, in a lobby that plays
    the same matches: a session id shared by half the board is one game."""
    end_of, count = {}, collections.Counter()
    for games in teams:
        for sid, end, _ in games:
            end_of[sid] = max(end_of.get(sid, end), end)
            count[sid] += 1
    shared = sorted((sid for sid in end_of if count[sid] >= 0.5 * len(teams)), key=end_of.get)
    if len(shared) < 3:
        return {}
    finals = sorted((sum(p for _, _, p in games) for games in teams), reverse=True)
    out = {}
    for k in range(1, len(shared) + 1):
        played = set(shared[:k])
        totals = sorted((sum(p for sid, _, p in games if sid in played) for games in teams),
                        reverse=True)
        for rank in RANKS:
            if rank <= len(finals) and finals[rank - 1] > 0:
                out[(rank, round(k / len(shared), 2))] = totals[rank - 1] / finals[rank - 1]
    out["games"] = len(shared)
    return out


# Lobbies of this many games are common enough to get a curve of their own,
# read at "after game k of n" exactly, instead of the pooled one snapped to
# tenths - where a six-game final's game 2 (0.33) lands between the 0.3 and 0.4
# steps that other game counts filled, and reads ten per cent too far along.
GAMES_MIN_BOARDS = 30


def games_curve(shares: dict) -> dict:
    """{n: {k: median share after game k of n}}, pooled over POOLED_RANKS."""
    out = {}
    for n in sorted({n for (n, _, _) in shares}):
        boards = max(len(shares.get((n, r, 1), [])) for r in POOLED_RANKS)
        if boards < GAMES_MIN_BOARDS:
            continue
        row = {}
        for k in range(1, n + 1):
            pooled = [v for rank in POOLED_RANKS for v in shares.get((n, rank, k), [])]
            if len(pooled) >= 5:
                row[str(k)] = round(statistics.median(pooled), 3)
        if row:
            out[str(n)] = row
    return out


def games_dispersion(shares: dict) -> dict:
    """Same shape as `games_curve`: half the p10-p90 band, as a share of the median."""
    out = {}
    for n in sorted({n for (n, _, _) in shares}):
        boards = max(len(shares.get((n, r, 1), [])) for r in POOLED_RANKS)
        if boards < GAMES_MIN_BOARDS:
            continue
        row = {}
        for k in range(1, n + 1):
            rel = []
            for rank in [r for r in POOLED_RANKS if r != 1]:
                values = sorted(shares.get((n, rank, k), []))
                if len(values) >= 5:
                    med = statistics.median(values)
                    if med > 0:
                        lo, hi = values[int(0.1 * len(values))], values[min(len(values) - 1, int(0.9 * len(values)))]
                        rel.append((hi - lo) / 2 / med)
            if rel:
                row[str(k)] = round(statistics.median(rel), 3)
        if row:
            out[str(n)] = row
    return out


def nearest_step(x: float) -> float:
    return min(STEPS, key=lambda s: abs(s - x))


def summarise(shares: dict) -> dict:
    """{rank: {step: {"median", "p10", "p90", "n"}}} over the boards."""
    table: dict = {}
    for (rank, step), values in shares.items():
        values = sorted(values)
        if len(values) < 3:
            continue
        table.setdefault(str(rank), {})[f"{step:.1f}"] = {
            "median": round(statistics.median(values), 3),
            "p10": round(values[int(0.1 * len(values))], 3),
            "p90": round(values[min(len(values) - 1, int(0.9 * len(values)))], 3),
            "n": len(values)}
    return table


def curve(shares: dict, steps=STEPS, key=lambda s: f"{s:.1f}") -> dict:
    """One share per step, pooled over POOLED_RANKS: those ranks agree to a few
    hundredths and the page needs a number, not a table."""
    out = {}
    for step in steps:
        pooled = [v for rank in POOLED_RANKS for v in shares.get((rank, step), [])]
        if len(pooled) >= 5:
            out[key(step)] = round(statistics.median(pooled), 3)
    return out


def carry(rows: list, at: float = 0.5) -> dict:
    """How much of a reading at one rank belongs to another, measured.

    `rows` is one dict per board, {rank: rho at that rank}, where

        rho_r = observed_r / (share x final_r)

    is the multiplicative error the pace curve alone makes there. Regress
    log rho_b on log rho_a across boards, pooled over every ordered pair of
    ranks, each rank first centred on its own median so that a curve that is
    slightly off at one rank does not read as agreement. The slope is the
    share of a reading to carry to another rank: 1 means the board moves as
    one and a reading prices the whole ladder, 0 means the ranks wander
    independently and a reading prices only itself.

    Centring each *board* on its own median would destroy the answer — it
    removes exactly the common factor being looked for — and did, in the first
    version of this function, which reported no carry anywhere.
    """
    ranks = [r for r in POOLED_RANKS if sum(1 for row in rows if r in row and row[r] > 0) >= 5]
    if len(ranks) < 2:
        return {}
    middle = {}
    for rank in ranks:
        values = [row[rank] for row in rows if rank in row and row[rank] > 0]
        middle[rank] = statistics.median(values)
    xs, ys = [], []
    for a in ranks:
        for b in ranks:
            if a == b:
                continue
            for row in rows:
                if row.get(a, 0) > 0 and row.get(b, 0) > 0:
                    xs.append(math.log(row[a] / middle[a]))
                    ys.append(math.log(row[b] / middle[b]))
    if len(xs) < 20:
        return {}
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
    sx, sy = statistics.pstdev(xs) or 1e-9, statistics.pstdev(ys) or 1e-9
    return {"slope": round(max(0.0, min(1.0, slope)), 3),
            "correlation": round(slope * sx / sy, 3),
            "boards": len(rows), "pairs": len(xs)}


def dispersion(shares: dict, steps=STEPS, key=lambda s: f"{s:.1f}") -> dict:
    """Half-width of the p10–p90 band as a share of the median, by step: the
    pace uncertainty the page quotes. Rank 1 is left out — a winner often owes
    their evening to one exceptional game, the same reason `fit_curve` drops
    it — and so are the ranks one page cannot see whole."""
    out = {}
    for step in steps:
        rel = []
        for rank in [r for r in POOLED_RANKS if r != 1]:
            values = sorted(shares.get((rank, step), []))
            if len(values) >= 5:
                med = statistics.median(values)
                if med > 0:
                    lo, hi = values[int(0.1 * len(values))], values[min(len(values) - 1, int(0.9 * len(values)))]
                    rel.append((hi - lo) / 2 / med)
        if rel:
            out[key(step)] = round(statistics.median(rel), 3)
    return out


def feed_rows(conn, tail: dict) -> list:
    """(minutes past the close, minutes this standing has held, error of the
    tail projection) for every reading the feed took past a window's close.

    The error is `final / (points / e(tau))` — how much the page's own
    projection was short — so the numbers below are in the units the page
    quotes, measured against the very table it will read.
    """
    if not tail:
        return []
    keys = sorted(float(k) for k in tail)

    def at(tau):
        if tau <= keys[0]:
            return tail[str(int(keys[0]))]
        if tau >= keys[-1]:
            return tail[str(int(keys[-1]))]
        for a, b in zip(keys, keys[1:]):
            if a <= tau <= b:
                f = (tau - a) / (b - a)
                lo, hi = tail[str(int(a))], tail[str(int(b))]
                return lo + f * (hi - lo)
        return tail[str(int(keys[-1]))]

    out = []
    comps = conn.execute(
        "SELECT id, end_time, field_size FROM competition WHERE end_time IS NOT NULL "
        "AND (SELECT COUNT(*) FROM snapshot s WHERE s.competition_id = competition.id "
        "AND s.note LIKE '%live feed%') >= 3").fetchall()
    for comp in comps:
        field = comp["field_size"] or 0
        if 0 < field <= 2 * CLOSED_MAX:       # a lobby is clocked on its games
            continue
        finals = {int(r["rank"]): float(r["points"]) for r in conn.execute(
            "SELECT rank, points FROM final_result WHERE competition_id = ?", (comp["id"],))
            if r["points"] and r["points"] > 0}
        if not finals:
            continue
        end = when(str(comp["end_time"]).replace(" ", "T") + "+00:00")
        if not end:
            continue
        reads = []
        for snap in conn.execute("SELECT id, ts FROM snapshot WHERE competition_id = ? AND "
                                 "note LIKE '%live feed%' ORDER BY ts", (comp["id"],)):
            stamp = when(str(snap["ts"]).replace(" ", "T") + "+00:00")
            points = {int(r["rank"]): float(r["points"]) for r in conn.execute(
                "SELECT rank, points FROM points WHERE snapshot_id = ?", (snap["id"],))}
            if stamp and points:
                reads.append(((stamp - end).total_seconds() / 60, points))
        for i, (tau, points) in enumerate(reads):
            if tau < -1:
                continue
            for rank, p in points.items():
                final = finals.get(rank)
                # Rank 1 is one team's exceptional evening, left out here as it
                # is left out of every other spread in this file.
                if not final or p <= 0 or rank == 1:
                    continue
                held = 0.0
                for earlier, older in reversed(reads[:i]):
                    was = older.get(rank)
                    if was is None:
                        continue
                    if abs(was - p) > 1e-9:
                        break
                    held = tau - earlier
                out.append((tau, held, final * at(tau) / p))
    return out


def feed_tail(rows: list) -> dict:
    """Half the p10–p90 spread of that error, as a share of its median — the
    same width `dispersion` reports — for a standing that is still moving and
    for one that has held. Empty when the feed has not tracked enough."""
    out = {}
    for label, sub in (("settling", [r for r in rows if r[1] < FEED_HELD]),
                       ("held", [r for r in rows if r[1] >= FEED_HELD])):
        errs = sorted(e for _, _, e in sub)
        if len(errs) < FEED_MIN_ROWS:
            continue
        mid = statistics.median(errs)
        lo = errs[int(0.1 * len(errs))]
        hi = errs[min(len(errs) - 1, int(0.9 * len(errs)))]
        out[label] = round((hi - lo) / 2 / mid, 3) if mid > 0 else 0.0
        out[label + "_rows"] = len(errs)
    if "settling" not in out or "held" not in out:
        return {}
    out["held_minutes"] = FEED_HELD
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=int, default=1, help="pages to read per board (100 rosters each)")
    parser.add_argument("--limit", type=int, help="stop after this many boards")
    parser.add_argument("--since", default="2026-01-01", help="oldest start date to replay")
    args = parser.parse_args()

    with db.session() as conn:
        comps = [dict(r) for r in conn.execute(
            "SELECT id, name, region, start_time, end_time, field_size, max_games, scoring, "
            "event_id, window_id, team_mode, game_mode, stage FROM competition WHERE source = 'osirion' "
            "AND notes LIKE '%scoring cumulative%' AND start_time >= ? AND field_size IS NOT NULL "
            "ORDER BY start_time DESC", (args.since,))]
    if args.limit:
        comps = comps[:args.limit]
    print(f"{len(comps)} boards with a verified scoring table since {args.since}")

    time_shares = {"open": collections.defaultdict(list), "closed": collections.defaultdict(list)}
    game_shares = collections.defaultdict(list)
    tail_shares = collections.defaultdict(list)
    count_shares = collections.defaultdict(list)     # (games in the lobby, rank, game k)
    counted = collections.Counter()
    # One row per board for `carry`, which needs the ranks of a board kept
    # together: pooling them into per-rank lists loses which board each came
    # from, and the pairing is the whole measurement.
    rho_rows = {"open": [], "closed": []}
    # One entry per open board for the tables by kind, newest first as the
    # query orders them: the family and category curves rest on the most
    # recent boards of each.
    cups = []
    HALF = 0.5
    for i, comp in enumerate(comps, 1):
        teams, disagree = sessions_of(comp, args.pages)
        if len(teams) < 15 or disagree > 0.1 * len(teams):
            counted["skipped"] += 1
            continue
        kind = "closed" if (comp["field_size"] or 0) <= CLOSED_MAX else "open"
        timed = by_time(comp, teams)
        for key, share in timed.items():
            time_shares[kind][key].append(share)
        if kind == "open":
            tailed = by_tail(comp, teams)
            for key, share in tailed.items():
                tail_shares[key].append(share)
            # A first round of at least half a lobby: a later round plays
            # for a handful of places and keeps no pace of its own.
            if len(teams) >= 50 and not str(comp.get("stage") or "").strip():
                cups.append({"start": str(comp["start_time"]), "name": category_key(comp["name"]),
                             "family": family_key(comp), "kind": kind_key(comp),
                             "curve": cup_curve(by_time(comp, teams, FAMILY_STEPS)),
                             "tail": cup_tail(tailed)})
        counted[kind] += 1
        played = {}
        if kind == "closed":
            gamed = by_game(teams)
            n = gamed.pop("games", 0)
            for (rank, k), share in gamed.items():
                game_shares[(rank, nearest_step(k))].append(share)
                if nearest_step(k) == HALF:
                    played[rank] = share
                if n:
                    count_shares[(n, rank, int(round(k * n)))].append(share)
        # rho at half the session, per rank, on the clock that format keeps
        source = played if kind == "closed" else {r: s for (r, tau), s in timed.items() if tau == HALF}
        half = CURVE_AT_HALF.get(kind)
        row = {rank: share / half for rank, share in source.items() if share > 0} if half else {}
        if len(row) >= 2:
            rho_rows[kind].append(row)
        if i % 200 == 0:
            print(f"  ... {i}/{len(comps)}", flush=True)

    print(f"\n{counted['open']} open queues and {counted['closed']} closed lobbies replayed, "
          f"{counted['skipped']} skipped (thin board or scoring disagreement)\n")
    for label, shares in (("open queue, by elapsed time", time_shares["open"]),
                          ("closed lobby, by elapsed time", time_shares["closed"]),
                          ("closed lobby, by games played", game_shares)):
        print(f"{label} — share of the final threshold (median over boards)")
        print("rank  " + "  ".join(f"{s:.1f}" for s in STEPS))
        for rank in RANKS:
            cells = []
            for step in STEPS:
                v = shares.get((rank, step), [])
                cells.append(f"{statistics.median(v):.2f}" if len(v) >= 3 else "  - ")
            print(f"{rank:>4}  " + "  ".join(f"{c:>4}" for c in cells))
        print("  linear:  " + "  ".join(f"{s:.2f}" for s in STEPS) + "\n")

    tail_curve = curve(tail_shares, TAIL, str)
    families, categories = kin_tables(cups)
    with db.session() as conn:
        rows = feed_rows(conn, tail_curve)
        evenings = tracked_evenings(conn)
    measured_feed = feed_tail(rows)
    # The feed's own reading of each cup it has followed enough, ahead of
    # the harvest's replay of it: the page takes the first row that matches.
    categories = feed_categories(evenings) + categories
    depth = depth_tables(evenings)
    pooled = {"curve": curve(time_shares["open"]), "dispersion": dispersion(time_shares["open"]),
              "tail": tail_curve, "tail_by_rank": tail_by_rank(tail_shares),
              "tail_dispersion": dispersion(tail_shares, TAIL, str),
              "tail_spread_by_rank": tail_by_rank(tail_shares, spread=True), "tail_feed": measured_feed}
    bands = live_bands(evenings, pooled, families, categories, depth)
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "note": "share of the final threshold reached at a point of the session; "
                "open queues on the wall clock, closed lobbies on games played",
        "boards": {"open": counted["open"], "closed": counted["closed"]},
        "curve": {"open_by_time": curve(time_shares["open"]),
                  "closed_by_game": curve(game_shares)},
        "carry": {"open_by_time": carry(rho_rows["open"]),
                  "closed_by_game": carry(rho_rows["closed"])},
        "dispersion": {"open_by_time": dispersion(time_shares["open"]),
                       "closed_by_game": dispersion(game_shares)},
        # Past the close, on the wall clock rather than on the window: the
        # games still in flight are a game long whatever the window was.
        "tail": {"open_by_time": tail_curve, "by_rank": tail_by_rank(tail_shares)},
        "tail_dispersion": {"open_by_time": dispersion(tail_shares, TAIL, str),
                            "by_rank": tail_by_rank(tail_shares, spread=True)},
        # How wrong that projection was on the boards the page itself reads -
        # the feed's, which lag the games. See `feed_tail`.
        "tail_feed": {"open_by_time": measured_feed} if measured_feed else {},
        # The pace of a cup's own kind - its recent editions, else its family -
        # read by the page in place of the pooled curve from FAMILY_FROM on.
        "families": families,
        "categories": categories,
        # How far ahead of, or behind, the top 25 a rank deeper into the
        # field runs, per band of rank / field and tenth, on the evenings
        # the feed followed.
        "depth": depth,
        # How wide a live answer has to be, in units of its own half-width,
        # by share of the session and band of rank, on those same evenings.
        "live_bands": bands,
        # A lobby's clock is its game count, and the common counts get their
        # own curve, read at "after game k of n" exactly.
        "games_curve": games_curve(count_shares),
        "games_dispersion": games_dispersion(count_shares),
        "open_by_time": summarise(time_shares["open"]),
        "closed_by_game": summarise(game_shares),
    }
    for kind in ("open_by_time", "closed_by_game"):
        print(f"{kind}: share  " + "  ".join(f"{k}:{v:.2f}" for k, v in payload["curve"][kind].items()))
        print(f"{' ' * len(kind)}  ±      " + "  ".join(f"{k}:{v:.2f}" for k, v in payload["dispersion"][kind].items()))
        got = payload["carry"][kind]
        if got:
            print(f"{' ' * len(kind)}  carry  {got['slope']:.2f} of a reading belongs to another rank "
                  f"(correlation {got['correlation']:+.2f} over {got['boards']} boards)")
    banded = payload["tail"]["by_rank"]
    if banded:
        print("\npast the close, rank by rank — share of the final threshold "
              "(the page reads the band of the rank asked)")
        print("  band        " + "  ".join(f"{m:>5}" for m in TAIL))
        for low, high, row, n in banded:
            label = f"{low}-{high}" if high else f"{low}+"
            print(f"  {label:<10}  " + "  ".join(
                f"{row[str(m)]:.3f}" if str(m) in row else "    -" for m in TAIL)
                  + f"   ({n} readings)")
        for low, high, row, n in payload["tail_dispersion"]["by_rank"]:
            label = f"{low}-{high}" if high else f"{low}+"
            print(f"  ± {label:<8}  " + "  ".join(
                f"{row[str(m)]:.3f}" if str(m) in row else "    -" for m in TAIL))
        print(f"  A band's minute needs {TAIL_BAND_MIN} readings; the deep bands only fill in when")
        print("  --pages read that far down the board (three pages reach rank 250, ten reach 1,000).")
    if families or categories:
        print(f"\nthe pace by kind of cup - share of the final threshold at the top 25, the most recent "
              f"{FAMILY_RECENT} boards of a family, {CAT_RECENT} editions of a cup:")
        print("  kind                                          n   0.3   0.5   0.7   0.9   1.0   +10")
        for row in families:
            label = f"{row[0]} {row[1]} {row[2]} min {row[3]} games"
            print(f"  {label:<44} {row[7]:>3}  " + "  ".join(f"{row[4].get(k, float('nan')):.2f}" for k in ("0.30", "0.50", "0.70", "0.90", "1.00"))
                  + f"  {row[5].get('10', float('nan')):.2f}")
        for row in categories:
            print(f"  {row[0][:44]:<44} {row[3]:>3}  " + "  ".join(f"{row[1].get(k, float('nan')):.2f}" for k in ("0.30", "0.50", "0.70", "0.90", "1.00"))
                  + f"  {row[2].get('10', float('nan')):.2f}   (latest {row[4]}" + (", as the feed read it)" if len(row) > 5 and row[5] == "feed" else ")"))
        print(f"  A family needs {FAMILY_MIN} boards, a category {CAT_MIN} editions replayed or {FEED_CAT_MIN} evenings followed;")
        print("  the page reads the cup's own row first, the feed's before the harvest's, then the family, then the pooled curve.")
    if depth:
        print(f"\nthe deep end against the top 25, on {len(evenings)} evenings the feed followed - "
              "the share of its final a band of q = rank / field has reached over the top 25's, by tenth:")
        print("  q band        " + "  ".join(f"{s:>5.1f}" for s in DEPTH_STEPS))
        for low, high, row, n in depth:
            print(f"  {low:.2f}-{high:<5.2f}  " + "  ".join(f"{row[f'{s:.1f}']:>5.3f}" if f"{s:.1f}" in row else "    -" for s in DEPTH_STEPS)
                  + f"   ({n} evenings)")
        print(f"  A tenth needs {DEPTH_MIN} evenings; the page reads the ratio from {DEPTH_FROM} on, and one above q {DEPTH_BANDS[0][0]}.")
    if bands:
        print("\nhow wide a live answer has to be, in units of its half-width (the cold multipliers are "
              "in quality.bands):")
        print("  ranks     bin       n     50 %              90 %")
        for low, high, table in bands:
            label = f"{low}-{high}" if high else f"{low}+"
            for bin_, q in table.items():
                print(f"  {label:<8}  {bin_:<7} {q['n']:>6}   {q['50'][0]:>+6.2f} {q['50'][1]:>+6.2f}      "
                      f"{q['90'][0]:>+6.2f} {q['90'][1]:>+6.2f}")
        print(f"  A bin needs {LIVE_BAND_MIN} readings. Where the range reaches further down than up, the")
        print("  extrapolation has landed high more often than low on the evenings followed so far.")
    if measured_feed:
        print(f"\ntail as the feed reads it, past the close ({len(rows)} readings of tracked evenings):")
        print(f"  a standing still moving is short of the final by ±{100 * measured_feed['settling']:.1f} % "
              f"({measured_feed['settling_rows']} readings)")
        print(f"  one unchanged for {FEED_HELD} min or more, by ±{100 * measured_feed['held']:.1f} % "
              f"({measured_feed['held_rows']} readings)")
        print("  The page quotes these past a window's close; `tail_dispersion` above is measured on")
        print("  the games' own end times, which say the board is final and certain twenty minutes in.")
    else:
        print(f"\ntail as the feed reads it: not enough tracked evenings yet ({len(rows)} readings, "
              f"{FEED_MIN_ROWS} needed on each side of {FEED_HELD} min) - the page keeps the games' clock.")
    with open(PACE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    print(f"Wrote {os.path.relpath(PACE_PATH)} — the export carries it into model.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
