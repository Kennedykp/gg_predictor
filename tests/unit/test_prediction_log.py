"""
The ledger contract (Epic 2G).

These tests exist to stop the record from lying. A prediction log is only worth
keeping if every field means exactly one thing months later, so each invariant
here is paired with the specific way the record could otherwise mislead.
"""

from datetime import datetime, timezone

import pytest

from domain.prediction_log import (
    FILTER_OUTCOMES,
    FINGERPRINTED_KEYS,
    LEDGER_SCHEMA_VERSION,
    LedgerRecord,
    LivePredictionStatus,
    OddsProvenance,
    OddsSnapshot,
    build_odds_snapshot,
    build_provenance,
    classify_status,
    config_fingerprint,
    fingerprinted_config,
    from_result_dict,
    to_json_dict,
    to_jsonl_line,
)

NOW = datetime(2026, 8, 17, 6, 30, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)


def provenance():
    return build_provenance(
        config_values={"EDGE_THRESHOLD": 0.05, "MIN_ODDS": 1.6},
        code_revision="abc1234",
    )


def record(**overrides):
    fields = dict(
        prediction_id="p1",
        run_id="r1",
        created_at=NOW,
        provenance=provenance(),
        fixture_id="740123",
        competition="eng.1",
        home_team_id="359",
        away_team_id="360",
        status=LivePredictionStatus.SCORED,
        probability=0.62,
    )
    fields.update(overrides)
    return LedgerRecord(**fields)


# ---------------------------------------------------------------------------
# A record is either scored or refused
# ---------------------------------------------------------------------------
class TestScoredXorRefused:
    """
    A probability beside a refusal reason is two contradictory claims. The record
    refuses to hold both, the same way `domain.evaluation.PredictionRecord` does.
    """

    def test_scored_requires_a_probability(self):
        with pytest.raises(ValueError, match="requires a probability"):
            record(status=LivePredictionStatus.SCORED, probability=None)

    @pytest.mark.parametrize(
        "status",
        [
            LivePredictionStatus.NO_TEAM_STATS,
            LivePredictionStatus.NO_POINT_IN_TIME_INPUTS,
            LivePredictionStatus.MODEL_RETURNED_NONE,
        ],
    )
    def test_refusal_cannot_carry_a_probability(self, status):
        with pytest.raises(ValueError, match="cannot carry a probability"):
            record(status=status, probability=0.5)

    @pytest.mark.parametrize("status", list(LivePredictionStatus))
    def test_every_status_is_constructible(self, status):
        scored = status is LivePredictionStatus.SCORED
        assert record(status=status, probability=0.5 if scored else None)

    def test_a_refused_fixture_is_still_a_record(self):
        """
        Refusals are recorded, not skipped. "The system was asked and declined"
        is the only evidence that distinguishes a quiet day from a broken feed,
        and coverage cannot be measured without it.
        """
        refused = record(
            status=LivePredictionStatus.NO_POINT_IN_TIME_INPUTS,
            probability=None,
            rejection_reasons=("Point-in-time model inputs unavailable: league_avg_goals",),
        )
        assert to_json_dict(refused)["status"] == "NO_POINT_IN_TIME_INPUTS"
        assert to_json_dict(refused)["probability"] is None


class TestProbabilityRange:
    @pytest.mark.parametrize("value", [-0.01, 1.01, 42.0])
    def test_out_of_range_is_refused(self, value):
        with pytest.raises(ValueError, match="out of range"):
            record(probability=value)

    @pytest.mark.parametrize("value", [0.0, 1.0])
    def test_the_boundaries_are_legal(self, value):
        """
        0.0 is a real POISSON_V1 output on a degenerate sample (GG-028) and 1.0
        is its mirror. Rejecting either would refuse to record the predictions
        most worth inspecting.
        """
        assert record(probability=value).probability == value


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
class TestTimezoneDiscipline:
    """
    A naive datetime compares against local time. On this UTC+1 machine a 23:30Z
    kickoff would land on the wrong matchday (GG-014), and a ledger that
    mis-dates a prediction cannot be graded against the right result.
    """

    def test_naive_created_at_is_refused(self):
        with pytest.raises(ValueError, match="created_at must be timezone-aware"):
            record(created_at=datetime(2026, 8, 17, 6, 30))

    def test_naive_kickoff_is_refused(self):
        with pytest.raises(ValueError, match="kickoff must be timezone-aware"):
            record(kickoff=datetime(2026, 8, 17, 14, 0))

    def test_absent_kickoff_is_allowed(self):
        """
        `espn.parse_kickoff` returns None on an unparseable date rather than
        guessing. The record keeps `kickoff_raw` so nothing is lost.
        """
        kept = record(kickoff=None, kickoff_raw="not-a-date")
        assert to_json_dict(kept)["kickoff"] is None
        assert to_json_dict(kept)["kickoff_raw"] == "not-a-date"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
