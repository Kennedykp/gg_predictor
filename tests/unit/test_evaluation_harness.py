"""
Epic 2B.3 - evaluation harness tests.

Structured around what would actually go wrong. The leakage tests matter most:
a harness that leaks produces better-looking numbers, so nothing about a passing
run would look suspicious. Each leakage test therefore constructs evidence that
WOULD change the answer if it leaked, and asserts it did not - rather than
asserting a filter was called.

Everything here is offline. No network, no odds, no live ESPN.
"""

from datetime import datetime, timedelta, timezone

import pytest

import poisson
from domain.evaluation import (
    LOG_LOSS_EPSILON,
    BttsOutcome,
    CalibrationBin,
    PredictionRecord,
    UnevaluableReason,
    brier_score,
    btts_outcome,
    calibration_table,
    log_loss,
    summarise,
    validate_probability,
)
from domain.historical import HistoricalMatch
from domain.match_records import Venue
from evaluation_harness import (
    MODEL_REGISTRY,
    PoissonV1Adapter,
    ReferenceBaseRateAdapter,
    evaluate,
    get_model,
    replay,
    to_league_records,
    to_team_records,
    write_artifacts,
)

UTC = timezone.utc


def _match(
    event_id,
    kickoff,
    home,
    away,
    home_goals=1,
    away_goals=1,
    *,
    competition="eng.1",
    season=2020,
    completed=True,
    phase="regular-season",
    status="STATUS_FULL_TIME",
):
    return HistoricalMatch(
        event_id=event_id,
        competition=competition,
        season=season,
        kickoff=kickoff,
        home_team_id=home,
        away_team_id=away,
        completed=completed,
        home_goals=home_goals,
        away_goals=away_goals,
        status=status,
        season_phase=phase,
        provider="espn",
    )


def _prediction(probability, outcome, **kwargs):
    defaults = dict(
        model_id="M",
        model_version="1",
        competition="eng.1",
        season=2020,
        event_id="e1",
        kickoff=datetime(2021, 1, 1, tzinfo=UTC),
        home_team_id="1",
        away_team_id="2",
    )
    defaults.update(kwargs)
    return PredictionRecord(outcome=outcome, probability=probability, **defaults)


def _season(n_matches=60, *, start=datetime(2020, 9, 1, 15, 0, tzinfo=UTC)):
    """A synthetic league with enough home/away history to satisfy POISSON_V1."""
    matches = []
    teams = ["1", "2", "3", "4"]
    kickoff = start
    counter = 0
    for cycle in range(n_matches // 4):
        for home_idx in range(len(teams)):
            away_idx = (home_idx + 1 + cycle) % len(teams)
            if home_idx == away_idx:
                continue
            counter += 1
            matches.append(
                _match(
                    f"m{counter}",
                    kickoff,
                    teams[home_idx],
                    teams[away_idx],
                    home_goals=(counter % 3),
                    away_goals=((counter + 1) % 3),
                )
            )
            kickoff += timedelta(days=1)
    return matches


# ---------------------------------------------------------------------------
# 1. BTTS outcome derivation
# ---------------------------------------------------------------------------


class TestBttsOutcome:
    @pytest.mark.parametrize(
        "home,away,expected",
        [
            (0, 0, BttsOutcome.NO),
            (1, 0, BttsOutcome.NO),
            (0, 2, BttsOutcome.NO),
            (1, 1, BttsOutcome.YES),
            (3, 2, BttsOutcome.YES),
            (5, 4, BttsOutcome.YES),
        ],
    )
    def test_scorelines(self, home, away, expected):
        assert btts_outcome(home, away) is expected

    @pytest.mark.parametrize("home,away", [(None, 1), (1, None), (None, None)])
    def test_missing_score_is_unknown_not_no(self, home, away):
        """
        The defect this prevents: a missing score counted as BTTS=NO is a free
        correct answer for any model predicting a low probability.
        """
        assert btts_outcome(home, away) is BttsOutcome.UNKNOWN

    def test_incomplete_fixture_is_unknown(self):
        assert btts_outcome(1, 1, completed=False) is BttsOutcome.UNKNOWN

    def test_zero_zero_is_a_real_negative_not_unknown(self):
        """0-0 is evidence. Conflating it with 'no data' was GG-001."""
        assert btts_outcome(0, 0) is BttsOutcome.NO

    def test_boolean_scores_rejected(self):
        # True > 0 is True in Python, so a bool would read as a 1-1 scoreline.
        assert btts_outcome(True, True) is BttsOutcome.UNKNOWN


# ---------------------------------------------------------------------------
# 2. Probability validation
# ---------------------------------------------------------------------------


class TestProbabilityValidation:
    @pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 1.0])
    def test_valid(self, value):
        assert validate_probability(value) == float(value)

    @pytest.mark.parametrize("value", [-0.001, 1.001, -1, 2])
    def test_out_of_range_rejected(self, value):
        with pytest.raises(ValueError):
            validate_probability(value)

    def test_nan_rejected(self):
        with pytest.raises(ValueError, match="NaN"):
            validate_probability(float("nan"))

    @pytest.mark.parametrize("value", [float("inf"), float("-inf")])
    def test_infinity_rejected(self, value):
        with pytest.raises(ValueError):
            validate_probability(value)

    def test_bool_rejected(self):
        with pytest.raises(ValueError, match="bool"):
            validate_probability(True)


