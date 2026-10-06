"""Leading indicators: warn BEFORE the failure, with a confidence score.

Pure functions over a pod's history (list[PodSnapshot], oldest first). No I/O.

The confidence here is a transparent HEURISTIC of how trustworthy the trend is
(fit quality x amount of evidence). It is not a calibrated probability, and the
policy engine treats predictive incidents more strictly than reactive ones.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.detect.reactive_rules import Finding, FindingKind, Severity
from agent.monitor.models import PodSnapshot

MAX_CONFIDENCE = 0.95


@dataclass(frozen=True)
class PredictiveConfig:
    # memory trend
    min_points: int = 5                  # distinct metric samples needed
    min_span_seconds: float = 40.0       # ...spread over at least this long
    full_confidence_points: int = 10     # evidence saturates here
    min_r2: float = 0.85                 # linear fit must be this good (filters GC sawtooth noise)
    min_slope_bytes_per_s: float = 1024  # ignore flat / negligible growth
    oom_horizon_seconds: float = 300.0   # warn if the limit will be hit within this long
    # restart velocity
    restart_window_seconds: float = 600.0
    restart_min_events: int = 2
    restart_threshold: int = 3           # the reactive RESTART_THRESHOLD this is meant to precede


@dataclass(frozen=True)
class MemoryTrend:
    slope_bytes_per_s: float
    r2: float
    n_points: int
    span_seconds: float
    current_bytes: int
    limit_bytes: int
    eta_seconds: float  # until the limit at the current slope (0 if already there)


def _segment_since_last_restart(history: list[PodSnapshot]) -> list[PodSnapshot]:
    """A container restart resets memory; fitting across it would manufacture a fake trend."""
    start = 0
    for i in range(1, len(history)):
        if history[i].restart_count != history[i - 1].restart_count:
            start = i
    return history[start:]


def memory_trend(history: list[PodSnapshot], cfg: PredictiveConfig = PredictiveConfig()) -> MemoryTrend | None:
    """Least-squares line through memory samples since the last restart. None if there isn't enough
    trustworthy data (too few points, too short, no limit, flat)."""
    seg = _segment_since_last_restart(history)
    by_ts: dict = {}
    for s in seg:  # polling is faster than metrics-server: dedupe by SAMPLE time, or we'd fit repeated points
        if s.memory_bytes is not None and s.metrics_timestamp is not None:
            by_ts.setdefault(s.metrics_timestamp, s.memory_bytes)
    if len(by_ts) < cfg.min_points:
        return None
    pts = sorted(by_ts.items())
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() for t, _ in pts]
    ys = [float(m) for _, m in pts]
    span = xs[-1]
    limit = seg[-1].memory_limit_bytes
    if span < cfg.min_span_seconds or not limit:
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None  # perfectly flat: no trend
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy)
    current = int(ys[-1])
    eta = 0.0 if current >= limit else (limit - current) / slope if slope > 0 else float("inf")
    return MemoryTrend(slope, r2, n, span, current, int(limit), eta)


def predict_oom(history: list[PodSnapshot], cfg: PredictiveConfig = PredictiveConfig()) -> Finding | None:
    t = memory_trend(history, cfg)
    if t is None or t.slope_bytes_per_s < cfg.min_slope_bytes_per_s or t.r2 < cfg.min_r2:
        return None
    if t.eta_seconds > cfg.oom_horizon_seconds:
        return None
    conf = min(MAX_CONFIDENCE, t.r2 * min(1.0, t.n_points / cfg.full_confidence_points))
    s = history[-1]
    mib = 1024 * 1024
    return Finding(
        FindingKind.PREDICTED_OOM, Severity.WARNING, s.namespace, s.name, s.uid,
        f"memory rising {t.slope_bytes_per_s / 1024:.0f} KiB/s (R²={t.r2:.2f}, {t.n_points} samples); "
        f"{t.current_bytes / mib:.0f}/{t.limit_bytes / mib:.0f} MiB, limit in ~{t.eta_seconds:.0f}s",
        s.observed_at, f"{s.uid}:{FindingKind.PREDICTED_OOM.value}", confidence=round(conf, 3))


def predict_restart_loop(history: list[PodSnapshot], cfg: PredictiveConfig = PredictiveConfig()) -> Finding | None:
    """Restarts accelerating toward the reactive threshold, before it is crossed."""
    if not history:
        return None
    last = history[-1]
    cutoff = last.observed_at.timestamp() - cfg.restart_window_seconds
    recent = [s for s in history if s.observed_at.timestamp() >= cutoff]
    events = max(0, recent[-1].restart_count - recent[0].restart_count)
    if events < cfg.restart_min_events or last.restart_count >= cfg.restart_threshold:
        return None  # not enough signal, or the reactive rule already owns it
    conf = min(0.9, 0.3 + 0.25 * events)
    remaining = cfg.restart_threshold - last.restart_count
    return Finding(
        FindingKind.PREDICTED_CRASH_LOOP, Severity.WARNING, last.namespace, last.name, last.uid,
        f"{events} restarts in the last {cfg.restart_window_seconds / 60:.0f} min; "
        f"{remaining} more reaches the restart threshold ({cfg.restart_threshold})",
        last.observed_at, f"{last.uid}:{FindingKind.PREDICTED_CRASH_LOOP.value}", confidence=round(conf, 3))


def evaluate_predictive(history: list[PodSnapshot], cfg: PredictiveConfig = PredictiveConfig()) -> list[Finding]:
    return [f for f in (predict_oom(history, cfg), predict_restart_loop(history, cfg)) if f is not None]
