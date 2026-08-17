"""
Regression suite for scheduler/erlang.py::required_agents() — an Erlang C
(M/M/c queue) staffing calculation with no dependency on the rest of the app
(no DB, no Flask context needed). Reference values below were computed by
running the function itself and hand-verifying the underlying math (the
occupancy-cap case is confirmed to actually engage the cap branch, not just
coincidentally match the service-level-driven count) — same "encode a
verified guarantee" philosophy as tests/test_scheduling.py.
"""
from math import ceil

from scheduler.erlang import _occupancy, _service_level, _traffic_intensity, required_agents


def test_zero_or_negative_transactions_need_no_agents():
    assert required_agents(0, aht_minutes=4, asa_minutes=20 / 60, interval_minutes=30,
                            shrinkage=0.15, service_level=0.80, max_occupancy=0.85) == 0
    assert required_agents(-5, aht_minutes=4, asa_minutes=20 / 60, interval_minutes=30,
                            shrinkage=0.15, service_level=0.80, max_occupancy=0.85) == 0


def test_moderate_load_matches_hand_verified_reference():
    """100 contacts/30min, AHT 4min, target 80% in 20s, 85% max occupancy,
    15% shrinkage. Service-level-driven headcount is 17 positions before
    shrinkage; grossed up for 15% shrinkage: ceil(17 / 0.85) = 20."""
    result = required_agents(transactions=100, aht_minutes=4, asa_minutes=20 / 60,
                              interval_minutes=30, shrinkage=0.15, service_level=0.80,
                              max_occupancy=0.85)
    assert result == 20


def test_shrinkage_inflates_headcount_proportionally():
    """Same volume/service-level target, only shrinkage differs — higher
    shrinkage must never produce fewer or equal agents than lower shrinkage."""
    no_shrinkage = required_agents(100, aht_minutes=4, asa_minutes=20 / 60, interval_minutes=30,
                                    shrinkage=0.0, service_level=0.80, max_occupancy=0.85)
    with_shrinkage = required_agents(100, aht_minutes=4, asa_minutes=20 / 60, interval_minutes=30,
                                      shrinkage=0.30, service_level=0.80, max_occupancy=0.85)
    assert no_shrinkage == 17
    assert with_shrinkage == 25
    assert with_shrinkage > no_shrinkage


def test_max_occupancy_cap_overrides_service_level_count():
    """200 contacts/30min at AHT 5min, with a deliberately low max_occupancy
    (50%) and a low service-level target (50%) so the service-level-driven
    count alone (36 positions, ~93% occupancy) would breach the cap — the
    occupancy ceiling must take over and demand more agents than the
    service-level loop alone would ask for."""
    intensity = _traffic_intensity(200, aht_minutes=5, interval_minutes=30)
    sl_positions = round(intensity + 1)
    while _service_level(sl_positions, intensity, aht_minutes=5, asa_minutes=20 / 60) < 0.5:
        sl_positions += 1
    assert _occupancy(sl_positions, intensity) > 0.5, 'test setup must actually breach the cap'

    result = required_agents(transactions=200, aht_minutes=5, asa_minutes=20 / 60,
                              interval_minutes=30, shrinkage=0.0, service_level=0.5,
                              max_occupancy=0.5)
    assert result == ceil(intensity / 0.5)
    assert result > sl_positions


def test_more_volume_never_needs_fewer_agents():
    low = required_agents(50, aht_minutes=4, asa_minutes=20 / 60, interval_minutes=30,
                           shrinkage=0.15, service_level=0.80, max_occupancy=0.85)
    high = required_agents(500, aht_minutes=4, asa_minutes=20 / 60, interval_minutes=30,
                            shrinkage=0.15, service_level=0.80, max_occupancy=0.85)
    assert high > low
