"""
Season and competition identity for a single provider event (Epic 2B.1).

WHY THIS MODULE EXISTS
----------------------
Before this Epic, a fixture was treated as belonging to season S because its
kickoff fell inside a July-June window built from S:

    dates=20190701-20200630   ->   "everything here is the 2019/20 season"

That is a calendar assumption wearing the costume of a fact, and it is wrong in
both directions:

  * TRUNCATION. The COVID-extended 2019/20 season finished on 2020-07-26
    (eng.1), 2020-08-02 (ita.1) and 2020-08-04 (eng.2). Those matchdays fall
    OUTSIDE a July-June window, so the window silently deleted them - 66 of
    eng.1's 380 fixtures, 98 of ita.1's, 82 of eng.2's.

  * CONTAMINATION. The very same matches fall INSIDE the window built for the
    NEXT season, so a request for 2020/21 returned 66 fixtures from 2019/20 -
    including three clubs (Bournemouth, Norwich, Watford) that had been
    relegated and never played a minute of 2020/21.

Widening the window cannot fix this. The two seasons OVERLAP in calendar space,
so no boundary date separates them; and ESPN refuses a `dates=` range longer
than 366 days with HTTP 400 (measured: 20190701-20200630 -> 200, and the same
range plus one day -> 400). The fix has to come from somewhere other than dates.

THE RULE
--------
A fixture belongs to a season because the PROVIDER SAYS SO at the event level.
Dates may DISCOVER candidates; only metadata may ADMIT them. This module is the
single place that answers:

    "does this event belong to the requested competition-season?"

It is deliberately provider-independent: it takes an already-extracted
`SeasonIdentity`, never a JSON blob, so a second provider is a second extractor
and not a second copy of these rules.

WHAT THE EVIDENCE SUPPORTS
--------------------------
Measured over 53,934 events in 140 cached league-seasons (Epic 2A corpus, seven
leagues, 2006-2025):

  * `season.year` is present on 100% of events (0 missing). It is the only
    field that is both universal and season-specific.
  * `season.year` alone is NOT sufficient. In eng.1's 2009 window, 380 events
    carry `season.year=2009` alongside `season.slug='2013-2014-...'`. Spot
    checks against real results show that block is corrupt: it repeats the
    2009-10 fixture list with WRONG SCORES (Chelsea 0-1 Hull for a match that
    really finished 2-1). Trusting `season.year` alone would have imported 380
    fabricated results.
  * `season.slug` corroborates where it encodes a season (45,657 events agree,
    380 disagree - exactly the corrupt block above) but frequently encodes a
    PHASE instead ('regular-season', 'group-stage', 'semi-finals'), so it
    cannot be required.

Hence the rule below: `season.year` decides, `season.slug` may VETO, and
anything unverifiable is refused rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

__all__ = [
    "SeasonIdentity",
    "SeasonVerdict",
    "season_year_from_label",
    "classify_event_season",
]


class SeasonVerdict(str, Enum):
    """
    Outcome of the one membership question, kept distinct on purpose.

    "Not this season", "not this competition" and "I cannot tell" are three
    different facts about the data. Collapsing them into a boolean would throw
    away the only signal that distinguishes a clean season boundary from a
    provider whose metadata has gone missing - and the second case is the one
    that must be investigated rather than absorbed.
    """

    ACCEPTED = "ACCEPTED"
    WRONG_SEASON = "WRONG_SEASON"
    WRONG_COMPETITION = "WRONG_COMPETITION"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True)
class SeasonIdentity:
    """
    What a provider states about one event's competition-season.

    Every field is Optional because absence is a real, reportable state. A
    missing `season_year` is not "probably the season we asked for"; it is a
    fact we do not have, and `classify_event_season` refuses on it.

    `phase` is carried for PROVENANCE ONLY. It is never used to admit or reject
    a match here - see the note on phase filtering in `classify_event_season`.
    """

    competition: Optional[str] = None
    season_year: Optional[int] = None
    season_label: Optional[str] = None
    phase: Optional[str] = None


# Season prefixes actually observed in ESPN labels, most specific first:
#   2009-2010-barclays-premier-league       YYYY-YYYY   (scoreboard slug)
#   2019-20-english-premier-league          YYYY-YY     (scoreboard slug)
#   20062007-english-league-championship    YYYYYYYY    (scoreboard slug)
#   201819-german-2-bundesliga              YYYYYY      (scoreboard slug)
#   2019-20 English Premier League          YYYY-YY     (schedule displayName)
#
# The delimiter class includes a space because the schedule endpoint sends a
# display name rather than a slug. Without it the veto would quietly stop
# working on that endpoint - the label would parse as "no opinion" and
# `season.year` would go unchallenged, which is the one thing the Epic 2A
# corpus proves is not safe.
#
# Anything else ('regular-season', 'group-stage', 'final') encodes no season and
# is treated as "no opinion", never as a disagreement.
_LABEL_SEASON_PATTERNS = (
    re.compile(r"^(\d{4})-(\d{4})(?:[-\s]|$)"),
    re.compile(r"^(\d{4})-(\d{2})(?:[-\s]|$)"),
    re.compile(r"^(\d{4})(\d{4})(?:[-\s]|$)"),
    re.compile(r"^(\d{4})(\d{2})(?:[-\s]|$)"),
)


def season_year_from_label(label: Optional[str]) -> Optional[int]:
    """
    The season a slug/label encodes, or None if it encodes none.

    None means "this label says nothing about the season" - the overwhelmingly
    common case for phase slugs - and callers must read it that way rather than
    as a contradiction. Returning a guess here would defeat the whole point of
    the module.
    """
    if not isinstance(label, str):
        return None
    text = label.strip().lower()
    if not text:
        return None

    for pattern in _LABEL_SEASON_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        start = int(match.group(1))
        # A plausible football season, not a street number in a club name.
        if 1850 <= start <= 2200:
            return start
        return None
    return None


def classify_event_season(
    identity: SeasonIdentity,
    *,
    expected_competition: Optional[str],
    requested_season: int,
) -> SeasonVerdict:
    """
    Decide whether one event belongs to the requested competition-season.

    THE ONE CHOKEPOINT. Every provider path funnels through here, so season
    semantics cannot drift between endpoints, between callers, or between
    production and research tooling.

    Order matters, and it is deliberate:

    1. COMPETITION FIRST, and independently of season. A correct season does not
       make an event the right competition: Epic 2A found postseason and
       playoff fixtures sitting inside league responses under the same
       `season.year` as the league programme. These are separate invariants and
       are checked separately (an event can be right-season/wrong-competition).

    2. SEASON MUST BE STATED. No `season_year`, no membership. Not inferred
       from the kickoff, not defaulted to `requested_season`, not guessed from
       the other competitor's fixtures. This is the fail-closed core of the
       Epic: the previous implementation's mistake was precisely that it had an
       answer for events that never declared one.

    3. LABEL MAY VETO, NEVER VOUCH. Where the label encodes a season and
       CONTRADICTS `season.year`, the event is UNVERIFIABLE - two parts of the
       same payload disagree and there is no principled way to pick a winner.
       Measured on the Epic 2A corpus this rejects exactly one block: eng.1's
       380 duplicated 2009-10 fixtures carrying a 2013-2014 slug and scores
       that match neither season. Where the label encodes no season it is
       silent, which is the norm.

    4. ONLY THEN the year comparison.

    PHASE IS NOT FILTERED HERE. Playoff, promotion and relegation fixtures are
    admitted exactly as they were before this Epic, because excluding them is a
    MODELLING policy, not a parsing fact, and this Epic is forbidden from
    inventing one silently. The evidence that it must not be improvised: 303
    ordinary ger.1 2010-11 Bundesliga fixtures carry `slug='group-stage'`, so
    the obvious "drop anything that is not regular-season" rule would delete a
    full legitimate season. `SeasonIdentity.phase` is preserved so the decision
    can be made later, deliberately, with the data visible.
    """
    stated_competition = (identity.competition or "").strip().lower()
    expected = (expected_competition or "").strip().lower()

    if expected:
        if not stated_competition:
            # The provider did not say what competition this is. "It came back
            # from the eng.1 URL" is the same class of reasoning as "it fell in
            # the window" - circumstantial, not stated.
            return SeasonVerdict.UNVERIFIABLE
        if stated_competition != expected:
            return SeasonVerdict.WRONG_COMPETITION

    season_year = identity.season_year
    if not isinstance(season_year, int) or isinstance(season_year, bool):
        return SeasonVerdict.UNVERIFIABLE

    label_season = season_year_from_label(identity.season_label)
    if label_season is not None and label_season != season_year:
        return SeasonVerdict.UNVERIFIABLE

    if season_year != requested_season:
        return SeasonVerdict.WRONG_SEASON

    return SeasonVerdict.ACCEPTED
