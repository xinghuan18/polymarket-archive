from __future__ import annotations

import math
from unittest.mock import patch

from model import forward_p_up, invert_implied_vol_per_s


def test_inversion_forward_consistency() -> None:
    p_up = 0.62
    anchor = 99.5
    ref = 100.0
    horizon = 1800.0

    vol = invert_implied_vol_per_s(p_up=p_up, anchor=anchor, ref_price=ref, horizon_s=horizon, df=3)
    assert vol is not None
    assert math.isfinite(vol)
    assert vol > 0.0

    p_rebuilt = forward_p_up(anchor=anchor, ref_price=ref, horizon_s=horizon, vol_per_s=vol, df=3)
    assert p_rebuilt is not None
    assert abs(p_rebuilt - p_up) < 1.0e-6


def test_invalid_inputs_return_none() -> None:
    assert invert_implied_vol_per_s(0.5, anchor=0.0, ref_price=100.0, horizon_s=100.0, df=3) is None
    assert invert_implied_vol_per_s(0.5, anchor=100.0, ref_price=0.0, horizon_s=100.0, df=3) is None
    assert invert_implied_vol_per_s(0.5, anchor=100.0, ref_price=100.0, horizon_s=100.0, df=3) is None

    assert forward_p_up(anchor=100.0, ref_price=100.0, horizon_s=100.0, vol_per_s=0.1, df=3) == 0.5
    assert forward_p_up(anchor=100.0, ref_price=99.0, horizon_s=0.0, vol_per_s=0.1, df=3) is None
    assert forward_p_up(anchor=100.0, ref_price=99.0, horizon_s=100.0, vol_per_s=0.0, df=3) is None


def test_invert_rejects_near_zero_quantile() -> None:
    with patch("model.student_t.ppf", return_value=1.0e-12):
        vol = invert_implied_vol_per_s(
            p_up=0.5,
            anchor=100.1,
            ref_price=100.0,
            horizon_s=600.0,
            df=3,
        )
    assert vol is None
