"""Scheduler, bus, clock and registry (FR-ENG-02/03/04, BRD 7.3)."""

from __future__ import annotations

import pytest

from simcore.core.bus import EventBus, UnknownEventKind
from simcore.core.clock import SimClock
from simcore.core.registry import Entity, EntityRegistry, Region
from simcore.core.scheduler import EventScheduler


def test_FR_ENG_03_ordering_key_time_priority_sequence() -> None:
    s = EventScheduler()
    s.schedule(2.0, "c", priority=50)
    s.schedule(1.0, "b2", priority=60)
    s.schedule(1.0, "a", priority=10)
    s.schedule(1.0, "b1", priority=60)
    assert [s.pop().kind for _ in range(4)] == ["a", "b2", "b1", "c"]


def test_scheduler_cancel_and_rejects_non_finite() -> None:
    s = EventScheduler()
    ev = s.schedule(1.0, "x")
    s.schedule(2.0, "y")
    s.cancel(ev)
    assert s.peek_time() == 2.0 and s.pop().kind == "y"
    with pytest.raises(ValueError, match="non-finite"):
        s.schedule(float("nan"), "z")


def test_FR_ENG_03_bus_is_typed_and_ordered() -> None:
    bus = EventBus()
    bus.register("tick", int)
    seen: list[str] = []
    bus.subscribe("tick", lambda e: seen.append(f"first:{e.payload}"))
    bus.subscribe("*", lambda e: seen.append(f"wild:{e.payload}"))
    bus.subscribe("tick", lambda e: seen.append(f"second:{e.payload}"))
    s = EventScheduler()
    bus.publish(s.schedule(0.0, "tick", 1))
    assert seen == ["first:1", "second:1", "wild:1"]
    with pytest.raises(TypeError, match="expects payload int"):
        bus.publish(s.schedule(0.0, "tick", "nope"))
    with pytest.raises(UnknownEventKind):
        bus.publish(s.schedule(0.0, "tock", None))
    with pytest.raises(ValueError, match="already registered"):
        bus.register("tick", str)


def test_FR_ENG_02_clock_is_monotonic() -> None:
    c = SimClock()
    c.advance_to(5.0)
    assert c.now == 5.0 and c.elapsed == 5.0
    with pytest.raises(ValueError, match="backwards"):
        c.advance_to(4.0)


def test_FR_ENG_04_registry_lookup() -> None:
    r = EntityRegistry()
    a = r.create(
        "agent",
        prefix="srv",
        tags=("server", "fast"),
        attributes={"zone": "z1"},
        position=(1.0, 1.0),
    )
    b = r.create(
        "agent", prefix="srv", tags=("server",), attributes={"zone": "z2"}, position=(9.0, 9.0)
    )
    r.add(Entity("dock-1", "location", ("dock",)))
    assert (a.id, b.id) == ("srv-0000", "srv-0001")
    assert [e.id for e in r.by_tag("server")] == ["srv-0000", "srv-0001"]
    assert [e.id for e in r.by_attribute("zone", "z2")] == ["srv-0001"]
    assert [e.id for e in r.in_region(Region(0, 0, 5, 5))] == ["srv-0000"]
    assert [e.id for e in r.select("type:location")] == ["dock-1"]
    assert [e.id for e in r.select("tag:fast")] == ["srv-0000"]
    with pytest.raises(ValueError, match="duplicate"):
        r.add(Entity("dock-1", "location"))
    with pytest.raises(KeyError, match="no entity"):
        r.get("nope")
