from __future__ import annotations

import math
from typing import Optional

from scipy.stats import t as student_t

_MIN_ABS_QUANTILE = 1.0e-8


def _clamp_prob(p: float) -> float:
    return max(1.0e-6, min(1.0 - 1.0e-6, p))


def invert_implied_vol_per_s(
    p_up: float,
    anchor: float,
    ref_price: float,
    horizon_s: float,
    df: int = 3,
) -> Optional[float]:
    if horizon_s <= 0.0 or anchor <= 0.0 or ref_price <= 0.0:
        return None

    p = _clamp_prob(p_up)
    threshold = math.log(anchor / ref_price)
    if threshold == 0.0:
        return None

    p_down = 1.0 - p
    x = student_t.ppf(p_down, df)
    if not math.isfinite(x) or abs(x) < _MIN_ABS_QUANTILE:
        return None

    scale = math.sqrt(df / (df - 2.0)) if df > 2 else 1.0
    sigma = abs(threshold) * scale / abs(x)
    vol_per_s = sigma / math.sqrt(horizon_s)
    if not math.isfinite(vol_per_s) or vol_per_s <= 0.0:
        return None
    return vol_per_s


def forward_p_up(
    anchor: float,
    ref_price: float,
    horizon_s: float,
    vol_per_s: float,
    df: int = 3,
) -> Optional[float]:
    if horizon_s <= 0.0 or anchor <= 0.0 or ref_price <= 0.0 or vol_per_s <= 0.0:
        return None

    sigma = vol_per_s * math.sqrt(horizon_s)
    if not math.isfinite(sigma) or sigma <= 0.0:
        return None

    threshold = math.log(anchor / ref_price)
    x = threshold / sigma
    if df > 2:
        x *= math.sqrt(df / (df - 2.0))

    p_down = student_t.cdf(x, df)
    if not math.isfinite(p_down):
        return None

    return _clamp_prob(1.0 - p_down)
