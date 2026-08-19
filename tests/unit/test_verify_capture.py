"""
Epic 2I — the `verify_capture.py` CLI.

Every test here is offline. `conftest.py` blocks sockets, so a test that
accidentally reached ESPN would fail loudly rather than pass slowly on live data —
and the schedule source is injected explicitly in any case.

Two groups carry the weight: the exit-code contract (an observer by default) and
the ledger-integrity proof (read-only, verified rather than asserted).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import verify_capture
from domain.capture_audit import ExpectedFixture, reconcile
from verify_capture import (
    EXIT_GAP,
    EXIT_LEDGER_MUTATED,
    EXIT_NO_SCHEDULE,
    EXIT_OK,
    build_payload,
    collect_schedule,
    ledger_digest,
    main,
    month_days,
    render,
    since_days,
    write_audit,
)

DAY = date(2026, 8, 15)
NOON = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)


def fixture(fixture_id: str, *, status: Optional[str] = None, kickoff: Any = NOON) -> ExpectedFixture:
    return ExpectedFixture(
        fixture_id=fixture_id,
        competition="eng.1",
        season=2026,
        kickoff=kickoff,
        status=status,
    )


def ledger_line(fixture_id: str, *, prediction_id: str = "p1") -> str:
    return json.dumps(
        {
            "schema_version": "2g.1",
            "prediction_id": prediction_id,
            "fixture_id": fixture_id,
            "competition": "eng.1",
            "season": 2026,
            "kickoff": NOON.isoformat(),
            "probability": 0.62,
        }
    )


@pytest.fixture()
def ledger_dir(tmp_path: Path) -> Path:
    path = tmp_path / "predictions"
    path.mkdir()
    return path


def write_ledger(ledger_dir: Path, *fixture_ids: str, month: str = "2026-08") -> Path:
    path = ledger_dir / f"{month}.jsonl"
    path.write_text(
        "".join(f"{ledger_line(fid)}\n" for fid in fixture_ids),
        encoding="utf-8",
    )
    return path


def source_of(*fixtures: ExpectedFixture, available: bool = True) -> Any:
    """A schedule source serving the same fixtures for every requested date."""

    def source(day: date) -> Tuple[List[ExpectedFixture], bool]:
        return list(fixtures), available

    return source


def run(
    argv: List[str],
    *,
    monkeypatch: pytest.MonkeyPatch,
    schedule: Any,
) -> int:
    """Invoke main() with the schedule source replaced. Never touches the network."""
    monkeypatch.setattr(verify_capture, "espn_schedule_source", lambda: schedule)
    return main(argv)


# ---------------------------------------------------------------------------
# Exit codes: an observer by default
# ---------------------------------------------------------------------------


def test_a_capture_gap_alone_exits_zero(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    # The default must stay observational. An audit that fails a scheduled job on
    # a football-side quirk gets removed from the schedule.
    code = run(
        ["--date", "2026-08-15", "--ledger-dir", str(ledger_dir), "--out", str(tmp_path / "out")],
        monkeypatch=monkeypatch,
        schedule=source_of(fixture("1")),
    )
    assert code == EXIT_OK


def test_fail_on_gap_makes_a_gap_non_zero(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    code = run(
        [
            "--date",
            "2026-08-15",
            "--ledger-dir",
            str(ledger_dir),
            "--out",
            str(tmp_path / "out"),
            "--fail-on-gap",
        ],
        monkeypatch=monkeypatch,
        schedule=source_of(fixture("1")),
    )
    assert code == EXIT_GAP


def test_fail_on_gap_still_exits_zero_when_capture_is_complete(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    write_ledger(ledger_dir, "1")
    code = run(
        [
            "--date",
            "2026-08-15",
            "--ledger-dir",
            str(ledger_dir),
            "--out",
            str(tmp_path / "out"),
            "--fail-on-gap",
        ],
        monkeypatch=monkeypatch,
        schedule=source_of(fixture("1")),
    )
    assert code == EXIT_OK


def test_fail_on_gap_exits_zero_for_a_partial_capture(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    # The false-alarm rule, at the CLI boundary: GG-013 skips must not fail a job.
    write_ledger(ledger_dir, "1")
    code = run(
        [
            "--date",
            "2026-08-15",
            "--ledger-dir",
            str(ledger_dir),
            "--out",
            str(tmp_path / "out"),
            "--fail-on-gap",
        ],
        monkeypatch=monkeypatch,
        schedule=source_of(fixture("1"), fixture("2"), fixture("3")),
    )
    assert code == EXIT_OK


def test_an_unavailable_schedule_is_not_a_gap_but_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    # A provider outage must never be reported as lost evidence. It is an unknown,
    # and blaming our pipeline for someone else's downtime would destroy trust in
    # the one alert this tool raises.
    code = run(
        [
            "--date",
            "2026-08-15",
            "--ledger-dir",
            str(ledger_dir),
            "--out",
            str(tmp_path / "out"),
            "--fail-on-gap",
        ],
        monkeypatch=monkeypatch,
        schedule=source_of(available=False),
    )
    assert code == EXIT_NO_SCHEDULE


def test_no_fixtures_scheduled_exits_zero_even_with_fail_on_gap(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    code = run(
        [
            "--date",
            "2026-08-15",
            "--ledger-dir",
            str(ledger_dir),
            "--out",
            str(tmp_path / "out"),
            "--fail-on-gap",
        ],
        monkeypatch=monkeypatch,
        schedule=source_of(),
    )
    assert code == EXIT_OK


# ---------------------------------------------------------------------------
# Ledger integrity
# ---------------------------------------------------------------------------


def test_the_ledger_is_byte_identical_after_a_run(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    path = write_ledger(ledger_dir, "1", "2")
    before = path.read_bytes()
    digest_before = ledger_digest(ledger_dir)

    run(
        ["--date", "2026-08-15", "--ledger-dir", str(ledger_dir), "--out", str(tmp_path / "out")],
        monkeypatch=monkeypatch,
        schedule=source_of(fixture("1"), fixture("2")),
    )

    assert path.read_bytes() == before
    assert ledger_digest(ledger_dir) == digest_before


def test_the_digest_detects_an_edited_line(ledger_dir: Path) -> None:
    # Proving the guard works. A digest that cannot detect a modification is
    # decoration, and every read-only claim in this Epic rests on it.
    path = write_ledger(ledger_dir, "1")
    before = ledger_digest(ledger_dir)
    path.write_text(ledger_line("1", prediction_id="tampered") + "\n", encoding="utf-8")
    assert ledger_digest(ledger_dir) != before


def test_the_digest_detects_an_appended_line(ledger_dir: Path) -> None:
    write_ledger(ledger_dir, "1")
    before = ledger_digest(ledger_dir)
    write_ledger(ledger_dir, "1", "2")
    assert ledger_digest(ledger_dir) != before


def test_the_digest_detects_a_deleted_month(ledger_dir: Path) -> None:
    # Filenames are hashed, so losing a whole month is caught too.
    write_ledger(ledger_dir, "1", month="2026-07")
    write_ledger(ledger_dir, "2", month="2026-08")
    before = ledger_digest(ledger_dir)
    (ledger_dir / "2026-07.jsonl").unlink()
    assert ledger_digest(ledger_dir) != before


def test_the_digest_of_an_absent_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    assert ledger_digest(tmp_path / "nope") == "sha256:empty"


def test_a_ledger_that_changes_mid_run_is_reported(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    # Simulates a prediction run writing while the audit reads. The audit's numbers
    # describe a moving target, so it says so rather than publishing them quietly.
    write_ledger(ledger_dir, "1")
    calls = {"n": 0}
    real_load = verify_capture.load_records

    def load_then_mutate(*args: Any, **kwargs: Any) -> Any:
        records = real_load(*args, **kwargs)
        calls["n"] += 1
        write_ledger(ledger_dir, "1", "2")
        return records

    monkeypatch.setattr(verify_capture, "load_records", load_then_mutate)
    code = run(
        ["--date", "2026-08-15", "--ledger-dir", str(ledger_dir), "--out", str(tmp_path / "out")],
        monkeypatch=monkeypatch,
        schedule=source_of(fixture("1")),
    )
    assert calls["n"] == 1
    assert code == EXIT_LEDGER_MUTATED


def test_nothing_is_written_into_the_ledger_directory(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    write_ledger(ledger_dir, "1")
    before = sorted(p.name for p in ledger_dir.iterdir())
    run(
        ["--date", "2026-08-15", "--ledger-dir", str(ledger_dir), "--out", str(tmp_path / "out")],
        monkeypatch=monkeypatch,
        schedule=source_of(fixture("1")),
    )
    assert sorted(p.name for p in ledger_dir.iterdir()) == before


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


def test_the_artifact_is_written_and_names_its_schedule_source(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    write_ledger(ledger_dir, "1")
    run(
        ["--date", "2026-08-15", "--ledger-dir", str(ledger_dir), "--out", str(out)],
        monkeypatch=monkeypatch,
        schedule=source_of(fixture("1")),
    )
    files = list(out.glob("capture_*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["schedule_source"] == "espn"
    assert payload["replay_used"] is False
    assert payload["probability_source"] is None


def test_dry_run_writes_no_artifact(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    code = run(
        ["--date", "2026-08-15", "--ledger-dir", str(ledger_dir), "--out", str(out), "--dry-run"],
        monkeypatch=monkeypatch,
        schedule=source_of(fixture("1")),
    )
    assert code == EXIT_OK
    assert not out.exists()


def test_artifacts_never_overwrite_each_other(tmp_path: Path) -> None:
    moment = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    first = write_audit({"a": 1}, generated_at=moment, audit_dir=tmp_path)
    second = write_audit({"a": 2}, generated_at=moment, audit_dir=tmp_path)
    assert first != second
    assert json.loads(first.read_text()) == {"a": 1}


def test_the_artifact_carries_both_schema_versions_and_the_integrity_block() -> None:
    payload = build_payload(
        reconcile([fixture("1")], []),
        generated_at=NOON,
        schedule_source="dataset",
        requested=[DAY],
        resolved=[DAY],
        unavailable=[],
        ledger_dir=Path("data/predictions"),
        dataset=Path("corpus"),
        digest_before="sha256:a",
        digest_after="sha256:a",
    )
    assert payload["schema_version"] == "2i.1"
    assert payload["capture_audit_schema_version"] == "2i.1"
    assert payload["ledger_integrity"]["unchanged"] is True
    assert payload["totals"]["capture_gaps"] == 1
    assert payload["days"][0]["verdict"] == "ZERO_CAPTURE"


def test_unavailable_days_are_named_in_the_artifact() -> None:
    payload = build_payload(
        reconcile([], []),
        generated_at=NOON,
        schedule_source="espn",
        requested=[DAY, date(2026, 8, 16)],
        resolved=[DAY],
        unavailable=[date(2026, 8, 16)],
        ledger_dir=Path("l"),
        dataset=None,
        digest_before="sha256:a",
        digest_after="sha256:a",
    )
    assert payload["inputs"]["days_schedule_unavailable"] == ["2026-08-16"]
    assert payload["inputs"]["days_requested"] == ["2026-08-15", "2026-08-16"]


def test_the_artifact_is_deterministic_apart_from_generated_at() -> None:
    # Same inputs, two instants: everything but the timestamp must match, or a
    # rerun could not be compared with an earlier one.
    args: Dict[str, Any] = {
        "schedule_source": "dataset",
        "requested": [DAY],
        "resolved": [DAY],
        "unavailable": [],
        "ledger_dir": Path("l"),
        "dataset": Path("d"),
        "digest_before": "sha256:a",
        "digest_after": "sha256:a",
    }
    audit = reconcile([fixture("1"), fixture("2")], [{"fixture_id": "1", "prediction_id": "p1"}])
    first = build_payload(audit, generated_at=NOON, **args)
    second = build_payload(audit, generated_at=NOON + timedelta(hours=3), **args)
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


def test_render_marks_gap_days_and_never_crashes_on_an_empty_audit() -> None:
    payload = build_payload(
        reconcile([fixture("1")], []),
        generated_at=NOON,
        schedule_source="dataset",
        requested=[DAY],
        resolved=[DAY],
        unavailable=[],
        ledger_dir=Path("l"),
        dataset=None,
        digest_before="sha256:a",
        digest_after="sha256:a",
    )
    text = render(payload)
    assert "GAP" in text
    assert "cannot be reconstructed" in text


# ---------------------------------------------------------------------------
# Date ranges
# ---------------------------------------------------------------------------


def test_month_days_covers_a_31_day_month() -> None:
    days = month_days("2026-08")
    assert (len(days), days[0], days[-1]) == (31, date(2026, 8, 1), date(2026, 8, 31))


def test_month_days_handles_february_in_a_leap_year() -> None:
    assert len(month_days("2024-02")) == 29


def test_month_days_rolls_over_december() -> None:
    days = month_days("2026-12")
    assert (len(days), days[-1]) == (31, date(2026, 12, 31))


@pytest.mark.parametrize("bad", ["2026", "2026-13", "not-a-month", "2026-1"])
def test_month_days_refuses_a_malformed_month(bad: str) -> None:
    with pytest.raises(ValueError):
        month_days(bad)


def test_since_days_ends_yesterday_and_excludes_today() -> None:
    # Fixtures later today may not have kicked off, so a missing prediction for
    # them is not yet evidence of anything.
    days = since_days("3d", date(2026, 8, 19))
    assert days == [date(2026, 8, 16), date(2026, 8, 17), date(2026, 8, 18)]


def test_since_days_crosses_a_month_boundary() -> None:
    assert since_days("2d", date(2026, 9, 1)) == [date(2026, 8, 30), date(2026, 8, 31)]


@pytest.mark.parametrize("bad", ["7", "d", "0d", "-1d", "week"])
def test_since_days_refuses_a_malformed_window(bad: str) -> None:
    with pytest.raises(ValueError):
        since_days(bad, DAY)


def test_collect_schedule_separates_resolved_days_from_unavailable_ones() -> None:
    def flaky(day: date) -> Tuple[List[ExpectedFixture], bool]:
        if day == DAY:
            return [fixture("1")], True
        return [], False

    fixtures, resolved, unavailable = collect_schedule([DAY, date(2026, 8, 16)], flaky)
    assert len(fixtures) == 1
    assert resolved == [DAY]
    assert unavailable == [date(2026, 8, 16)]


# ---------------------------------------------------------------------------
# Offline dataset behaviour
# ---------------------------------------------------------------------------


def test_dataset_mode_reads_the_schedule_from_disk_without_touching_espn(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    # `load_dataset` is stubbed at the seam `dataset_schedule_source` imports, so
    # no corpus format is assumed and no provider module is imported.
    class Match:
        def __init__(self, event_id: str) -> None:
            self.event_id = event_id
            self.competition = "eng.1"
            self.season = 2026
            self.kickoff = NOON
            self.status = None

    import historical_dataset

    monkeypatch.setattr(historical_dataset, "load_dataset", lambda _: [Match("1"), Match("2")])

    def explode() -> Any:  # pragma: no cover - must never be called
        raise AssertionError("dataset mode must not build an ESPN schedule source")

    monkeypatch.setattr(verify_capture, "espn_schedule_source", explode)

    out = tmp_path / "out"
    write_ledger(ledger_dir, "1")
    code = main(
        [
            "--date",
            "2026-08-15",
            "--ledger-dir",
            str(ledger_dir),
            "--out",
            str(out),
            "--dataset",
            str(tmp_path / "corpus"),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(next(out.glob("capture_*.json")).read_text())
    assert payload["schedule_source"] == "dataset"
    assert payload["inputs"]["dataset"] == str(tmp_path / "corpus")
    assert payload["totals"]["expected"] == 2
    assert payload["totals"]["captured"] == 1


def test_a_dataset_date_with_no_fixtures_is_available_not_an_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A corpus on disk always answers. Absence of a date is a real "nothing
    # scheduled", which is what makes offline runs safe to gate on.
    import historical_dataset

    monkeypatch.setattr(historical_dataset, "load_dataset", lambda _: [])
    source = verify_capture.dataset_schedule_source(tmp_path)
    assert source(DAY) == ([], True)


# ---------------------------------------------------------------------------
# Determinism end to end
# ---------------------------------------------------------------------------


def test_two_identical_runs_produce_identical_artifacts_apart_from_the_timestamp(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    write_ledger(ledger_dir, "1")
    argv = ["--date", "2026-08-15", "--ledger-dir", str(ledger_dir), "--out", str(out)]
    schedule = source_of(fixture("1"), fixture("2"), fixture("3", status="postponed"))

    run(argv, monkeypatch=monkeypatch, schedule=schedule)
    run(argv, monkeypatch=monkeypatch, schedule=schedule)

    payloads = []
    for path in sorted(out.glob("capture_*.json")):
        payload = json.loads(path.read_text())
        payload.pop("generated_at")
        payloads.append(payload)
    assert len(payloads) == 2
    assert payloads[0] == payloads[1]


def test_the_default_window_is_yesterday(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path, tmp_path: Path
) -> None:
    seen: List[date] = []

    def recording(day: date) -> Tuple[List[ExpectedFixture], bool]:
        seen.append(day)
        return [], True

    run(
        ["--ledger-dir", str(ledger_dir), "--out", str(tmp_path / "out")],
        monkeypatch=monkeypatch,
        schedule=recording,
    )
    assert len(seen) == 1
    assert seen[0] == datetime.now(timezone.utc).date() - timedelta(days=1)


def test_date_month_and_since_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path
) -> None:
    with pytest.raises(SystemExit):
        run(
            ["--date", "2026-08-15", "--month", "2026-08"],
            monkeypatch=monkeypatch,
            schedule=source_of(),
        )


def test_a_malformed_date_is_refused_by_the_cli(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path
) -> None:
    # `parser.error` rather than a raw traceback: SystemExit(2) with a usage
    # message is argparse's convention for bad input, and it is what the other
    # entry points do. My first draft asserted ValueError, which was wrong about
    # the intended behaviour rather than a defect in it.
    with pytest.raises(SystemExit) as exit_info:
        run(["--date", "15-08-2026"], monkeypatch=monkeypatch, schedule=source_of())
    assert exit_info.value.code == 2


def test_a_malformed_since_window_is_refused_by_the_cli(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run(["--since", "one-week"], monkeypatch=monkeypatch, schedule=source_of())
    assert exit_info.value.code == 2

