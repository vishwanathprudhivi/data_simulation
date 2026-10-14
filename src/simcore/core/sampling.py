"""Runtime samplers built from ``DistributionSpec`` (FR-AGT-04, FR-SCN-02)."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from simcore.specs.distributions import (
    ConstantDist,
    DistributionSpec,
    EmpiricalDist,
    ExponentialDist,
    GammaDist,
    LognormalDist,
    NormalDist,
    TriangularDist,
    UniformDist,
    WeibullDist,
)

__all__ = ["Sampler", "make_sampler", "mean_of"]

Sampler = Callable[[np.random.Generator], float]


def make_sampler(spec: DistributionSpec) -> Sampler:
    """Return ``f(generator) -> float`` drawing one value from ``spec``."""
    if isinstance(spec, ConstantDist):
        value = spec.value
        return lambda g: value
    if isinstance(spec, ExponentialDist):
        mean = spec.mean
        return lambda g: float(g.exponential(mean))
    if isinstance(spec, LognormalDist):
        mu, sigma = spec.mu, spec.sigma
        return lambda g: float(g.lognormal(mu, sigma))
    if isinstance(spec, GammaDist):
        shape, scale = spec.shape, spec.scale
        return lambda g: float(g.gamma(shape, scale))
    if isinstance(spec, WeibullDist):
        k, lam = spec.shape, spec.scale
        return lambda g: float(lam * g.weibull(k))
    if isinstance(spec, NormalDist):
        m, sd, lo = spec.mean, spec.sd, spec.min

        def normal(g: np.random.Generator) -> float:
            for _ in range(1000):
                x = float(g.normal(m, sd))
                if lo is None or x >= lo:
                    return x
            raise RuntimeError(f"normal({m}, {sd}) truncated at {lo}: rejection failed")

        return normal
    if isinstance(spec, UniformDist):
        lo_u, hi_u = spec.low, spec.high
        return lambda g: float(g.uniform(lo_u, hi_u))
    if isinstance(spec, TriangularDist):
        a, c, b = spec.low, spec.mode, spec.high
        return lambda g: float(g.triangular(a, c, b))
    if isinstance(spec, EmpiricalDist):
        values = np.asarray(spec.values, dtype=float)
        p = None
        if spec.weights is not None:
            w = np.asarray(spec.weights, dtype=float)
            p = w / w.sum()
        return lambda g: float(g.choice(values, p=p))
    raise TypeError(f"unsupported distribution {type(spec).__name__}")


def mean_of(spec: DistributionSpec) -> float:
    """Analytical mean (used by estimators and validation)."""
    if isinstance(spec, ConstantDist):
        return spec.value
    if isinstance(spec, ExponentialDist):
        return spec.mean
    if isinstance(spec, LognormalDist):
        return float(np.exp(spec.mu + spec.sigma**2 / 2))
    if isinstance(spec, GammaDist):
        return spec.shape * spec.scale
    if isinstance(spec, WeibullDist):
        from math import gamma

        return spec.scale * gamma(1 + 1 / spec.shape)
    if isinstance(spec, NormalDist):
        return spec.mean
    if isinstance(spec, UniformDist):
        return (spec.low + spec.high) / 2
    if isinstance(spec, TriangularDist):
        return (spec.low + spec.mode + spec.high) / 3
    if isinstance(spec, EmpiricalDist):
        return float(np.average(spec.values, weights=spec.weights))
    raise TypeError(f"unsupported distribution {type(spec).__name__}")