# ---------------------------------------------------------------------------
# 3-4. Metrics against hand calculations
# ---------------------------------------------------------------------------


class TestBrierScore:
    def test_hand_calculated(self):
        # (0.8-1)^2 + (0.3-0)^2 = 0.04 + 0.09 = 0.13; /2 = 0.065
        predictions = [
            _prediction(0.8, BttsOutcome.YES),
            _prediction(0.3, BttsOutcome.NO),
        ]
        assert brier_score(predictions) == pytest.approx(0.065)

    def test_perfect_predictions_score_zero(self):
        predictions = [
            _prediction(1.0, BttsOutcome.YES),
            _prediction(0.0, BttsOutcome.NO),
        ]
        assert brier_score(predictions) == pytest.approx(0.0)

    def test_completely_wrong_predictions_score_one(self):
        predictions = [
            _prediction(0.0, BttsOutcome.YES),
            _prediction(1.0, BttsOutcome.NO),
        ]
        assert brier_score(predictions) == pytest.approx(1.0)

    def test_empty_set_is_none_not_zero(self):
        """0.0 would read as a perfect score. None means 'no data'."""
        assert brier_score([]) is None

    def test_unknown_outcomes_excluded(self):
        predictions = [
            _prediction(0.8, BttsOutcome.YES),
            _prediction(None, BttsOutcome.UNKNOWN, unevaluable_reason=UnevaluableReason.NO_RESULT),
        ]
        # (0.8-1)^2 = 0.04 over ONE scored prediction.
        assert brier_score(predictions) == pytest.approx(0.04)


class TestLogLoss:
    def test_hand_calculated(self):
        import math

        predictions = [
            _prediction(0.8, BttsOutcome.YES),
            _prediction(0.3, BttsOutcome.NO),
        ]
        expected = -(math.log(0.8) + math.log(0.7)) / 2
        assert log_loss(predictions) == pytest.approx(expected)

    def test_p_zero_wrong_is_finite_but_large(self):
        """
        -log(0) is infinite; a single such prediction would destroy the mean.
        Clipped inside the log only.
        """
        result = log_loss([_prediction(0.0, BttsOutcome.YES)])
        assert result is not None
        assert result > 30
        assert result != float("inf")

    def test_p_one_wrong_is_finite(self):
        result = log_loss([_prediction(1.0, BttsOutcome.NO)])
        assert result is not None
        assert result != float("inf")

    def test_reported_probability_is_never_modified(self):
        """The epsilon is a scoring device, not an edit to the model's output."""
        prediction = _prediction(0.0, BttsOutcome.YES)
        log_loss([prediction])
        assert prediction.probability == 0.0

    def test_perfect_predictions_near_zero(self):
        predictions = [
            _prediction(1.0, BttsOutcome.YES),
            _prediction(0.0, BttsOutcome.NO),
        ]
        assert log_loss(predictions) == pytest.approx(0.0, abs=1e-10)

    def test_epsilon_is_explicit(self):
        assert 0 < LOG_LOSS_EPSILON < 1e-6

    def test_empty_set_is_none(self):
        assert log_loss([]) is None


# ---------------------------------------------------------------------------
# 5. Calibration bins
# ---------------------------------------------------------------------------


