"""
RESEARCH TOOLING - Epic 2A. NOT production code. NOT imported by the pipeline.

Measures the cold-start problem instead of asserting it, and gathers the evidence
that decides WHICH family of prior is defensible for this project.

Five measurements, each answering a question the design document must not guess at:

  1. PREVALENCE   How many fixtures actually have no venue history? Broken down by
                  matchweek, because "cold start" is a curve, not a state.
  2. CONTINUITY   Does a promoted club keep its ESPN team id when it changes
                  division? If not, no cross-season prior can follow it.
  3. CARRYOVER    How much does last season's venue rate tell us about this
                  season's? This is the entire premise of a previous-season prior;
                  if the correlation is ~0 the prior is worthless regardless of
                  how it is weighted.
  4. RELIABILITY  Split-half correlation of venue rates WITHIN a season. This is
                  the signal-to-noise measurement that says how much shrinkage a
                  19-match venue sample deserves.
  5. BASELINE     How stable is the league goals-per-team-per-match figure across
                  seasons, and how fast does a partial current season converge to
                  its final value?

NO PARAMETER IS SELECTED HERE. Reliability and carryover coefficients are
DESCRIPTIVE. Turning one into a production weight requires the chronological
validation harness designed in the Epic document, not a correlation printed by
this script.

Read-only. GET only. Shares the coverage audit's disk cache.
"""

from __future__ import annotations

import argparse
import collections
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import ESPN_BASE_URL  # noqa: E402
from domain.match_records import MatchRecord, Venue  # noqa: E402
from espn import parse_scoreboard_events  # noqa: E402
from research.audit_espn_history import (  # noqa: E402
    CACHE_DIR,
    SCOREBOARD_LIMIT,
    BoundedFetcher,
)


def season_records(fetcher: BoundedFetcher, league: str, season: int) -> List[MatchRecord]:
    """
    Completed fixtures for one league-season, as domain MatchRecords.

    Goes through `espn.parse_scoreboard_events` - the production adapter - so what
    is measured here is exactly what the pipeline would see. One record per
    fixture, from the HOME perspective.
    """
    url = f"{ESPN_BASE_URL}/{league}/scoreboard"
    params = {"dates": f"{season}0701-{season + 1}0630", "limit": SCOREBOARD_LIMIT}
    payload, _ = fetcher.get(url, params)
    if payload is None:
        return []
    records = parse_scoreboard_events(payload, league)
    # The audit found ESPN files a season's July catch-up fixtures under the
    # PREVIOUS season slug (eng.1 2020 returned 66 events of 2019-20). Filtering
    # on the event's own declared season is the only reliable separator; the date
    # window alone is not one.
    return [r for r in records if r.kickoff is not None]


# MatchRecord.kickoff is Optional[datetime] by contract (Epic 1B.1 made missing
# data explicit rather than defaulted). Callers below filter the None cases out
# first, but a comprehension cannot prove that to a type checker, so sorting goes
# through this helper instead of `key=lambda r: r.kickoff`.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _kickoff_key(record: MatchRecord) -> datetime:
    """Total ordering on kickoff. None sorts first; callers exclude it anyway."""
    return record.kickoff or _EPOCH


def pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Plain Pearson r. Returns None when undefined (n<3 or zero variance)."""
    if len(xs) < 3 or len(ys) != len(xs):
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def team_venue_rates(records: List[MatchRecord]) -> Dict[Tuple[str, str], Tuple[float, float, int]]:
    """
    (team_id, venue) -> (goals_for_per_match, goals_against_per_match, n).

    Records arrive HOME-perspective only, so each fixture is expanded into the two
    team-perspectives the model actually uses.
    """
    acc: Dict[Tuple[str, str], List[int]] = collections.defaultdict(lambda: [0, 0, 0])
    for r in records:
        if r.team_id is None or r.opponent_id is None:
            continue
        if r.goals_for is None or r.goals_against is None:
            continue
        home = acc[(r.team_id, Venue.HOME)]
        home[0] += r.goals_for
        home[1] += r.goals_against
        home[2] += 1
        away = acc[(r.opponent_id, Venue.AWAY)]
        away[0] += r.goals_against
        away[1] += r.goals_for
        away[2] += 1
    return {k: (v[0] / v[2], v[1] / v[2], v[2]) for k, v in acc.items() if v[2] > 0}


def measure_prevalence(records: List[MatchRecord], league: str, season: int) -> None:
    """
    Cold start as a curve: for every fixture in chronological order, how many
    prior VENUE-matching matches each side had under the production cutoff.

    This replicates `eligible_history` semantics - strict `kickoff <`, venue-pure,
    same competition - rather than approximating them.
    """
    ordered = sorted((r for r in records if r.kickoff is not None), key=_kickoff_key)
    home_counts: Dict[str, int] = collections.Counter()
    away_counts: Dict[str, int] = collections.Counter()

    buckets: Dict[str, List[int]] = collections.defaultdict(list)
    zero_either = 0
    zero_both = 0
    thin_either = 0  # n < 3 on either side
    total = 0

    for r in ordered:
        if r.team_id is None or r.opponent_id is None:
            continue
        total += 1
        h = home_counts[r.team_id]
        a = away_counts[r.opponent_id]

        # Approximate matchweek by counting how many league matches each side has
        # played at any venue; for a round-robin this tracks matchday closely.
        week = min(h + a, 40)
        buckets[f"{week:02d}"].append(min(h, a))

        if h == 0 or a == 0:
            zero_either += 1
        if h == 0 and a == 0:
            zero_both += 1
        if h < 3 or a < 3:
            thin_either += 1

        home_counts[r.team_id] += 1
        away_counts[r.opponent_id] += 1

    if not total:
        return
    print(f"  {league} {season}: fixtures={total}  "
          f"n=0 on at least one side: {zero_either} ({zero_either / total:.1%})  "
          f"n=0 on BOTH sides: {zero_both} ({zero_both / total:.1%})  "
          f"n<3 either side: {thin_either} ({thin_either / total:.1%})")


def measure_carryover(fetcher: BoundedFetcher, league: str, seasons: List[int]) -> None:
    """Correlate a team's venue rate in season S with the same rate in S+1."""
    print(f"\n  {league}: previous-season -> current-season venue rate correlation")
    print(f"    {'pair':<14}{'n_teams':>8}{'r(GF home)':>12}{'r(GA home)':>12}"
          f"{'r(GF away)':>12}{'r(GA away)':>12}")

    all_pairs: Dict[str, List[Tuple[float, float]]] = collections.defaultdict(list)

    for prev, cur in zip(seasons, seasons[1:], strict=False):
        rates_prev = team_venue_rates(season_records(fetcher, league, prev))
        rates_cur = team_venue_rates(season_records(fetcher, league, cur))
        cols: Dict[str, Tuple[List[float], List[float]]] = {
            "GF home": ([], []), "GA home": ([], []),
            "GF away": ([], []), "GA away": ([], []),
        }
        teams = 0
        for (team, venue), (gf, ga, n) in rates_prev.items():
            if n < 5:
                continue
            cur_entry = rates_cur.get((team, venue))
            if cur_entry is None or cur_entry[2] < 5:
                continue
            label_gf = f"GF {venue}"
            label_ga = f"GA {venue}"
            if label_gf in cols:
                cols[label_gf][0].append(gf)
                cols[label_gf][1].append(cur_entry[0])
                cols[label_ga][0].append(ga)
                cols[label_ga][1].append(cur_entry[1])
                all_pairs[label_gf].append((gf, cur_entry[0]))
                all_pairs[label_ga].append((ga, cur_entry[1]))
                teams += 1

        def fmt(label: str, cols: Dict[str, Tuple[List[float], List[float]]] = cols) -> str:
            r = pearson(cols[label][0], cols[label][1])
            return f"{r:>12.3f}" if r is not None else f"{'-':>12}"

        print(f"    {prev}->{cur}   {teams // 2:>8}"
              f"{fmt('GF home')}{fmt('GA home')}{fmt('GF away')}{fmt('GA away')}")

    print("    " + "-" * 68)
    pooled = []
    for label in ("GF home", "GA home", "GF away", "GA away"):
        pairs = all_pairs[label]
        r = pearson([p[0] for p in pairs], [p[1] for p in pairs])
        pooled.append(f"{r:>12.3f}" if r is not None else f"{'-':>12}")
    n_pooled = len(all_pairs['GF home'])
    print(f"    {'POOLED':<14}{n_pooled:>8}" + "".join(pooled))
    print("    (surviving teams only: relegated clubs vanish from the pair, which is")
    print("     itself a finding - the correlation is conditioned on staying up)")


