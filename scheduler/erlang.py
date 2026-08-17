"""
Erlang C (M/M/c queue) staffing math — how many agents are needed to hit a
target service level for a given contact volume, average handle time, and
shrinkage. Standard textbook queueing theory, not vendor- or file-specific.

Used by app.py's forecast-import fallback (scheduler.parsers didn't supply
a pre-computed required-agent count) to size required headcount per
half-hourly interval, instead of the flat linear approximation
(contacts / (rate * hours)) that ignores queueing effects and intraday peaks
entirely.
"""
from math import ceil, exp


def _traffic_intensity(transactions: float, aht_minutes: float, interval_minutes: float) -> float:
    """Offered load, in Erlangs, for this interval."""
    return (transactions / interval_minutes) * aht_minutes


def _erlang_b(positions: int, intensity: float) -> float:
    """Probability all `positions` agents are busy and a new contact is
    blocked, in a pure loss system with no queue — the base recursion
    Erlang C's waiting probability is built from."""
    inverse = 1.0
    for n in range(1, positions + 1):
        inverse = 1.0 + (inverse * n / intensity)
    return 1.0 / inverse


def _waiting_probability(positions: int, intensity: float) -> float:
    """Probability a contact has to wait at all before being answered."""
    b = _erlang_b(positions, intensity)
    return positions * b / (positions - intensity * (1 - b))


def _service_level(positions: int, intensity: float, aht_minutes: float, asa_minutes: float) -> float:
    """Fraction of contacts expected to be answered within asa_minutes."""
    wait_prob = _waiting_probability(positions, intensity)
    return max(0.0, 1 - wait_prob * exp(-(positions - intensity) * (asa_minutes / aht_minutes)))


def _occupancy(positions: int, intensity: float) -> float:
    return intensity / positions


def required_agents(transactions: float, aht_minutes: float, asa_minutes: float,
                     interval_minutes: float, shrinkage: float,
                     service_level: float, max_occupancy: float) -> int:
    """How many scheduled agents are needed to hit `service_level` for this
    interval's contact volume, never exceeding `max_occupancy`, with
    `shrinkage` (the fraction of paid time not available to take contacts)
    added back on top. Returns 0 if there's no volume to staff for."""
    if transactions <= 0:
        return 0

    intensity = _traffic_intensity(transactions, aht_minutes, interval_minutes)
    if intensity <= 0:
        return 0

    # Always > intensity: round(x + 1) is never less than x + 0.5.
    positions = round(intensity + 1)
    while _service_level(positions, intensity, aht_minutes, asa_minutes) < service_level:
        positions += 1

    raw_positions = positions
    if _occupancy(positions, intensity) > max_occupancy:
        raw_positions = ceil(intensity / max_occupancy)

    return ceil(raw_positions / (1 - shrinkage))