class TestCalibration:
    def test_lower_bound_inclusive_upper_exclusive(self):
        table = calibration_table([_prediction(0.3, BttsOutcome.YES)], bin_count=10)
        assert table[3].count == 1  # 0.3 lands in [0.30, 0.40)
        assert table[2].count == 0  # not in [0.20, 0.30)

    def test_p_one_lands_in_final_bin(self):
        """
        Without the closed final bin, p=1.0 would vanish from calibration while
        still counting in the Brier score - two reports silently disagreeing.
        """
        table = calibration_table([_prediction(1.0, BttsOutcome.YES)], bin_count=10)
        assert table[-1].count == 1
        assert sum(b.count for b in table) == 1

    def test_p_zero_lands_in_first_bin(self):
        table = calibration_table([_prediction(0.0, BttsOutcome.NO)], bin_count=10)
        assert table[0].count == 1

    def test_empty_bins_retained_with_none_statistics(self):
        table = calibration_table([_prediction(0.55, BttsOutcome.YES)], bin_count=10)
        assert len(table) == 10
        assert table[0].count == 0
        assert table[0].mean_predicted is None
        assert table[0].observed_rate is None
        assert table[0].gap is None

    def test_gap_is_signed_observed_minus_predicted(self):
        # Two predictions at 0.5, one YES one NO -> observed 0.5, predicted 0.5
        table = calibration_table(
            [_prediction(0.5, BttsOutcome.YES), _prediction(0.5, BttsOutcome.NO)],
            bin_count=10,
        )
        assert table[5].gap == pytest.approx(0.0)

    def test_under_prediction_gives_positive_gap(self):
        table = calibration_table([_prediction(0.2, BttsOutcome.YES)], bin_count=10)
        assert table[2].gap == pytest.approx(0.8)

    def test_label_convention(self):
        assert CalibrationBin(0.3, 0.4, 0, None, None).label == "[0.30, 0.40)"
        assert CalibrationBin(0.9, 1.0, 0, None, None).label == "[0.90, 1.00]"

    def test_all_predictions_land_somewhere(self):
        predictions = [_prediction(i / 20, BttsOutcome.YES) for i in range(21)]
        table = calibration_table(predictions, bin_count=10)
        assert sum(b.count for b in table) == len(predictions)


# ---------------------------------------------------------------------------
# 6-10. Point-in-time / leakage
# ---------------------------------------------------------------------------


class TestStrictCutoff:
    """
    The strict `<` boundary, tested at one-second resolution. Each case is
    constructed so a leak would visibly change POISSON_V1's answer.
    """

    def test_one_second_before_is_included(self):
        target_kickoff = datetime(2020, 10, 1, 15, 0, tzinfo=UTC)
        dataset = _season(40) + [
            _match("prior", target_kickoff - timedelta(seconds=1), "1", "2"),
            _match("target", target_kickoff, "1", "2"),
        ]
        records = replay(dataset, PoissonV1Adapter(), targets=[dataset[-1]])
        assert any(r.event_id == "target" for r in records)
        target = next(r for r in records if r.event_id == "target")
        assert target.history_matches >= 1

    def test_exactly_at_kickoff_is_excluded(self):
        target_kickoff = datetime(2020, 10, 1, 15, 0, tzinfo=UTC)
        simultaneous = _match("sim", target_kickoff, "3", "4")
        target = _match("target", target_kickoff, "1", "2")
        records = replay([simultaneous, target], PoissonV1Adapter(), targets=[target])
        assert records[0].history_matches == 0

    def test_after_kickoff_is_excluded(self):
        target_kickoff = datetime(2020, 10, 1, 15, 0, tzinfo=UTC)
        later = _match("later", target_kickoff + timedelta(seconds=1), "3", "4")
        target = _match("target", target_kickoff, "1", "2")
        records = replay([later, target], PoissonV1Adapter(), targets=[target])
        assert records[0].history_matches == 0


