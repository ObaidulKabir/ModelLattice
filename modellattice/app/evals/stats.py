"""Confidence interval for an aggregate score across N task results
(v0.1 Section 5.3, Measurement Confidence; Section 10.2 "Uncertainty").

Normal approximation, clipped to [0, 1]. Approximate for small n (our
MVP task banks run ~3-5 tasks per profile) - documented rather than
hidden: `n` is always returned alongside the interval so a consumer can
judge how much to trust it.
"""
import dataclasses
import math


@dataclasses.dataclass
class Interval:
    mean: float
    lo: float
    hi: float
    n: int


def confidence_interval(scores: list[float], z: float = 1.96) -> Interval:
    n = len(scores)
    if n == 0:
        return Interval(mean=0.0, lo=0.0, hi=0.0, n=0)
    mean = sum(scores) / n
    if n == 1:
        return Interval(mean=mean, lo=max(0.0, mean - 0.25), hi=min(1.0, mean + 0.25), n=1)

    variance = sum((s - mean) ** 2 for s in scores) / (n - 1)
    se = math.sqrt(variance / n)
    margin = z * se
    return Interval(mean=mean, lo=max(0.0, mean - margin), hi=min(1.0, mean + margin), n=n)