class TestIdentity:
    @pytest.mark.parametrize(
        "field_name", ["prediction_id", "run_id", "fixture_id"]
    )
    def test_empty_identity_is_refused(self, field_name):
        """An unidentifiable prediction cannot be graded, so it cannot exist."""
        with pytest.raises(ValueError, match=f"{field_name} must not be empty"):
            record(**{field_name: ""})

    def test_fixture_id_is_coerced_to_string(self):
        """
        ESPN ids arrive as ints and strings. `"740123" != 740123` would split one
        fixture's records across two identities and break settlement joins.
        """
        built = from_result_dict(
            {
                "fixture_id": 740123,
                "league_id": "eng.1",
                "home_team_id": 359,
                "away_team_id": 360,
                "gg_probability": 0.6,
            },
            prediction_id="p",
            run_id="r",
            created_at=NOW,
            provenance=provenance(),
        )
        assert built.fixture_id == "740123"
        assert built.home_team_id == "359"


# ---------------------------------------------------------------------------
# Filter outcome: three values, never a boolean
# ---------------------------------------------------------------------------
class TestFilterOutcome:
    def test_the_three_states_are_accepted(self):
        for outcome in ("PASSED", "FAILED", "UNEVALUATED"):
            assert record(filter_outcome=outcome).filter_outcome == outcome

    def test_an_unknown_outcome_is_refused(self):
        with pytest.raises(ValueError, match="filter_outcome must be one of"):
            record(filter_outcome="MAYBE")

    def test_a_boolean_is_refused(self):
        """
        GG-002 survived because "passed" and "never evaluated" were the same
        `False`. Storing a bool here would rebuild that ambiguity in the archive,
        where it would be permanent.
        """
        with pytest.raises(ValueError, match="filter_outcome must be one of"):
            record(filter_outcome=False)

    def test_the_string_set_matches_the_real_enum(self):
        """
        The contract cannot import `domain.filter_evaluation` (it imports
        `filters`), so the set is duplicated as strings - and pinned here, where
        importing the real enum is allowed. Adding a fourth state without
        updating the ledger fails this test.
        """
        from domain.filter_evaluation import FilterOutcome

        assert FILTER_OUTCOMES == {outcome.value for outcome in FilterOutcome}


# ---------------------------------------------------------------------------
# Odds
# ---------------------------------------------------------------------------
class TestOddsSnapshot:
    def test_absent_cannot_carry_a_price(self):
        with pytest.raises(ValueError, match="ABSENT cannot carry a price"):
            OddsSnapshot(provenance=OddsProvenance.ABSENT, price=2.5)

    def test_a_priced_snapshot_requires_a_price(self):
        with pytest.raises(ValueError, match="requires a price"):
            OddsSnapshot(provenance=OddsProvenance.PARTIAL_NO_BOOKMAKER)

    def test_naive_observed_at_is_refused(self):
        with pytest.raises(ValueError, match="observed_at must be timezone-aware"):
            OddsSnapshot(
                provenance=OddsProvenance.PARTIAL_NO_BOOKMAKER,
                price=2.5,
                observed_at=datetime(2026, 8, 17, 6, 0),
            )

    def test_todays_prices_are_marked_partial(self):
        """
        `odds_api.get_btts_odds` returns a bare float: the bookmaker and the
        observation time are discarded before any caller sees them. The record
        says so rather than leaving two nulls that look like an omission.
        """
        snapshot = build_odds_snapshot(
            {"odds": 2.50, "implied_probability": 0.40, "edge": 0.22}
        )
        assert snapshot.provenance is OddsProvenance.PARTIAL_NO_BOOKMAKER
        assert snapshot.bookmaker is None
        assert snapshot.observed_at is None

    def test_no_price_is_absent(self):
        assert build_odds_snapshot({"odds": None}).provenance is OddsProvenance.ABSENT

    def test_a_zero_edge_survives(self):
        """
        GG-007: `shared/odds.py` writes `round(edge, 4) if edge else None`, which
        turns a genuine 0.0 edge into null and makes "exactly break-even"
        indistinguishable from "no odds". A calibration study would silently drop
        those rows. `is not None` here keeps them.
        """
        snapshot = build_odds_snapshot(
            {"odds": 2.0, "implied_probability": 0.5, "edge": 0.0}
        )
        assert snapshot.edge == 0.0
        assert snapshot.edge is not None