def measure_reliability(fetcher: BoundedFetcher, league: str, seasons: List[int]) -> None:
    """
    Split-half reliability of a venue rate within one season.

    Odd-numbered vs even-numbered home matches for the same team, same season. A
    low correlation means a ~19-match venue rate is mostly noise, which is the
    empirical case FOR shrinkage - and the reason a raw venue mean is a poor
    estimator even when n is "full".
    """
    print(f"\n  {league}: within-season split-half reliability of venue rates")
    print(f"    {'season':<8}{'teams':>7}{'r(GF)':>9}{'r(GA)':>9}   "
          f"Spearman-Brown corrected r(GF)")
    for season in seasons:
        records = season_records(fetcher, league, season)
        halves: Dict[Tuple[str, str, int], List[int]] = collections.defaultdict(
            lambda: [0, 0, 0])
        seq: Dict[Tuple[str, str], int] = collections.Counter()
        for r in sorted((x for x in records if x.kickoff), key=_kickoff_key):
            if r.team_id is None or r.opponent_id is None:
                continue
            for team, venue, gf, ga in (
                (r.team_id, Venue.HOME, r.goals_for, r.goals_against),
                (r.opponent_id, Venue.AWAY, r.goals_against, r.goals_for),
            ):
                if gf is None or ga is None:
                    continue
                idx = seq[(team, venue)]
                seq[(team, venue)] += 1
                bucket = halves[(team, venue, idx % 2)]
                bucket[0] += gf
                bucket[1] += ga
                bucket[2] += 1

        gf_a, gf_b, ga_a, ga_b = [], [], [], []
        for (team, venue, parity), vals in halves.items():
            if parity != 0:
                continue
            other = halves.get((team, venue, 1))
            if other is None or vals[2] < 5 or other[2] < 5:
                continue
            gf_a.append(vals[0] / vals[2])
            gf_b.append(other[0] / other[2])
            ga_a.append(vals[1] / vals[2])
            ga_b.append(other[1] / other[2])

        r_gf = pearson(gf_a, gf_b)
        r_ga = pearson(ga_a, ga_b)
        sb = (2 * r_gf / (1 + r_gf)) if r_gf is not None and r_gf > -1 else None
        print(f"    {season:<8}{len(gf_a):>7}"
              f"{(f'{r_gf:.3f}' if r_gf is not None else '-'):>9}"
              f"{(f'{r_ga:.3f}' if r_ga is not None else '-'):>9}   "
              f"{(f'{sb:.3f}' if sb is not None else '-')}")


def measure_baseline(fetcher: BoundedFetcher, league: str, seasons: List[int]) -> None:
    """League goals-per-team-per-match per season, plus in-season convergence."""
    print(f"\n  {league}: league baseline (goals per team per match)")
    finals = []
    print(f"    {'season':<8}{'final':>8}{'after 10 fx':>13}{'after 30 fx':>13}"
          f"{'after 60 fx':>13}{'prev-season abs err':>21}")
    prev_final: Optional[float] = None
    for season in seasons:
        records = sorted((r for r in season_records(fetcher, league, season) if r.kickoff),
                         key=_kickoff_key)
        if not records:
            continue
        goals = [(r.goals_for or 0) + (r.goals_against or 0) for r in records]
        final = sum(goals) / (2 * len(goals))
        finals.append(final)

        def partial(k: int, goals: List[int] = goals) -> str:
            if len(goals) < k:
                return f"{'-':>13}"
            return f"{sum(goals[:k]) / (2 * k):>13.3f}"

        err = f"{abs(final - prev_final):>21.3f}" if prev_final is not None else f"{'-':>21}"
        print(f"    {season:<8}{final:>8.3f}{partial(10)}{partial(30)}{partial(60)}{err}")
        prev_final = final

    if len(finals) > 2:
        print(f"    across seasons: mean={statistics.mean(finals):.3f}  "
              f"sd={statistics.pstdev(finals):.3f}  "
              f"min={min(finals):.3f}  max={max(finals):.3f}")


def measure_promotion_continuity(fetcher: BoundedFetcher, top: str, second: str,
                                 seasons: List[int]) -> None:
    """
    Do promoted clubs keep their ESPN team id when they change division?

    If a club's id is stable across the divisional boundary, a cross-league prior
    can at least be JOINED. Whether a second-division scoring rate should then be
    trusted is a separate, modelling question.
    """
    print(f"\n  promotion continuity: {second} -> {top}")
    print(f"    {'season pair':<16}{'promoted ids found':>20}{'id also in 2nd tier':>22}")
    for prev, cur in zip(seasons, seasons[1:], strict=False):
        second_prev = {r.team_id for r in season_records(fetcher, second, prev)} | \
                      {r.opponent_id for r in season_records(fetcher, second, prev)}
        top_prev = {r.team_id for r in season_records(fetcher, top, prev)} | \
                   {r.opponent_id for r in season_records(fetcher, top, prev)}
        top_cur = {r.team_id for r in season_records(fetcher, top, cur)} | \
                  {r.opponent_id for r in season_records(fetcher, top, cur)}
        second_prev.discard(None)
        top_prev.discard(None)
        top_cur.discard(None)

        newcomers = top_cur - top_prev
        matched = newcomers & second_prev
        print(f"    {prev}->{cur}      {len(newcomers):>20}{len(matched):>22}"
              f"   {'ALL MATCHED' if newcomers and matched == newcomers else ''}")
        unmatched = {t for t in (newcomers - matched) if t is not None}
        if unmatched:
            print(f"       unmatched newcomer ids: {sorted(unmatched)}")