class TestLeakage:
    def test_target_cannot_see_its_own_result(self):
        """
        The target is in the dataset. Its own result must not enter its history,
        which the strict cutoff guarantees without a special case.
        """
        target = _match("target", datetime(2021, 1, 1, 15, 0, tzinfo=UTC), "1", "2", 4, 4)
        dataset = _season(40) + [target]
        records = replay(dataset, PoissonV1Adapter(), targets=[target])
        assert all(r.event_id != "x" for r in records)
        # A 4-4 in its own history would inflate both venue averages sharply.
        record = records[0]
        assert record.probability is not None
        assert record.history_matches == len(
            [m for m in dataset if m.kickoff < target.kickoff]
        )

    def test_future_matches_do_not_contribute(self):
        target = _match("target", datetime(2020, 10, 1, 15, 0, tzinfo=UTC), "1", "2")
        future = [
            _match(f"f{i}", datetime(2021, 3, i + 1, 15, 0, tzinfo=UTC), "1", "2", 5, 5)
            for i in range(10)
        ]
        with_future = replay(_season(40) + [target] + future, PoissonV1Adapter(), targets=[target])
        without_future = replay(_season(40) + [target], PoissonV1Adapter(), targets=[target])
        assert with_future[0].probability == without_future[0].probability

    def test_later_same_season_matches_do_not_contribute(self):
        target = _match("target", datetime(2020, 10, 1, 15, 0, tzinfo=UTC), "1", "2")
        later_same_season = _match(
            "later", datetime(2021, 2, 1, 15, 0, tzinfo=UTC), "1", "2", 6, 6, season=2020
        )
        a = replay(_season(40) + [target, later_same_season], PoissonV1Adapter(), targets=[target])
        b = replay(_season(40) + [target], PoissonV1Adapter(), targets=[target])
        assert a[0].probability == b[0].probability

    def test_future_season_does_not_contribute(self):
        target = _match("target", datetime(2020, 10, 1, 15, 0, tzinfo=UTC), "1", "2")
        next_season = [
            _match(f"n{i}", datetime(2021, 9, i + 1, 15, 0, tzinfo=UTC), "1", "2", 7, 7, season=2021)
            for i in range(10)
        ]
        a = replay(_season(40) + [target] + next_season, PoissonV1Adapter(), targets=[target])
        b = replay(_season(40) + [target], PoissonV1Adapter(), targets=[target])
        assert a[0].probability == b[0].probability

    def test_cross_competition_does_not_contribute(self):
        target = _match("target", datetime(2020, 10, 1, 15, 0, tzinfo=UTC), "1", "2")
        other_league = [
            _match(
                f"o{i}",
                datetime(2020, 9, i + 1, 15, 0, tzinfo=UTC),
                "1",
                "2",
                8,
                8,
                competition="esp.1",
            )
            for i in range(10)
        ]
        a = replay(_season(40) + [target] + other_league, PoissonV1Adapter(), targets=[target])
        b = replay(_season(40) + [target], PoissonV1Adapter(), targets=[target])
        assert a[0].probability == b[0].probability

    def test_history_is_recomputed_per_target_not_reused(self):
        """
        Chronological replay: an earlier target must see strictly less evidence
        than a later one. A cached season-wide aggregate would make these equal.
        """
        dataset = _season(60)
        records = replay(dataset, PoissonV1Adapter())
        history_sizes = [r.history_matches for r in records]
        assert history_sizes == sorted(history_sizes)
        assert history_sizes[0] < history_sizes[-1]


# ---------------------------------------------------------------------------
# 11. The adapter calls production code
# ---------------------------------------------------------------------------


class TestAdapterUsesProductionModel:
    def test_adapter_calls_poisson_module(self, monkeypatch):
        """
        Proves the harness does not carry its own copy of the formula: patch the
        production function and the harness's answer must change with it.
        """
        calls = []

        def spy(**kwargs):
            calls.append(kwargs)
            return {"lambda_home": 1.0, "lambda_away": 1.0, "gg_probability": 0.4242}

        monkeypatch.setattr(poisson, "calculate_gg_probability", spy)

        dataset = _season(60)
        records = replay(dataset, PoissonV1Adapter(), targets=[dataset[-1]])
        assert calls, "adapter did not call poisson.calculate_gg_probability"
        assert records[0].probability == pytest.approx(0.4242)

    def test_adapter_passes_the_five_documented_inputs(self, monkeypatch):
        captured = {}

        def spy(**kwargs):
            captured.update(kwargs)
            return {"lambda_home": 1.0, "lambda_away": 1.0, "gg_probability": 0.5}

        monkeypatch.setattr(poisson, "calculate_gg_probability", spy)
        dataset = _season(60)
        replay(dataset, PoissonV1Adapter(), targets=[dataset[-1]])
        assert set(captured) == {
            "league_avg_goals",
            "home_goals_scored_home",
            "home_goals_conceded_home",
            "away_goals_scored_away",
            "away_goals_conceded_away",
        }

    def test_model_identity_is_explicit(self):
        adapter = PoissonV1Adapter()
        assert adapter.model_id == "POISSON_V1"
        assert adapter.model_version

    def test_registry_contains_poisson_v1(self):
        assert "POISSON_V1" in MODEL_REGISTRY
        assert get_model("POISSON_V1").model_id == "POISSON_V1"

    def test_unknown_model_fails_loudly(self):
        with pytest.raises(KeyError):
            get_model("DIXON_COLES")