# ---------------------------------------------------------------------------
# Status recovery
# ---------------------------------------------------------------------------
class TestClassifyStatus:
    """
    Each refusal path in `main.process_fixture` maps to one named status. The
    reason strings are pinned here because this is the coupling accepted in
    exchange for not editing the function that produces predictions.
    """

    def test_a_probability_means_scored(self):
        assert classify_status({"gg_probability": 0.6}) is LivePredictionStatus.SCORED

    def test_missing_team_stats(self):
        assert (
            classify_status(
                {
                    "gg_probability": None,
                    "rejection_reasons": ["Missing or unreliable team stats"],
                }
            )
            is LivePredictionStatus.NO_TEAM_STATS
        )

    def test_missing_point_in_time_inputs(self):
        assert (
            classify_status(
                {
                    "gg_probability": None,
                    "rejection_reasons": [
                        "Point-in-time model inputs unavailable: league_avg_goals"
                    ],
                }
            )
            is LivePredictionStatus.NO_POINT_IN_TIME_INPUTS
        )

    def test_model_declined(self):
        assert (
            classify_status(
                {
                    "gg_probability": None,
                    "rejection_reasons": ["Failed to calculate probability"],
                }
            )
            is LivePredictionStatus.MODEL_RETURNED_NONE
        )

    def test_an_unrecognised_reason_falls_back_conservatively(self):
        """
        A reworded reason must not crash capture. `MODEL_RETURNED_NONE` is the
        honest fallback: no probability was produced, and the specific cause is
        no longer legible.
        """
        assert (
            classify_status(
                {"gg_probability": None, "rejection_reasons": ["something new"]}
            )
            is LivePredictionStatus.MODEL_RETURNED_NONE
        )

    def test_a_zero_probability_is_scored_not_refused(self):
        """
        GG-028 produces an exact 0.0. Truthiness would read that as "no
        probability" and file the system's most extreme claim as a refusal.
        """
        assert classify_status({"gg_probability": 0.0}) is LivePredictionStatus.SCORED


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
class TestConfigFingerprint:
    def test_the_same_configuration_hashes_identically(self):
        values = {"EDGE_THRESHOLD": 0.05, "MIN_ODDS": 1.6}
        assert config_fingerprint(values) == config_fingerprint(dict(values))

    def test_a_changed_threshold_changes_the_fingerprint(self):
        """
        The point of the field. A threshold edit with no version bump still shows
        up as a discontinuity in the ledger.
        """
        before = config_fingerprint({"EDGE_THRESHOLD": 0.05, "MIN_ODDS": 1.6})
        after = config_fingerprint({"EDGE_THRESHOLD": 0.08, "MIN_ODDS": 1.6})
        assert before != after

    def test_dict_ordering_does_not_change_the_fingerprint(self):
        a = config_fingerprint({"MIN_ODDS": 1.6, "EDGE_THRESHOLD": 0.05})
        b = config_fingerprint({"EDGE_THRESHOLD": 0.05, "MIN_ODDS": 1.6})
        assert a == b

    def test_a_live_threshold_edit_is_observed(self, monkeypatch):
        """
        Measured from `config`, not declared. Read on every call, so a patched
        threshold is reflected instead of frozen at import time.
        """
        import config

        before = config_fingerprint()
        monkeypatch.setattr(config, "EDGE_THRESHOLD", 0.42)
        assert config_fingerprint() != before

    def test_a_league_change_is_observed(self, monkeypatch):
        """Adding a league changes which fixtures are predicted at all."""
        import config

        before = config_fingerprint()
        monkeypatch.setitem(config.ALLOWED_LEAGUES, "ned.1", "Eredivisie")
        assert config_fingerprint() != before

    def test_every_recommendation_threshold_is_covered(self):
        """
        Each of these can change a published recommendation with no model change.
        Omitting one would leave a real behaviour change invisible.
        """
        for key in ("EDGE_THRESHOLD", "MIN_ODDS", "MIN_AVG_GOALS", "MAX_CLEAN_SHEET_PCT"):
            assert key in FINGERPRINTED_KEYS

    def test_real_config_is_readable(self):
        values = fingerprinted_config()
        assert values["EDGE_THRESHOLD"] is not None
        assert isinstance(values["ALLOWED_LEAGUES"], list)


