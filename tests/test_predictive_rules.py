import math
from datetime import timedelta

import pytest

from agent.detect.predictive_rules import (MAX_CONFIDENCE, PredictiveConfig, evaluate_predictive, memory_trend,
                                           predict_oom, predict_restart_loop)
from agent.detect.reactive_rules import FindingKind, Severity
from tests.helpers import MIB, T0, mem_series, snap

KIB = 1024


# ---------------- memory trend ----------------
def test_clean_linear_growth_is_measured_exactly():
    t = memory_trend(mem_series(n=8, rate=200 * KIB))
    assert math.isclose(t.slope_bytes_per_s, 200 * KIB, rel_tol=1e-6) and math.isclose(t.r2, 1.0)
    assert t.n_points == 8 and t.span_seconds == 70 and t.limit_bytes == 64 * MIB


def test_eta_is_time_to_limit_from_latest_sample():
    h = mem_series(n=8, start=20 * MIB, rate=200 * KIB, limit=64 * MIB)
    t = memory_trend(h)
    last = 20 * MIB + 200 * KIB * 70
    assert math.isclose(t.eta_seconds, (64 * MIB - last) / (200 * KIB))


def test_not_enough_samples_gives_no_trend():
    assert memory_trend(mem_series(n=4)) is None


def test_too_short_a_span_gives_no_trend():
    assert memory_trend(mem_series(n=6, step=5)) is None  # 25s < 40s


def test_polling_duplicates_do_not_count_as_evidence():
    # 3 distinct samples polled 4x each must NOT pass a 5-sample minimum
    assert memory_trend(mem_series(n=3, step=30, poll_dupes=4)) is None
    assert memory_trend(mem_series(n=8, poll_dupes=4)).n_points == 8


def test_no_limit_means_no_prediction():
    assert memory_trend(mem_series(limit=None)) is None
    assert predict_oom(mem_series(limit=None)) is None


def test_missing_metrics_gives_no_trend():
    assert memory_trend([snap() for _ in range(10)]) is None


def test_flat_memory_is_not_a_trend():
    assert memory_trend(mem_series(rate=0)) is None


def test_restart_resets_the_fit_segment():
    h = mem_series(n=12, rate=200 * KIB, restarts={6: 1})  # restart_count changes at sample 6
    assert memory_trend(h).n_points == 6  # only samples from the restart on


def test_fit_does_not_straddle_a_restart():
    pre = mem_series(n=6, start=20 * MIB, rate=500 * KIB)
    post = mem_series(n=6, start=20 * MIB, rate=0 + 1, restarts={0: 1})
    # shift post timestamps after pre
    shifted = [s.__class__(**{**s.__dict__, "metrics_timestamp": s.metrics_timestamp + timedelta(seconds=100),
                              "observed_at": s.observed_at + timedelta(seconds=100)}) for s in post]
    assert predict_oom(pre + shifted) is None  # post-restart is flat; the pre-restart ramp must not leak in


# ---------------- OOM prediction ----------------
def test_steady_leak_toward_limit_warns_before_failure():
    f = predict_oom(mem_series(n=8, start=20 * MIB, rate=200 * KIB))
    assert f.kind is FindingKind.PREDICTED_OOM and f.severity is Severity.WARNING and f.predictive
    assert 0 < f.confidence <= MAX_CONFIDENCE and "limit in" in f.detail


def test_leak_too_slow_to_matter_within_horizon_is_ignored():
    assert predict_oom(mem_series(n=8, rate=2 * KIB)) is None  # would take hours


def test_horizon_is_configurable():
    h = mem_series(n=8, start=20 * MIB, rate=100 * KIB)  # ~ (64-27)MiB/100KiB/s ≈ 380s
    assert predict_oom(h) is None
    assert predict_oom(h, PredictiveConfig(oom_horizon_seconds=600)) is not None


def test_gc_sawtooth_noise_is_rejected_by_fit_quality():
    # Upward slope AND the limit within the horizon, so only fit quality can reject it (R^2 ~ 0.5).
    noisy = mem_series(n=10, start=45 * MIB, rate=200 * KIB, noise=[0, 6 * MIB, -5 * MIB, 4 * MIB, -6 * MIB])
    t = memory_trend(noisy)
    assert t.slope_bytes_per_s > 100 * KIB and t.eta_seconds < 300 and t.r2 < 0.85
    assert predict_oom(noisy) is None


def test_modest_noise_around_a_real_trend_still_fires():
    h = mem_series(n=12, start=20 * MIB, rate=250 * KIB, noise=[0, 30 * KIB, -30 * KIB, 15 * KIB])
    assert predict_oom(h) is not None


def test_confidence_grows_with_evidence_and_is_capped():
    few = predict_oom(mem_series(n=6, rate=300 * KIB)).confidence
    many = predict_oom(mem_series(n=14, start=20 * MIB, rate=200 * KIB)).confidence
    assert few < many <= MAX_CONFIDENCE


def test_already_at_limit_has_zero_eta():
    t = memory_trend(mem_series(n=6, start=60 * MIB, rate=1 * MIB, limit=64 * MIB))
    assert t.eta_seconds == 0 and predict_oom(mem_series(n=6, start=60 * MIB, rate=1 * MIB)) is not None


def test_shrinking_memory_never_warns():
    assert predict_oom(mem_series(n=8, start=50 * MIB, rate=-100 * KIB)) is None


def test_finding_identity_is_stable_per_pod():
    a, b = predict_oom(mem_series(n=8)), predict_oom(mem_series(n=9))
    assert a.dedupe_key == b.dedupe_key == "u1:PREDICTED_OOM"


# ---------------- restart velocity ----------------
def restarts(counts, step=60):
    return [snap(restart_count=c, observed_at=T0 + timedelta(seconds=i * step)) for i, c in enumerate(counts)]


def test_two_restarts_in_window_warns_before_reactive_threshold():
    f = predict_restart_loop(restarts([0, 0, 1, 1, 2]))
    assert f.kind is FindingKind.PREDICTED_CRASH_LOOP and f.confidence == pytest.approx(0.8)
    assert "1 more reaches the restart threshold (3)" in f.detail


def test_single_restart_is_not_a_pattern():
    assert predict_restart_loop(restarts([0, 0, 1, 1])) is None


def test_old_restarts_outside_window_are_ignored():
    h = restarts([0, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2], step=120)  # restarts 20+ min ago
    assert predict_restart_loop(h, PredictiveConfig(restart_window_seconds=600)) is None


def test_defers_to_reactive_once_threshold_reached():
    assert predict_restart_loop(restarts([0, 1, 2, 3])) is None


def test_empty_history_is_safe():
    assert predict_restart_loop([]) is None and evaluate_predictive([]) == []


def test_evaluate_predictive_combines_rules():
    h = mem_series(n=8, rate=250 * KIB)
    assert {f.kind for f in evaluate_predictive(h)} == {FindingKind.PREDICTED_OOM}