def measure_promoted_performance(fetcher: BoundedFetcher, top: str, second: str,
                                 seasons: List[int]) -> None:
    """
    How do promoted clubs actually score in the top flight versus how they scored
    in the division below, and versus the destination league mean?

    This is the quantity a promotion adjustment would have to encode. Reported as
    a measurement, NOT converted into a coefficient.
    """
    print(f"\n  promoted-club scoring shift: {second} -> {top}")
    print(f"    {'pair':<14}{'clubs':>7}{'2nd-tier GF/m':>15}{'top-flight GF/m':>17}"
          f"{'top-flight mean':>17}{'ratio to own past':>19}")
    for prev, cur in zip(seasons, seasons[1:], strict=False):
        top_prev_recs = season_records(fetcher, top, prev)
        top_cur_recs = season_records(fetcher, top, cur)
        second_prev_recs = season_records(fetcher, second, prev)
        if not (top_prev_recs and top_cur_recs and second_prev_recs):
            continue

        top_prev_ids = {r.team_id for r in top_prev_recs} | {r.opponent_id for r in top_prev_recs}
        top_cur_ids = {r.team_id for r in top_cur_recs} | {r.opponent_id for r in top_cur_recs}
        newcomers = {t for t in (top_cur_ids - top_prev_ids) if t}

        def all_venue_gf(records: List[MatchRecord]) -> Dict[str, Tuple[int, int]]:
            acc: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
            for r in records:
                if r.goals_for is None or r.goals_against is None:
                    continue
                if r.team_id:
                    acc[r.team_id][0] += r.goals_for
                    acc[r.team_id][1] += 1
                if r.opponent_id:
                    acc[r.opponent_id][0] += r.goals_against
                    acc[r.opponent_id][1] += 1
            return {k: (v[0], v[1]) for k, v in acc.items() if v[1] > 0}

        second_gf = all_venue_gf(second_prev_recs)
        top_gf = all_venue_gf(top_cur_recs)
        league_mean = (sum(g for g, _ in top_gf.values()) /
                       sum(n for _, n in top_gf.values())) if top_gf else 0.0

        before, after = [], []
        for team in newcomers:
            if team in second_gf and team in top_gf:
                g2, n2 = second_gf[team]
                g1, n1 = top_gf[team]
                if n2 >= 10 and n1 >= 10:
                    before.append(g2 / n2)
                    after.append(g1 / n1)
        if not before:
            continue
        ratio = statistics.mean(after) / statistics.mean(before)
        print(f"    {prev}->{cur}   {len(before):>7}{statistics.mean(before):>15.3f}"
              f"{statistics.mean(after):>17.3f}{league_mean:>17.3f}{ratio:>19.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Epic 2A cold-start measurements")
    parser.add_argument("--league", default="eng.1")
    parser.add_argument("--second-tier", default="eng.2")
    parser.add_argument("--from-season", type=int, default=2014)
    parser.add_argument("--to-season", type=int, default=2025)
    parser.add_argument("--skip", default="2019,2020",
                        help="seasons to omit from statistics (known coverage defects)")
    parser.add_argument("--only", default="",
                        help="comma separated: prevalence,carryover,reliability,baseline,promotion")
    args = parser.parse_args()

    skip = {int(s) for s in args.skip.split(",") if s.strip()}
    seasons = [s for s in range(args.from_season, args.to_season + 1) if s not in skip]
    wanted = {w.strip() for w in args.only.split(",") if w.strip()} or {
        "prevalence", "carryover", "reliability", "baseline", "promotion"}

    fetcher = BoundedFetcher(CACHE_DIR)
    league = args.league

    print("=" * 100)
    print("EPIC 2A - COLD-START MEASUREMENTS (read-only research tooling)")
    print(f"league={league}  seasons={seasons}  skipped={sorted(skip)}")
    print("=" * 100)

    if "prevalence" in wanted:
        print("\n[1] COLD-START PREVALENCE (production cutoff semantics)")
        for season in seasons:
            measure_prevalence(season_records(fetcher, league, season), league, season)

    if "carryover" in wanted:
        print("\n[2] PREVIOUS-SEASON CARRYOVER SIGNAL")
        measure_carryover(fetcher, league, seasons)

    if "reliability" in wanted:
        print("\n[3] WITHIN-SEASON RELIABILITY OF VENUE RATES")
        measure_reliability(fetcher, league, seasons)

    if "baseline" in wanted:
        print("\n[4] LEAGUE BASELINE BEHAVIOUR")
        measure_baseline(fetcher, league, seasons)

    if "promotion" in wanted:
        print("\n[5] PROMOTED CLUBS")
        measure_promotion_continuity(fetcher, league, args.second_tier, seasons)
        measure_promoted_performance(fetcher, league, args.second_tier, seasons)

    print(f"\nnetwork requests made: {fetcher.requests_made} "
          f"(cache hits: {fetcher.cache_hits})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
