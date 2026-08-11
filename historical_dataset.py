"""
Historical dataset builder (Epic 2B.2).

Turns validated provider readouts into a reproducible on-disk corpus:

    espn.get_league_history()      season identity enforced (Epic 2B.1)
             |
             v
    build_league_season()          raw records + an account of what was refused
             |
             v
    write_dataset()                one JSONL file per league-season
             |
             v
    build_manifest()               counts, checksums, anomalies, versions

DETERMINISM IS THE POINT. Two builds of the same source data must produce
byte-identical files, because a dataset that shifts under you cannot be used to
compare two models: any difference in a later backtest would be unattributable.
That is why records are sorted by `domain.historical.sort_key`, written with a
fixed field order, and checksummed - and why `built_at` is recorded in the
manifest but deliberately excluded from the checksum.

Nothing here filters for modelling. Playoffs, postponements and unrecognised
phases are all written to disk with their labels intact; `model_dataset()`
narrows at the point of use. The build step must not be the place where records
quietly disappear, or the corpus stops being a record of what the provider said.

This module performs NO model mathematics and is not imported by any production
path (main.py, analyze_all.py, run3/). It is an offline tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import espn
from domain.historical import (
    ELIGIBILITY_RULE_VERSION,
    SCHEMA_VERSION,
    HistoricalMatch,
    ModelEligibility,
    duplicate_event_ids,
    from_jsonl_line,
    repeated_pairings,
    sort_matches,
    to_jsonl_line,
)

__all__ = [
    "SeasonBuild",
    "BuildReport",
    "build_league_season",
    "build_dataset",
    "write_dataset",
    "load_dataset",
    "build_manifest",
    "write_manifest",
    "season_filename",
    "file_checksum",
]

# The seasons Epic 2A found usable history for, and the five wired production
# leagues. Defaults only - every entry point takes explicit arguments - but they
# keep an accidental run bounded rather than sweeping 20 years of ESPN.
DEFAULT_LEAGUES: Tuple[str, ...] = ("eng.1", "ger.1", "ita.1", "esp.1", "fra.1")

# A fetch that returns None means "the provider failed", which is NOT the same
# as an empty season. The distinction is preserved all the way into the manifest
# (Epic 1B.2 error semantics).
FetchFn = Callable[[str, int], Optional["espn.HistoricalReadout"]]


@dataclass(frozen=True)
class SeasonBuild:
    """
    The outcome of building one league-season.

    `failed` is separate from `matches == []` on purpose. A season that ESPN has
    no events for is a fact about football; a season we could not retrieve is a
    fact about the network. Writing an empty file for the second would record
    the absence as though it were evidence.
    """

    league: str
    season: int
    matches: List[HistoricalMatch]
    rejected: Dict[str, int]
    failed: bool = False

    @property
    def with_result(self) -> int:
        return sum(1 for match in self.matches if match.has_result)

    @property
    def eligible(self) -> int:
        return sum(
            1
            for match in self.matches
            if match.eligibility.verdict is ModelEligibility.ELIGIBLE and match.has_result
        )

    @property
    def uncertain(self) -> int:
        return sum(
            1
            for match in self.matches
            if match.eligibility.verdict is ModelEligibility.UNCERTAIN
        )

    @property
    def ineligible(self) -> int:
        return sum(
            1
            for match in self.matches
            if match.eligibility.verdict is ModelEligibility.INELIGIBLE
        )


@dataclass
class BuildReport:
    """Everything built in one run, plus the seasons that could not be."""

    builds: List[SeasonBuild] = field(default_factory=list)

    @property
    def matches(self) -> List[HistoricalMatch]:
        collected: List[HistoricalMatch] = []
        for build in self.builds:
            collected.extend(build.matches)
        return sort_matches(collected)

    @property
    def failures(self) -> List[SeasonBuild]:
        return [build for build in self.builds if build.failed]


def build_league_season(
    league: str,
    season: int,
    fetch: Optional[FetchFn] = None,
) -> SeasonBuild:
    """
    Build one league-season.

    `fetch` is injectable so the whole builder can be exercised offline against
    captured payloads; it defaults to the live ESPN adapter. Note that the
    adapter has already enforced season and competition identity - this function
    deliberately does NOT re-decide membership, because a second opinion on
    season identity is exactly the duplicated definition Epic 2B.1 removed.
    """
    fetcher: FetchFn = fetch or espn.get_league_history
    readout = fetcher(league, season)

    if readout is None:
        return SeasonBuild(league=league, season=season, matches=[], rejected={}, failed=True)

    return SeasonBuild(
        league=league,
        season=season,
        matches=sort_matches(readout.matches),
        rejected=dict(readout.rejected),
    )


def build_dataset(
    leagues: Sequence[str],
    seasons: Sequence[int],
    fetch: Optional[FetchFn] = None,
    on_progress: Optional[Callable[[SeasonBuild], None]] = None,
) -> BuildReport:
    """
    Build every requested league-season.

    A failed season does not abort the run - the other seasons are still real -
    but it is recorded as a failure and no file is written for it, so a partial
    corpus can never masquerade as a complete one.
    """
    report = BuildReport()
    for league in leagues:
        for season in seasons:
            build = build_league_season(league, season, fetch=fetch)
            report.builds.append(build)
            if on_progress is not None:
                on_progress(build)
    return report


def season_filename(league: str, season: int) -> str:
    """`eng.1` + 2019 -> `eng.1_2019.jsonl`. Flat, greppable, one file per season."""
    return f"{league}_{season}.jsonl"


def file_checksum(path: Path) -> str:
    """SHA-256 of the file's bytes, for the manifest."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_dataset(report: BuildReport, out_dir: Path) -> Dict[str, str]:
    """
    Write one JSONL file per successfully built league-season.

    Returns filename -> checksum. Failed seasons are absent from both the
    directory and the mapping: the manifest reports them as failures instead,
    so "we did not get this" never reads as "there was nothing here".

    Files end with a trailing newline and are written with `\\n` explicitly, so a
    build on another platform produces the same bytes and therefore the same
    checksum.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    checksums: Dict[str, str] = {}

    for build in report.builds:
        if build.failed:
            continue
        name = season_filename(build.league, build.season)
        path = out_dir / name
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for match in build.matches:
                handle.write(to_jsonl_line(match))
                handle.write("\n")
        checksums[name] = file_checksum(path)

    return checksums


def load_dataset(out_dir: Path) -> List[HistoricalMatch]:
    """
    Read a built dataset back.

    Sorted on the way out so a reader gets the same order the builder wrote,
    regardless of how the filesystem happened to enumerate the files.
    """
    matches: List[HistoricalMatch] = []
    for path in sorted(out_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    matches.append(from_jsonl_line(line))
    return sort_matches(matches)


def build_manifest(
    report: BuildReport,
    checksums: Dict[str, str],
    *,
    built_at: Optional[datetime] = None,
) -> Dict[str, object]:
    """
    Describe the build well enough to reproduce and audit it.

    Records, per league-season: how many records were written, how many carry a
    result, how many a model may learn from, how many are playoffs, how many
    have a phase we could not recognise, and every reason an event was refused.

    ANOMALIES ARE REPORTED, NOT REPAIRED. A short season stays short. Epic 2A
    established that fra.1 2019/20 was genuinely abandoned at 279 fixtures, so a
    builder that padded it to 380 would be inventing 101 matches; one that
    flagged it as corrupt would be crying wolf about a real historical event.
    The manifest states the count and leaves the judgement to a reader.

    `built_at` is included for provenance and excluded from every checksum, so
    two builds of the same data still verify as identical.
    """
    timestamp = (built_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    all_matches = report.matches

    seasons: List[Dict[str, object]] = []
    for build in sorted(report.builds, key=lambda b: (b.league, b.season)):
        name = season_filename(build.league, build.season)
        seasons.append(
            {
                "league": build.league,
                "season": build.season,
                "file": None if build.failed else name,
                "checksum": checksums.get(name),
                "provider_failed": build.failed,
                "records": len(build.matches),
                "with_result": build.with_result,
                "model_eligible": build.eligible,
                "postseason": build.ineligible,
                "uncertain_phase": build.uncertain,
                "rejected": dict(sorted(build.rejected.items())),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "eligibility_rule_version": ELIGIBILITY_RULE_VERSION,
        "built_at": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": espn.PROVIDER_NAME,
        "totals": {
            "league_seasons_requested": len(report.builds),
            "league_seasons_failed": len(report.failures),
            "records": len(all_matches),
            "with_result": sum(1 for m in all_matches if m.has_result),
            "model_eligible": sum(
                1
                for m in all_matches
                if m.eligibility.verdict is ModelEligibility.ELIGIBLE and m.has_result
            ),
        },
        # Integrity checks over the corpus as a whole. Duplicates are a builder
        # defect and must be empty; repeated pairings are usually real history
        # (a replay, a relegation playoff) and are listed for inspection.
        "duplicate_event_ids": duplicate_event_ids(all_matches),
        "repeated_pairings": repeated_pairings(all_matches),
        "seasons": seasons,
    }


def write_manifest(manifest: Dict[str, object], out_dir: Path) -> Path:
    """Write `manifest.json`, sorted and newline-terminated for stable diffs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _parse_seasons(raw: str) -> List[int]:
    """`2018-2021` or `2018,2019` -> [2018, 2019, 2020, 2021]."""
    if "-" in raw:
        start, _, end = raw.partition("-")
        return list(range(int(start), int(end) + 1))
    return [int(part) for part in raw.split(",") if part.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI. Bounded by explicit `--leagues`/`--seasons`; never sweeps by default."""
    parser = argparse.ArgumentParser(description="Build the historical match dataset (Epic 2B.2)")
    parser.add_argument("--leagues", default=",".join(DEFAULT_LEAGUES))
    parser.add_argument("--seasons", required=True, help="e.g. 2018-2021 or 2019,2020")
    parser.add_argument("--out", default="data/historical")
    args = parser.parse_args(argv)

    leagues = [code.strip() for code in args.leagues.split(",") if code.strip()]
    seasons = _parse_seasons(args.seasons)
    out_dir = Path(args.out)

    def progress(build: SeasonBuild) -> None:
        if build.failed:
            print(f"  {build.league} {build.season}: PROVIDER FAILED (no file written)")
            return
        print(
            f"  {build.league} {build.season}: {len(build.matches)} records, "
            f"{build.eligible} model-eligible, {build.ineligible} postseason, "
            f"{build.uncertain} uncertain, {sum(build.rejected.values())} rejected"
        )

    print(f"Building {len(leagues)} leagues x {len(seasons)} seasons -> {out_dir}")
    report = build_dataset(leagues, seasons, on_progress=progress)
    checksums = write_dataset(report, out_dir)
    manifest = build_manifest(report, checksums)
    write_manifest(manifest, out_dir)

    totals = manifest["totals"]
    assert isinstance(totals, dict)
    print(
        f"\nWrote {len(checksums)} files, {totals['records']} records "
        f"({totals['model_eligible']} model-eligible). "
        f"{totals['league_seasons_failed']} league-season(s) failed."
    )
    # A build with any provider failure is incomplete. Exit non-zero so it cannot
    # be mistaken for a clean corpus by anything that checks a status code.
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