class TestVenueBridge:
    def test_away_perspective_swaps_goals(self):
        """
        The flip that would not raise if reversed: it would just score every
        away side with its opponents' figures.
        """
        match = _match("e", datetime(2020, 9, 1, tzinfo=UTC), "home", "away", 3, 1)
        home_records = to_team_records([match], "home")
        away_records = to_team_records([match], "away")
        assert (home_records[0].goals_for, home_records[0].goals_against) == (3, 1)
        assert (away_records[0].goals_for, away_records[0].goals_against) == (1, 3)

    def test_uninvolved_team_gets_no_records(self):
        match = _match("e", datetime(2020, 9, 1, tzinfo=UTC), "home", "away")
        assert to_team_records([match], "someone_else") == []

    def test_league_records_are_home_perspective_only(self):
        """
        `derive_league_baseline` divides by 2*fixtures, so the dataset's single
        row per fixture must be emitted once, not twice.
        """
        matches = _season(8)
        records = to_league_records(matches)
        assert len(records) == len(matches)
        assert all(r.venue == Venue.HOME for r in records)

    def test_league_baseline_matches_hand_calculation(self):
        """
        Fixtures 2-1, 1-1, 0-2, 3-0 -> 10 goals over 8 team-games = 1.25.
        The documented worked example from domain/poisson_inputs.py.
        """
        from domain.poisson_inputs import derive_league_baseline

        base = datetime(2020, 9, 1, 15, 0, tzinfo=UTC)
        matches = [
            _match("a", base, "1", "2", 2, 1),
            _match("b", base + timedelta(days=1), "3", "4", 1, 1),
            _match("c", base + timedelta(days=2), "1", "3", 0, 2),
            _match("d", base + timedelta(days=3), "2", "4", 3, 0),
        ]
        baseline = derive_league_baseline(
            to_league_records(matches),
            target_kickoff=base + timedelta(days=10),
            competition="eng.1",
        )
        assert baseline.avg_goals_per_team == pytest.approx(1.25)
        assert baseline.fixtures == 4


# ---------------------------------------------------------------------------
# 12-13. Unevaluable targets and coverage
# ---------------------------------------------------------------------------