class TestProvenanceBlock:
    def test_all_four_version_axes_are_present(self):
        """
        Four independent things change what is published. Recording only "the
        model" would leave threshold and provider changes unexplained.
        """
        block = to_json_dict(record())["provenance"]
        for key in (
            "model_version",
            "filter_version",
            "decision_version",
            "data_source_version",
        ):
            assert block[key]

    def test_schema_version_is_serialised(self):
        assert to_json_dict(record())["schema_version"] == LEDGER_SCHEMA_VERSION

    def test_model_version_matches_the_evaluation_harness(self):
        """
        The live path and the offline harness must be able to state they ran the
        same model. If these drift, no comparison between them is meaningful.
        """
        from evaluation_harness import PoissonV1Adapter

        adapter = PoissonV1Adapter()
        block = to_json_dict(record())["provenance"]
        assert block["model_id"] == adapter.model_id
        assert block["model_version"] == adapter.model_version


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
class TestSerialisation:
    def test_key_order_is_stable(self):
        """
        Identical records must produce identical bytes, so a diff between two
        ledger files shows real change and nothing else.
        """
        first = to_jsonl_line(record())
        second = to_jsonl_line(record())
        assert first == second
        assert list(to_json_dict(record())) == list(to_json_dict(record(probability=0.9)))

    def test_one_record_is_one_line(self):
        line = to_jsonl_line(record(rejection_reasons=("a", "b")))
        assert "\n" not in line

    def test_round_trips_through_json(self):
        import json

        parsed = json.loads(to_jsonl_line(record(kickoff=KICKOFF)))
        assert parsed["fixture_id"] == "740123"
        assert parsed["probability"] == 0.62
        assert parsed["kickoff"] == KICKOFF.isoformat()

    def test_the_record_carries_no_outcome_field(self):
        """
        A prediction is immutable. Grading is a later fact about the same
        fixture; writing it back into this record would erase what was actually
        claimed at prediction time - the whole point of the ledger.
        """
        keys = set(to_json_dict(record()))
        assert "outcome" not in keys
        assert "result" not in keys
        assert "settled" not in keys

    def test_samples_travel_with_the_prediction(self):
        """
        A lambda from one match and one from nineteen are not equally
        trustworthy, and the float alone cannot say which it is.
        """
        built = from_result_dict(
            {
                "fixture_id": "1",
                "league_id": "eng.1",
                "home_team_id": "a",
                "away_team_id": "b",
                "gg_probability": 0.5,
                "model_input_samples": {"home": 3, "away": 19, "league": 120},
            },
            prediction_id="p",
            run_id="r",
            created_at=NOW,
            provenance=provenance(),
        )
        payload = to_json_dict(built)
        assert (payload["home_sample"], payload["away_sample"]) == (3, 19)
        assert payload["league_sample"] == 120


class TestAdapterIsReadOnly:
    def test_the_result_dict_is_not_mutated(self):
        """
        The core guarantee, at the contract level: adapting a result cannot
        change it. Anything else would make capture part of prediction.
        """
        import copy

        result = {
            "fixture_id": "1",
            "league_id": "eng.1",
            "home_team_id": "a",
            "away_team_id": "b",
            "gg_probability": 0.5,
            "decision": "BET",
            "rejection_reasons": [],
            "model_input_samples": {"home": 5, "away": 6, "league": 60},
        }
        before = copy.deepcopy(result)
        from_result_dict(
            result,
            prediction_id="p",
            run_id="r",
            created_at=NOW,
            provenance=provenance(),
        )
        assert result == before

    def test_the_record_is_frozen(self):
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            record().__setattr__("probability", 0.99)
