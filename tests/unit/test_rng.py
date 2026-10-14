"""Named RNG streams (FR-ENG-06, FR-EXP-01, AC-EXP-02)."""

from __future__ import annotations

import numpy as np

from simcore.core.rng import RNGManager


def test_FR_EXP_01_streams_are_reproducible() -> None:
    a = RNGManager(7, 3).stream("demand").random(5)
    b = RNGManager(7, 3).stream("demand").random(5)
    assert np.array_equal(a, b)


def test_FR_EXP_01_streams_differ_by_name_replication_and_seed() -> None:
    base = RNGManager(7, 3).stream("demand").random(3)
    assert not np.array_equal(base, RNGManager(7, 3).stream("service_time").random(3))
    assert not np.array_equal(base, RNGManager(7, 4).stream("demand").random(3))
    assert not np.array_equal(base, RNGManager(8, 3).stream("demand").random(3))


def test_AC_EXP_02_new_consumer_on_service_time_does_not_change_demand() -> None:
    plain = RNGManager(11, 0)
    demand_plain = plain.stream("demand").random(100)

    busy = RNGManager(11, 0)
    busy.stream("service_time").random(10_000)  # an extra consumer on another stream
    busy.stream("agent_speed").normal(size=17)
    demand_busy = busy.stream("demand").random(100)
    assert np.array_equal(demand_plain, demand_busy)


def test_stream_creation_order_is_irrelevant() -> None:
    m1 = RNGManager(5, 1)
    m1.stream("x")
    y1 = m1.stream("y").random(4)
    m2 = RNGManager(5, 1)
    y2 = m2.stream("y").random(4)
    assert np.array_equal(y1, y2)


def test_describe_lists_streams() -> None:
    m = RNGManager(1, 2)
    m.stream("b")
    m.stream("a")
    d = m.describe()
    assert d["streams"] == ["a", "b"] and d["master_seed"] == 1 and d["replication_id"] == 2
