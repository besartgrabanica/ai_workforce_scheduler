"""
Intraday coverage derivation for the dashboard's heatmap chart.

Nothing else in the app currently compares HalfHourlyForecast (half-hourly
contact-volume demand, written on forecast import) against actual scheduled
headcount at that same half-hour granularity — ShiftAssignment only stores
one shift_start/shift_end span per employee per day, not per-slot data, so
"how many people are actually on duty at 14:30" has to be derived by
expanding each work assignment's span across the day's forecasted slots.
Kept out of scheduler/output.py (Excel export) and scheduler/algorithm.py
(schedule generation) since this is read-only reporting, not part of either.
"""
from .models import DailyForecast, ShiftAssignment


def intraday_coverage(day):
    """Returns (slots, demand, scheduled) for the given date — three
    parallel lists, one entry per half-hourly forecast slot that day.
    demand is HalfHourlyForecast.total (contact volume); scheduled is how
    many 'work'-status ShiftAssignments span that slot_time. Empty lists if
    no forecast exists for this date."""
    fc = DailyForecast.query.filter_by(date=day).first()
    if not fc or not fc.half_hourly:
        return [], [], []

    assignments = ShiftAssignment.query.filter_by(date=day, status='work').all()

    slots, demand, scheduled = [], [], []
    for hh in fc.half_hourly:
        slots.append(hh.slot_time)
        demand.append(round(hh.total, 1))
        scheduled.append(sum(
            1 for a in assignments
            if a.shift_start and a.shift_end and a.shift_start <= hh.slot_time < a.shift_end
        ))

    return slots, demand, scheduled
