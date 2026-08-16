"""
Epic 2E - the shot-statistics sidecar. RESEARCH ONLY.

WHAT THIS IS. A reader for per-match shot statistics that are ALREADY PRESENT in
the Epic 2A cache and were never parsed by anything. Production's `espn.py`
contains no reference to shots, possession or corners; this module does not
change that. It is a sidecar keyed by `event_id`, so `HistoricalMatch` and its
Epic 2B.2 schema version stay exactly as they are.

WHY A SIDECAR RATHER THAN A NEW FIELD ON HistoricalMatch. That contract is a
record of what the provider said about a RESULT, is consumed by production code
paths, and carries a schema version that other Epics' artifacts are keyed by.
Widening it to carry an unproven research feature would force a schema bump on
production for the sake of an experiment that may be abandoned this week. A
dictionary keyed by `event_id` joins onto the dataset at the point of use and
costs the rest of the system nothing.

    HistoricalMatch  -->  what happened (production contract, untouched)
    ShotProfile      -->  how it happened (research sidecar, this module)

THE BANNED FIELDS, AND WHY THIS MODULE IS STRUCTURED THE WAY IT IS.
`competitor.form` is CONTAMINATED. Measured, not assumed: a fra.1 fixture played
2025-08-15, on the opening weekend, carries `form='LWLWW'` - five results that
did not exist yet - while `records` correctly reads `1-0-0`. ESPN populates
`form` as of RETRIEVAL, which for this cache is 2026-08-09, i.e. after every
season in it finished. Reading it would import the answer.

Being careful is not a control. So the ban is STRUCTURAL: extraction never sees
a competitor dictionary. It sees `_permitted_view`, which copies an ALLOWLIST of
keys and drops everything else. A future edit that tries to read `form` finds
nothing there to read, and `tests/regression/test_epic2e_protocol.py` proves it
by rewriting `form` to absurd values and asserting every output is bit-identical.

MISSING IS NOT ZERO (GG-001). A completed match in which a team had 0.0%
possession is physically impossible, so `possessionPct == 0` is the
missing-as-zero signature, not an observation. Such a block is UNAVAILABLE and
the fixture is REFUSED. Availability is deliberately decided WITHOUT looking at
`totalGoals`: making availability depend on the outcome would be a subtle
selection leak, so the outcome-agreement check lives in `agreement_diagnostic`
and is reported, never used as a filter.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import espn  # noqa: E402
from domain.historical import HistoricalMatch  # noqa: E402

CACHE_DIR = REPO_ROOT / "research" / ".cache"

#: Bumped if the extracted shape changes. Separate from the 2B.2 schema version
#: because this sidecar is research-only and must never force a production bump.
SIDECAR_VERSION = "2e.1"

#: The ONLY competitor keys extraction may see. Anything absent from this set is
#: unreachable by construction, which is the point - see the module docstring.
#: `form` and `records` are deliberately, and permanently, not here.
PERMITTED_COMPETITOR_KEYS = frozenset({"id", "homeAway", "score", "statistics"})

#: Named so the ban is greppable and testable rather than merely intended.
BANNED_COMPETITOR_KEYS = frozenset({"form", "records"})

#: The ONLY statistic names extraction may keep. `totalGoals` is admitted for
#: one purpose - validating that the block describes THIS match rather than a
#: season aggregate - and is never exposed as a feature.
PERMITTED_STAT_NAMES = frozenset(
    {
        "totalShots",
        "shotsOnTarget",
        "possessionPct",
        "wonCorners",
        "shotAssists",
        "totalGoals",
    }
)


def _permitted_view(competitor: Mapping[str, Any]) -> Dict[str, Any]:
    """
    An allowlisted copy of a competitor dictionary.

    This is the leakage control, expressed as code rather than as a comment. The
    contaminated fields are not filtered out downstream, and are not "avoided by
    convention": they are absent from the object every extraction function
    receives, so there is nothing to accidentally read.
    """
    return {key: competitor.get(key) for key in PERMITTED_COMPETITOR_KEYS}


@dataclass(frozen=True)
class TeamShotLine:
    """
    One team's shot line in one match.

    `available` is a first-class field rather than the caller's problem, for the
    same reason `HistoricalMatch.completed` is: "no statistics were published"
    and "the team took zero shots" are different facts and only one of them is
    a measurement. Every numeric field is Optional so an unavailable line cannot
    silently present itself as a row of zeroes.
    """

    team_id: str
    is_home: bool
    available: bool
    shots: Optional[int] = None
    shots_on_target: Optional[int] = None
    possession_pct: Optional[float] = None
    corners: Optional[int] = None
    shot_assists: Optional[int] = None
    #: Kept for the per-match validation diagnostic ONLY. Never a feature.
    stated_goals: Optional[int] = None
    stated_score: Optional[int] = None

    def __post_init__(self) -> None:
        if self.available:
            if self.shots is None or self.shots_on_target is None:
                raise ValueError(
                    "an AVAILABLE shot line must carry shots and shots_on_target; "
                    f"got shots={self.shots!r} sot={self.shots_on_target!r}"
                )
            if self.shots_on_target > self.shots:
                raise ValueError(
                    "shots_on_target cannot exceed shots "
                    f"({self.shots_on_target} > {self.shots})"
                )


@dataclass(frozen=True)
class ShotProfile:
    """Both teams' shot lines for one fixture, keyed by the fixture's event id."""

    event_id: str
    competition: str
    season: int
    home: TeamShotLine
    away: TeamShotLine

    @property
    def available(self) -> bool:
        """
        Usable only when BOTH sides published statistics.

        One-sided availability is refused rather than half-used: a shot
        differential computed against a missing opponent is not a weaker
        measurement, it is a different quantity.
        """
        return self.home.available and self.away.available


def _stat_map(view: Mapping[str, Any]) -> Dict[str, str]:
    """Allowlisted `name -> displayValue` for one competitor."""
    out: Dict[str, str] = {}
    for entry in view.get("statistics") or []:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if name in PERMITTED_STAT_NAMES:
            out[str(name)] = entry.get("displayValue")
    return out


def _as_int(raw: Any) -> Optional[int]:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _as_float(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _team_line(competitor: Mapping[str, Any]) -> Optional[TeamShotLine]:
    """
    One competitor's shot line, or None when the competitor is unidentifiable.

    Availability turns on `possessionPct > 0` alone. A played match with 0.0%
    possession does not exist, so a zero there is the provider presenting
    "missing" as a number (GG-001). The outcome is NOT consulted: see the module
    docstring on why availability must not depend on the result.
    """
    view = _permitted_view(competitor)
    team_id = view.get("id")
    if team_id is None:
        return None
    stats = _stat_map(view)

    possession = _as_float(stats.get("possessionPct"))
    shots = _as_int(stats.get("totalShots"))
    on_target = _as_int(stats.get("shotsOnTarget"))

    available = (
        possession is not None
        and possession > 0.0
        and shots is not None
        and on_target is not None
        and on_target <= shots
    )

    is_home = view.get("homeAway") == "home"
    if not available:
        return TeamShotLine(
            team_id=str(team_id),
            is_home=is_home,
            available=False,
            stated_goals=_as_int(stats.get("totalGoals")),
            stated_score=_as_int(view.get("score")),
        )

    return TeamShotLine(
        team_id=str(team_id),
        is_home=is_home,
        available=True,
        shots=shots,
        shots_on_target=on_target,
        possession_pct=possession,
        corners=_as_int(stats.get("wonCorners")),
        shot_assists=_as_int(stats.get("shotAssists")),
        stated_goals=_as_int(stats.get("totalGoals")),
        stated_score=_as_int(view.get("score")),
    )


def profile_from_event(
    event: Mapping[str, Any],
    competition: str,
) -> Optional[ShotProfile]:
    """
    Extract one fixture's shot profile from a raw ESPN event.

    Season is taken from the payload's OWN season block, never from the window
    that was requested, for the same reason `domain.season_identity` insists on
    it: the request is not evidence about what the provider believes.
    """
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competition_block = competitions[0]
    event_id = event.get("id") or competition_block.get("id")
    if event_id is None:
        return None
    season = ((event.get("season") or {}) or {}).get("year")
    if not isinstance(season, int):
        return None

    home: Optional[TeamShotLine] = None
    away: Optional[TeamShotLine] = None
    for competitor in competition_block.get("competitors") or []:
        if not isinstance(competitor, Mapping):
            continue
        line = _team_line(competitor)
        if line is None:
            continue
        if line.is_home:
            home = line
        else:
            away = line
    if home is None or away is None:
        return None

    return ShotProfile(
        event_id=str(event_id),
        competition=competition,
        season=season,
        home=home,
        away=away,
    )


def cache_path(url: str, params: Mapping[str, Any]) -> Path:
    """Identical keying to the Epic 2A audit, so its payloads are readable here."""
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"


def load_shot_profiles(
    league: str,
    season: int,
) -> Tuple[Dict[str, ShotProfile], List[str]]:
    """
    Every shot profile for one league-season, from cache only. Never the network.

    The same `_season_discovery_windows` and `limit` as `epic2c_experiment`'s
    loader, because a different key silently misses every file and reports a cold
    cache the project in fact has. Deduplicated by `event_id`, exactly as
    `load_season` does, so the sidecar and the dataset agree fixture for fixture.
    """
    profiles: Dict[str, ShotProfile] = {}
    missing: List[str] = []
    for window in espn._season_discovery_windows(season):
        url = f"{espn.ESPN_BASE_URL}/{league}/scoreboard"
        path = cache_path(url, {"dates": window, "limit": 1000})
        if not path.exists():
            missing.append(f"{league}:{season}:{window}")
            continue
        payload = (json.loads(path.read_text()) or {}).get("payload")
        if payload is None:
            missing.append(f"{league}:{season}:{window}:empty")
            continue
        for event in payload.get("events") or []:
            if not isinstance(event, Mapping):
                continue
            profile = profile_from_event(event, league)
            # Season is filtered on the PAYLOAD's own identity, so a fixture that
            # a discovery window merely overlapped is not attributed to `season`.
            if profile is not None and profile.season == season:
                profiles[profile.event_id] = profile
    return profiles, missing


def load_many(
    leagues: Sequence[str],
    seasons: Sequence[int],
) -> Tuple[Dict[str, ShotProfile], List[str]]:
    """Shot profiles for a grid of leagues and seasons, keyed by event id."""
    everything: Dict[str, ShotProfile] = {}
    missing: List[str] = []
    for league in leagues:
        for season in seasons:
            profiles, gaps = load_shot_profiles(league, season)
            everything.update(profiles)
            missing.extend(gaps)
    return everything, missing


@dataclass(frozen=True)
class AgreementDiagnostic:
    """
    Evidence that the statistics block describes THIS match, not the season.

    Reported, never used as a filter. If the blocks were season aggregates the
    whole direction would be a LEAK-001 repeat, so this number is the reason the
    sidecar is trustworthy at all - and it has to be visible in the artifact for
    that claim to be checkable by someone who did not run it.
    """

    checked: int
    agreeing: int

    @property
    def rate(self) -> Optional[float]:
        return (self.agreeing / self.checked) if self.checked else None


def agreement_diagnostic(
    matches: Iterable[HistoricalMatch],
    profiles: Mapping[str, ShotProfile],
) -> AgreementDiagnostic:
    """
    Does each available block's own `totalGoals` equal the recorded scoreline?

    A season-cumulative block would disagree grossly and increasingly through the
    season. Agreement in the high nineties is what licenses treating these fields
    as per-match observations.
    """
    checked = 0
    agreeing = 0
    for match in matches:
        profile = profiles.get(match.event_id)
        if profile is None or not profile.available:
            continue
        if match.home_goals is None or match.away_goals is None:
            continue
        for line, goals in ((profile.home, match.home_goals), (profile.away, match.away_goals)):
            if line.stated_goals is None:
                continue
            checked += 1
            if line.stated_goals == goals:
                agreeing += 1
    return AgreementDiagnostic(checked=checked, agreeing=agreeing)
