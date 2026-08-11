"""
The historical observation contract (Epic 2B.2).

Epic 2B.1 established WHICH SEASON an event belongs to. This module establishes
WHAT WE KEEP about it, and answers a second, independent question:

    is this match admissible evidence for regular-season team strength?

Three ideas are kept deliberately separate, because conflating them is how a
dataset quietly becomes wrong:

    SEASON IDENTITY      does this event belong to season S?   (2B.1)
    RAW HISTORY          what did the provider actually say?   (here)
    MODEL ELIGIBILITY    should a league model learn from it?  (here)

A record is preserved in the raw dataset regardless of its eligibility. Nothing
is deleted for being a playoff; it is LABELLED as one, and the label carries its
own reason so the decision can be audited or reversed without re-fetching.

This module is provider-independent. It accepts already-extracted values, never
JSON, for the same reason `domain.season_identity` does: a second provider
should mean a second extractor, not a second copy of the rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

from domain.season_identity import season_year_from_label

__all__ = [
    "SCHEMA_VERSION",
    "ELIGIBILITY_RULE_VERSION",
    "HistoricalMatch",
    "ModelEligibility",
    "EligibilityAssessment",
    "classify_model_eligibility",
    "sort_key",
    "sort_matches",
    "to_json_dict",
    "from_json_dict",
    "to_jsonl_line",
    "from_jsonl_line",
    "model_dataset",
    "matches_before",
    "duplicate_event_ids",
    "repeated_pairings",
]

# Bumped when the on-disk record shape changes. Written into every manifest so a
# dataset built by an older version is recognisable rather than silently
# misread.
SCHEMA_VERSION = "2b2.1"

# Bumped when the eligibility VOCABULARY changes. Separate from the schema
# version because the same records can be reclassified without the shape
# changing, and a later Epic must be able to tell those two apart.
ELIGIBILITY_RULE_VERSION = "2b2.1"


class ModelEligibility(str, Enum):
    """
    Whether a match may inform regular-season team-strength estimation.

    Three values, not a boolean, for the same reason `SeasonVerdict` has four:
    "this is a playoff" and "I cannot tell what this is" are different facts,
    and only the second is a reason to go and look at the provider.
    """

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class EligibilityAssessment:
    """A verdict plus the evidence for it. The reason is part of the answer."""

    verdict: ModelEligibility
    reason: str
    phase: Optional[str] = None


# ---------------------------------------------------------------------------
# Eligibility vocabulary
#
# Every entry below is derived from the Epic 2A corpus (53,934 events, 7
# leagues, 2006-2025), not from what the words sound like. That distinction is
# the whole point of this table - see `group-stage`.
# ---------------------------------------------------------------------------

# Substrings that mark a knockout / promotion / relegation decider. Matched
# against the lower-cased phase. Verified against the corpus: no ordinary league
# slug contains any of them, and every slug that does is a genuine postseason
# tie.
_POSTSEASON_MARKERS: Tuple[str, ...] = (
    "playoff",
    "play-off",
    "promotion",
    "relegation",
    "semifinal",
    "semi-final",
    "quarterfinal",
    "quarter-final",
    "final",
    "qualif",
)

# Phases that ESPN uses for ORDINARY league play despite not being season
# labels. Each is here because the corpus says so, and the count is the
# evidence:
#
#   regular-season  7,480 events. Self-describing; eng.2 2017+, fra.1 2016-19,
#                   ger.2 2017-18, ita.1 2022. Always the league programme.
#
#   group-stage       306 events, and this is the important one. 303 of them are
#                   ger.1 2010/11 - 303 of that season's 306 fixtures, played by
#                   18 clubs from 2010-08-20 to 2011-05-14. It is an entire
#                   Bundesliga season mislabelled. The other 3 are ordinary
#                   esp.1 2010/11 fixtures (Valencia v Almeria, and two more).
#                   A rule that read the WORD rather than the DATA would delete
#                   a complete legitimate league season.
_REGULAR_SEASON_PHASES: frozenset = frozenset({"regular-season", "group-stage"})


def classify_model_eligibility(phase: Optional[str]) -> EligibilityAssessment:
    """
    Decide whether a match may inform regular-season team strength.

    Deliberately NOT `phase != "regular-season"`. Epic 2B.1 measured why: that
    rule discards 303 ordinary Bundesliga fixtures labelled `group-stage`, and
    also discards every pre-2017 season, whose phase is a season label
    ('20062007-english-league-championship') rather than the word.

    Order matters. Postseason markers are checked FIRST, so a season-shaped
    label that also names a playoff ('20082009-german-bundesliga-playoffs')
    resolves as a playoff rather than as an ordinary season.

    An unrecognised phase returns UNCERTAIN. It is not assumed to be ordinary
    league play, and it is not assumed to be a playoff - it is reported, and the
    record stays in the raw dataset either way.
    """
    if phase is None:
        return EligibilityAssessment(
            ModelEligibility.UNCERTAIN,
            "no phase stated by the provider",
            None,
        )

    if not isinstance(phase, str) or not phase.strip():
        return EligibilityAssessment(
            ModelEligibility.UNCERTAIN,
            "phase present but empty or not a string",
            phase if isinstance(phase, str) else None,
        )

    normalised = phase.strip().lower()

    for marker in _POSTSEASON_MARKERS:
        if marker in normalised:
            return EligibilityAssessment(
                ModelEligibility.INELIGIBLE,
                f"phase names a postseason tie ({marker!r})",
                phase,
            )

    if normalised in _REGULAR_SEASON_PHASES:
        return EligibilityAssessment(
            ModelEligibility.ELIGIBLE,
            f"phase is a known regular-season marker ({normalised!r})",
            phase,
        )

    if season_year_from_label(normalised) is not None:
        return EligibilityAssessment(
            ModelEligibility.ELIGIBLE,
            "phase is a season label with no postseason marker",
            phase,
        )

    return EligibilityAssessment(
        ModelEligibility.UNCERTAIN,
        "phase is not a recognised season label or phase marker",
        phase,
    )


@dataclass(frozen=True)
class HistoricalMatch:
    """
    One validated historical fixture, as the provider stated it.

    Immutable, and deliberately close to the payload: this is a record of what
    ESPN said, not a feature vector. Derived quantities belong downstream, where
    they can be recomputed; provenance cannot be recomputed once discarded.

    `season` is the provider's OWN season identity (Epic 2B.1), never the season
    we requested. The two agreeing is what admitted the record in the first
    place; storing the request would make the field circular and useless as
    evidence.

    Scores are Optional because a postponed or abandoned fixture is real
    history: it happened, it is part of the season's shape, and it has no
    result. `completed` is the field that says whether they mean anything, and
    `__post_init__` refuses the contradiction of a completed match with no
    score rather than substituting zero for it.
    """

    event_id: str
    competition: str
    season: int
    kickoff: datetime
    home_team_id: str
    away_team_id: str
    completed: bool
    home_goals: Optional[int] = None
    away_goals: Optional[int] = None
    status: Optional[str] = None
    home_team_name: Optional[str] = None
    away_team_name: Optional[str] = None
    season_phase: Optional[str] = None
    provider: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("HistoricalMatch.event_id is required and must be non-empty")
        if not self.competition:
            raise ValueError("HistoricalMatch.competition is required and must be non-empty")
        if isinstance(self.season, bool) or not isinstance(self.season, int):
            raise ValueError(
                f"HistoricalMatch.season must be an int; got {self.season!r}. "
                "A season is never inferred - see domain.season_identity."
            )
        if not isinstance(self.kickoff, datetime):
            raise ValueError("HistoricalMatch.kickoff must be a datetime")
        if self.kickoff.tzinfo is None:
            raise ValueError(
                "HistoricalMatch.kickoff must be timezone-aware; got a naive datetime. "
                "A naive kickoff cannot be ordered against another season's fixtures."
            )
        if not self.home_team_id or not self.away_team_id:
            raise ValueError("HistoricalMatch requires both team ids")
        if self.completed and (self.home_goals is None or self.away_goals is None):
            raise ValueError(
                f"HistoricalMatch {self.event_id} is completed but is missing a score. "
                "Zero is never substituted for an unknown result."
            )
        for goals in (self.home_goals, self.away_goals):
            if goals is None:
                continue
            if isinstance(goals, bool) or not isinstance(goals, int) or goals < 0:
                raise ValueError(
                    f"HistoricalMatch {self.event_id} has a non-integer or negative score: "
                    f"{self.home_goals!r}-{self.away_goals!r}"
                )

    @property
    def eligibility(self) -> EligibilityAssessment:
        """This match's modeling eligibility, derived from its stated phase."""
        return classify_model_eligibility(self.season_phase)

    @property
    def has_result(self) -> bool:
        """Completed and carrying both scores - i.e. usable as an observation."""
        return self.completed and self.home_goals is not None and self.away_goals is not None

    @property
    def total_goals(self) -> Optional[int]:
        if self.home_goals is None or self.away_goals is None:
            return None
        return self.home_goals + self.away_goals

    @property
    def pairing(self) -> Tuple[str, str, str]:
        """(competition, home, away) - the fixture, ignoring which event id it got."""
        return (self.competition, self.home_team_id, self.away_team_id)