class TestUnevaluable:
    def test_first_fixture_of_all_history_is_unevaluable(self):
        """
        No prior matches means no inputs. POISSON_V1 must refuse, not be rescued
        with a league average - that is Epic 2C's decision to make and measure.
        """
        first = _match("first", datetime(2020, 9, 1, 15, 0, tzinfo=UTC), "1", "2")
        records = replay([first], PoissonV1Adapter())
        assert records[0].probability is None
        assert records[0].unevaluable_reason is UnevaluableReason.INSUFFICIENT_HISTORY

    def test_early_season_target_stays_unevaluable(self):
        """
        A team with no prior HOME match cannot have a home scoring rate. The
        away side's history does not substitute for it.
        """
        base = datetime(2020, 9, 1, 15, 0, tzinfo=UTC)
        dataset = [
            _match("a", base, "3", "4"),
            _match("b", base + timedelta(days=1), "4", "3"),
            _match("target", base + timedelta(days=2), "1", "2"),
        ]
        records = replay(dataset, PoissonV1Adapter(), targets=[dataset[-1]])
        assert records[0].unevaluable_reason is UnevaluableReason.INSUFFICIENT_HISTORY
        assert "home_goals_scored_home" in (records[0].detail or "")

    def test_no_result_target_is_unevaluable_not_scored_as_no(self):
        cancelled = HistoricalMatch(
            event_id="cancelled",
            competition="eng.1",
            season=2019,
            kickoff=datetime(2020, 4, 1, 15, 0, tzinfo=UTC),
            home_team_id="1",
            away_team_id="2",
            completed=False,
            status="STATUS_CANCELED",
            season_phase="regular-season",
        )
        records = replay(_season(40) + [cancelled], PoissonV1Adapter(), targets=[cancelled])
        assert records[0].outcome is BttsOutcome.UNKNOWN
        assert records[0].unevaluable_reason is UnevaluableReason.NO_RESULT
        assert records[0].probability is None

    def test_ineligible_target_is_reported_not_scored(self):
        playoff = _match(
            "playoff",
            datetime(2021, 5, 30, 15, 0, tzinfo=UTC),
            "1",
            "2",
            phase="promotion-playoff-finals",
        )
        records = replay(_season(40) + [playoff], PoissonV1Adapter(), targets=[playoff])
        assert records[0].unevaluable_reason is UnevaluableReason.NOT_MODEL_ELIGIBLE

    def test_prediction_record_requires_a_reason_when_unscored(self):
        with pytest.raises(ValueError, match="no probability and no reason"):
            _prediction(None, BttsOutcome.YES)

    def test_prediction_record_refuses_probability_and_reason_together(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            _prediction(
                0.5,
                BttsOutcome.YES,
                unevaluable_reason=UnevaluableReason.NO_RESULT,
            )


class TestCoverage:
    def test_coverage_is_scored_over_targets(self):
        predictions = [
            _prediction(0.6, BttsOutcome.YES),
            _prediction(0.4, BttsOutcome.NO),
            _prediction(
                None,
                BttsOutcome.NO,
                unevaluable_reason=UnevaluableReason.INSUFFICIENT_HISTORY,
            ),
            _prediction(
                None,
                BttsOutcome.UNKNOWN,
                unevaluable_reason=UnevaluableReason.NO_RESULT,
            ),
        ]
        summary = summarise(predictions, model_id="M", model_version="1")
        assert summary.targets == 4
        assert summary.scored == 2
        assert summary.coverage == pytest.approx(0.5)

    def test_unevaluable_reasons_are_itemised(self):
        predictions = [
            _prediction(
                None,
                BttsOutcome.NO,
                unevaluable_reason=UnevaluableReason.INSUFFICIENT_HISTORY,
            ),
            _prediction(
                None,
                BttsOutcome.UNKNOWN,
                unevaluable_reason=UnevaluableReason.NO_RESULT,
            ),
        ]
        summary = summarise(predictions, model_id="M", model_version="1")
        assert summary.unevaluable == {"INSUFFICIENT_HISTORY": 1, "NO_RESULT": 1}

    def test_empty_evaluation_set(self):
        summary = summarise([], model_id="M", model_version="1")
        assert summary.targets == 0
        assert summary.scored == 0
        assert summary.coverage is None
        assert summary.brier is None
        assert summary.log_loss is None
        assert len(summary.calibration) == 10

    def test_coverage_and_quality_are_separate(self):
        """
        A perfect Brier over one of a hundred targets is not a good model. Both
        numbers must survive into the summary.
        """
        predictions = [_prediction(1.0, BttsOutcome.YES)] + [
            _prediction(
                None,
                BttsOutcome.YES,
                unevaluable_reason=UnevaluableReason.INSUFFICIENT_HISTORY,
                event_id=f"e{i}",
            )
            for i in range(99)
        ]
        summary = summarise(predictions, model_id="M", model_version="1")
        assert summary.brier == pytest.approx(0.0)
        assert summary.coverage == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# 14-15. Determinism and artifacts
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_replay_ordering_is_stable(self):
        dataset = _season(40)
        a = replay(dataset, PoissonV1Adapter())
        b = replay(list(reversed(dataset)), PoissonV1Adapter())
        assert [r.event_id for r in a] == [r.event_id for r in b]

    def test_probabilities_are_reproducible(self):
        dataset = _season(40)
        a = replay(dataset, PoissonV1Adapter())
        b = replay(dataset, PoissonV1Adapter())
        assert [r.probability for r in a] == [r.probability for r in b]

    def test_artifacts_are_byte_identical_across_runs(self, tmp_path):
        dataset = _season(40)
        run = evaluate(dataset, PoissonV1Adapter())
        first = tmp_path / "a"
        second = tmp_path / "b"
        write_artifacts([run], first, dataset_checksum="abc")
        write_artifacts([evaluate(dataset, PoissonV1Adapter())], second, dataset_checksum="abc")
        for name in ("evaluation_predictions.jsonl", "evaluation_summary.json", "calibration.json"):
            assert (first / name).read_bytes() == (second / name).read_bytes()

    def test_artifacts_record_schema_and_dataset_checksum(self, tmp_path):
        import json

        run = evaluate(_season(40), PoissonV1Adapter())
        write_artifacts([run], tmp_path, dataset_checksum="deadbeef")
        summary = json.loads((tmp_path / "evaluation_summary.json").read_text())
        assert summary["dataset_checksum"] == "deadbeef"
        assert summary["schema_version"]

    def test_dataset_is_not_mutated_by_evaluation(self):
        dataset = _season(40)
        before = [(m.event_id, m.home_goals, m.away_goals) for m in dataset]
        replay(dataset, PoissonV1Adapter())
        after = [(m.event_id, m.home_goals, m.away_goals) for m in dataset]
        assert before == after


# ---------------------------------------------------------------------------
# 16. Reference predictor
# ---------------------------------------------------------------------------


class TestReferencePredictor:
    def test_uses_only_prior_matches(self):
        base = datetime(2020, 9, 1, 15, 0, tzinfo=UTC)
        # 20 prior fixtures, all 1-1 (BTTS rate 1.0), then a 0-0 target.
        history = [
            _match(f"h{i}", base + timedelta(days=i), "1", "2", 1, 1) for i in range(20)
        ]
        target = _match("t", base + timedelta(days=100), "1", "2", 0, 0)
        records = replay(history + [target], ReferenceBaseRateAdapter(), targets=[target])
        assert records[0].probability == pytest.approx(1.0)

    def test_declines_below_minimum_history(self):
        base = datetime(2020, 9, 1, 15, 0, tzinfo=UTC)
        history = [_match(f"h{i}", base + timedelta(days=i), "1", "2") for i in range(5)]
        target = _match("t", base + timedelta(days=100), "1", "2")
        records = replay(history + [target], ReferenceBaseRateAdapter(min_matches=20), targets=[target])
        assert records[0].unevaluable_reason is UnevaluableReason.INSUFFICIENT_HISTORY

    def test_is_identified_as_a_reference_not_a_model(self):
        assert ReferenceBaseRateAdapter().model_id == "REFERENCE_BASE_RATE"

    def test_does_not_see_the_season_final_rate(self):
        """
        The reference must obey the same cutoff. Future 0-0s must not drag its
        estimate down.
        """
        base = datetime(2020, 9, 1, 15, 0, tzinfo=UTC)
        history = [
            _match(f"h{i}", base + timedelta(days=i), "1", "2", 1, 1) for i in range(20)
        ]
        target = _match("t", base + timedelta(days=50), "1", "2", 1, 1)
        future = [
            _match(f"f{i}", base + timedelta(days=100 + i), "1", "2", 0, 0)
            for i in range(50)
        ]
        records = replay(history + [target] + future, ReferenceBaseRateAdapter(), targets=[target])
        assert records[0].probability == pytest.approx(1.0)


class TestBreakdowns:
    def test_breakdown_by_competition(self):
        dataset = _season(40) + [
            _match(
                f"esp{i}",
                datetime(2020, 9, i + 1, 15, 0, tzinfo=UTC),
                "10",
                "11",
                competition="esp.1",
            )
            for i in range(20)
        ]
        run = evaluate(dataset, PoissonV1Adapter())
        breakdown = run.breakdown("competition")
        assert set(breakdown) == {"eng.1", "esp.1"}

    def test_breakdown_by_evidence_bucket(self):
        run = evaluate(_season(60), PoissonV1Adapter())
        breakdown = run.breakdown("evidence")
        assert "0" in breakdown  # the earliest targets had no home evidence

    def test_unknown_breakdown_key_raises(self):
        run = evaluate(_season(20), PoissonV1Adapter())
        with pytest.raises(ValueError, match="unknown breakdown key"):
            run.breakdown("matchweek")