# ---------------------------------------------------------------------------
# Ordering and serialisation
# ---------------------------------------------------------------------------

# Written in this order on every line. Fixed so that two builds of the same data
# are byte-identical and diffable.
_FIELD_ORDER: Tuple[str, ...] = (
    "event_id",
    "competition",
    "season",
    "kickoff",
    "home_team_id",
    "away_team_id",
    "home_team_name",
    "away_team_name",
    "home_goals",
    "away_goals",
    "completed",
    "status",
    "season_phase",
    "provider",
)


def sort_key(match: HistoricalMatch) -> Tuple[str, int, str, str]:
    """
    Total order over historical matches.

    Kickoff alone is not a total order - fixtures kick off simultaneously all
    the time - so the provider's event id breaks ties. Without that last term
    the output of two identical builds could differ, and reproducibility is a
    stated requirement rather than a nicety.
    """
    return (match.competition, match.season, match.kickoff.isoformat(), match.event_id)


def sort_matches(matches: Iterable[HistoricalMatch]) -> List[HistoricalMatch]:
    """Deterministic ordering. Same input, same order, always."""
    return sorted(matches, key=sort_key)


def to_json_dict(match: HistoricalMatch) -> Dict[str, Any]:
    """Serialise one record, with a fixed key order and UTC timestamps."""
    kickoff = match.kickoff.astimezone(timezone.utc)
    values: Dict[str, Any] = {
        "event_id": match.event_id,
        "competition": match.competition,
        "season": match.season,
        "kickoff": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "home_team_id": match.home_team_id,
        "away_team_id": match.away_team_id,
        "home_team_name": match.home_team_name,
        "away_team_name": match.away_team_name,
        "home_goals": match.home_goals,
        "away_goals": match.away_goals,
        "completed": match.completed,
        "status": match.status,
        "season_phase": match.season_phase,
        "provider": match.provider,
    }
    return {key: values[key] for key in _FIELD_ORDER}


def from_json_dict(raw: Dict[str, Any]) -> HistoricalMatch:
    """
    Rebuild a record from its serialised form.

    Unknown keys are an error rather than something to ignore: a file written by
    a newer schema must not be read as though the extra fields did not matter.
    """
    unknown = set(raw) - set(_FIELD_ORDER)
    if unknown:
        raise ValueError(f"unrecognised field(s) in historical record: {sorted(unknown)}")

    kickoff_raw = raw.get("kickoff")
    if not isinstance(kickoff_raw, str):
        raise ValueError("historical record is missing its kickoff")
    parsed = datetime.fromisoformat(kickoff_raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return HistoricalMatch(
        event_id=raw["event_id"],
        competition=raw["competition"],
        season=raw["season"],
        kickoff=parsed,
        home_team_id=raw["home_team_id"],
        away_team_id=raw["away_team_id"],
        completed=bool(raw["completed"]),
        home_goals=raw.get("home_goals"),
        away_goals=raw.get("away_goals"),
        status=raw.get("status"),
        home_team_name=raw.get("home_team_name"),
        away_team_name=raw.get("away_team_name"),
        season_phase=raw.get("season_phase"),
        provider=raw.get("provider"),
    )


def to_jsonl_line(match: HistoricalMatch) -> str:
    """One record as one line. `sort_keys=False` preserves `_FIELD_ORDER`."""
    return json.dumps(to_json_dict(match), sort_keys=False, ensure_ascii=False)


def from_jsonl_line(line: str) -> HistoricalMatch:
    """Inverse of `to_jsonl_line`."""
    return from_json_dict(json.loads(line))


# ---------------------------------------------------------------------------
# Views over a dataset
# ---------------------------------------------------------------------------


def model_dataset(matches: Iterable[HistoricalMatch]) -> List[HistoricalMatch]:
    """
    The subset a regular-season team-strength model may learn from.

    A VIEW, not a filter applied at write time. The raw dataset keeps every
    validated record; this narrows it at the point of use, so the exclusion can
    be re-examined - or reversed by a later modelling decision - without
    re-fetching a single byte from ESPN.

    Two independent requirements: the match must be ordinary league play
    (ELIGIBLE, so neither a playoff nor an unrecognised phase), and it must have
    an actual result. UNCERTAIN is excluded here and reported by the builder,
    because "I do not know what this is" is not a licence to train on it.
    """
    return [
        match
        for match in sort_matches(matches)
        if match.eligibility.verdict is ModelEligibility.ELIGIBLE and match.has_result
    ]


def matches_before(
    matches: Iterable[HistoricalMatch],
    cutoff: datetime,
    *,
    competition: Optional[str] = None,
    season: Optional[int] = None,
    team_id: Optional[str] = None,
    eligible_only: bool = True,
) -> List[HistoricalMatch]:
    """
    Everything knowable strictly BEFORE `cutoff`.

    The point-in-time guarantee, unchanged from Epic 1B.5 and restated here for
    the dataset layer:

        match.kickoff < cutoff        strictly less than, never <=

    `<` rather than `<=` is what stops a fixture contributing its own result to
    its own features. Two fixtures kicking off at the same instant tell us
    nothing about each other either, and the strict comparison excludes those
    for free - which is why the boundary is not a tolerance to be relaxed.

    A naive `cutoff` is refused rather than assumed to be UTC. Guessing a
    timezone here would move the boundary by hours, in whichever direction
    happened to leak the most.
    """
    if cutoff.tzinfo is None:
        raise ValueError(
            "matches_before() requires a timezone-aware cutoff; got a naive datetime. "
            "A naive cutoff silently shifts the point-in-time boundary."
        )

    selected: List[HistoricalMatch] = []
    for match in matches:
        if match.kickoff >= cutoff:
            continue
        if competition is not None and match.competition != competition:
            continue
        if season is not None and match.season != season:
            continue
        if team_id is not None and team_id not in (match.home_team_id, match.away_team_id):
            continue
        if eligible_only and match.eligibility.verdict is not ModelEligibility.ELIGIBLE:
            continue
        if eligible_only and not match.has_result:
            continue
        selected.append(match)

    return sort_matches(selected)


def duplicate_event_ids(matches: Iterable[HistoricalMatch]) -> Dict[str, int]:
    """
    Event ids appearing more than once, with their counts.

    An exact duplicate is a builder defect - the provider's id is the identity -
    so this should always be empty and is asserted rather than tolerated.
    """
    counts: Dict[str, int] = {}
    for match in matches:
        key = f"{match.competition}:{match.season}:{match.event_id}"
        counts[key] = counts.get(key, 0) + 1
    return {key: count for key, count in sorted(counts.items()) if count > 1}


def repeated_pairings(matches: Iterable[HistoricalMatch]) -> Dict[str, int]:
    """
    Home/away pairings occurring more than once in the same season.

    REPORTED, NOT REMOVED. In a double round-robin the same pairing should occur
    once per season, so a repeat is worth a look - but ita.1 2022/23 contains a
    genuine third Spezia-Verona meeting (a relegation playoff), and eng.2 keeps
    the original row of a postponed fixture alongside its replay. Both are real
    history with distinct event ids. Deleting either to make a season reach an
    expected count would be fabricating data, so this only counts them.
    """
    counts: Dict[str, int] = {}
    for match in matches:
        competition, home, away = match.pairing
        key = f"{competition}:{match.season}:{home}v{away}"
        counts[key] = counts.get(key, 0) + 1
    return {key: count for key, count in sorted(counts.items()) if count > 1}


def with_provider(match: HistoricalMatch, provider: str) -> HistoricalMatch:
    """Attach provider provenance without mutating the original record."""
    return replace(match, provider=provider)
