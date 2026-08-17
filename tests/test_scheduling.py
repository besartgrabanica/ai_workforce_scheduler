"""
Regression suite for scheduler/algorithm.py::generate_schedule().

Each test encodes a guarantee that was verified by hand at some point this
project's history (see RUNBOOK.md / commit history) — the point of this file
is that those guarantees no longer depend on someone re-running a one-off
script to notice if a future change breaks them.
"""
from datetime import date

from app import db
from scheduler.algorithm import _time_to_minutes, generate_schedule, set_holidays
from scheduler.models import (
    DailyForecast,
    DayRestriction,
    Employee,
    ExcludedDate,
    ForecastPeriod,
    RotationCycleWeek,
    RotationPattern,
    Schedule,
    ScheduleGroup,
    ShiftAssignment,
    ShiftTemplate,
)
from tests.conftest import make_employee, set_param


def _generate(year, month, name='test'):
    sched = Schedule(name=name, year=year, month=month)
    db.session.add(sched)
    db.session.commit()
    summary = generate_schedule(sched.id, year, month)
    return sched, summary


def test_nobody_falls_short_of_monthly_target(default_template):
    """The scheduler's core promise: whatever an employee's FTE% works out
    to for the month, they end the month at or above that target — never
    short, regardless of how the day-by-day allocation plays out."""
    set_param('default_agents_weekday', 100)  # far more than our 15 employees, cap never binds
    for pct in (100, 100, 100, 100, 100, 75, 75, 75, 75, 75, 50, 50, 50, 50, 50):
        make_employee(f'Employee {pct}-{id(object())}', fte_percent=pct)

    _, summary = _generate(2026, 9)

    short = [e for e in summary['employee_summary'] if e['assigned_wd'] < e['target_wd']]
    assert short == [], f"employees fell short of target: {short}"


def test_coverage_cap_is_never_exceeded(default_template):
    """The client only pays for coverage up to max_coverage_pct (105% by
    default) of the forecast requirement — the scheduler must never
    knowingly staff a day above that ceiling."""
    set_param('default_agents_weekday', 5)  # small requirement, plenty of eager willing staff
    set_param('max_coverage_pct', 105)
    for i in range(30):
        make_employee(f'Employee {i}', fte_percent=100)

    _, summary = _generate(2026, 9)

    over_cap = [(d, v) for d, v in summary['daily_coverage'].items() if v['pct'] > 105.5]
    assert over_cap == [], f"days exceeded the coverage cap: {over_cap}"


def test_hard_exclusions_are_always_respected(default_template):
    """Saturday opt-out, an explicit excluded date, and a public holiday for
    someone who doesn't work holidays must never result in a 'work' status,
    no matter how the day-by-day allocation prioritizes other employees."""
    set_param('default_agents_weekday', 50)
    set_param('default_agents_saturday', 50)

    no_saturday = make_employee('No Saturday', works_saturday='no')
    excluded_date_emp = make_employee('Has Excluded Date')
    db.session.add(ExcludedDate(employee_id=excluded_date_emp.id, date=date(2026, 9, 9)))
    no_holidays = make_employee('No Holidays', works_holidays=False)
    db.session.commit()

    set_holidays({2026: {date(2026, 9, 7)}})  # a Monday in the test month

    sched, _ = _generate(2026, 9)

    saturdays = [a for a in ShiftAssignment.query.filter_by(schedule_id=sched.id, employee_id=no_saturday.id)
                 if a.date.weekday() == 5]
    assert all(a.status != 'work' for a in saturdays)

    excl = ShiftAssignment.query.filter_by(schedule_id=sched.id, employee_id=excluded_date_emp.id,
                                            date=date(2026, 9, 9)).first()
    assert excl.status != 'work'

    holiday_row = ShiftAssignment.query.filter_by(schedule_id=sched.id, employee_id=no_holidays.id,
                                                   date=date(2026, 9, 7)).first()
    assert holiday_row.status == 'holiday_off'


def test_rotation_stays_continuous_across_a_mid_week_month_boundary(default_template):
    """The whole point of anchoring a rotation to a fixed date + real
    calendar weeks instead of 'week 1 = day 1 of the schedule': a month
    that ends mid-week must not reset the rotation on the 1st of the next
    month. September 2026 ends on a Wednesday, which is exactly this case."""
    tpl_a = default_template
    tpl_b = ShiftTemplate(name='Night', start_time='14:00', end_time='22:30', hours=7.5, color='#7c3aed')
    db.session.add(tpl_b)
    db.session.commit()

    pattern = RotationPattern(name='2-week rotation', anchor_date=date(2026, 9, 7))  # a Monday
    db.session.add(pattern)
    db.session.flush()
    db.session.add(RotationCycleWeek(rotation_pattern_id=pattern.id, position=0, shift_template_id=tpl_a.id))
    db.session.add(RotationCycleWeek(rotation_pattern_id=pattern.id, position=1, shift_template_id=tpl_b.id))
    db.session.commit()

    emp = make_employee('Rotation Employee', rotation_pattern_id=pattern.id)

    sept, _ = _generate(2026, 9, name='sept')
    octo, _ = _generate(2026, 10, name='oct')

    tail = (ShiftAssignment.query.filter_by(schedule_id=sept.id, employee_id=emp.id, status='work')
            .filter(ShiftAssignment.date >= date(2026, 9, 28)).all())
    head = (ShiftAssignment.query.filter_by(schedule_id=octo.id, employee_id=emp.id, status='work')
            .filter(ShiftAssignment.date <= date(2026, 10, 4)).all())

    tail_templates = {a.shift_template_id for a in tail}
    head_templates = {a.shift_template_id for a in head}
    assert tail_templates == head_templates, (
        f"rotation flipped across the month boundary: tail={tail_templates} head={head_templates}")

    next_week = (ShiftAssignment.query.filter_by(schedule_id=octo.id, employee_id=emp.id, status='work')
                 .filter(ShiftAssignment.date >= date(2026, 10, 5), ShiftAssignment.date <= date(2026, 10, 9))
                 .all())
    next_week_templates = {a.shift_template_id for a in next_week}
    assert next_week_templates and next_week_templates != tail_templates, (
        "rotation should flip at the next real Monday")


def test_day_of_week_override_wins_over_rotation(default_template):
    """A standing 'always this shift on Tuesdays' choice is a deliberate
    per-employee exception and must win over whichever shift the rotation
    would otherwise assign that week."""
    tpl_a = default_template
    tpl_b = ShiftTemplate(name='Night', start_time='14:00', end_time='22:30', hours=7.5, color='#7c3aed')
    db.session.add(tpl_b)
    db.session.commit()

    pattern = RotationPattern(name='2-week rotation', anchor_date=date(2026, 9, 7))
    db.session.add(pattern)
    db.session.flush()
    db.session.add(RotationCycleWeek(rotation_pattern_id=pattern.id, position=0, shift_template_id=tpl_a.id))
    db.session.add(RotationCycleWeek(rotation_pattern_id=pattern.id, position=1, shift_template_id=tpl_b.id))
    db.session.commit()

    emp = make_employee('Overridden Employee', rotation_pattern_id=pattern.id)
    db.session.add(DayRestriction(employee_id=emp.id, day_of_week=1, shift_type=tpl_b.name.lower()))  # Tuesday
    db.session.commit()

    sched, _ = _generate(2026, 9)

    tuesdays = [a for a in ShiftAssignment.query.filter_by(schedule_id=sched.id, employee_id=emp.id, status='work')
                if a.date.weekday() == 1]
    assert tuesdays, "expected at least one Tuesday work assignment"
    assert all(a.shift_template_id == tpl_b.id for a in tuesdays), (
        "day-of-week override did not win over the rotation on Tuesdays")


def test_schedule_group_members_always_move_together(default_template):
    """A schedule group's entire purpose: members share identical work/off
    days every day of the month, never split by individual prioritization."""
    set_param('default_agents_weekday', 50)
    grp = ScheduleGroup(name='Test Group')
    db.session.add(grp)
    db.session.flush()
    a = make_employee('Group Member A', fte_percent=75, schedule_group_id=grp.id)
    b = make_employee('Group Member B', fte_percent=75, schedule_group_id=grp.id)

    sched, _ = _generate(2026, 9)

    pattern_a = [x.status for x in
                 ShiftAssignment.query.filter_by(schedule_id=sched.id, employee_id=a.id).order_by(ShiftAssignment.date)]
    pattern_b = [x.status for x in
                 ShiftAssignment.query.filter_by(schedule_id=sched.id, employee_id=b.id).order_by(ShiftAssignment.date)]
    assert pattern_a == pattern_b


def test_part_time_days_off_are_spread_not_front_loaded(default_template):
    """Regression test for the month-end coverage cliff: a workforce made
    entirely of part-timers, with more capacity than the daily requirement
    (so the coverage cap never forces deferral), must not have everyone
    exhaust their monthly quota in the first half of the month and leave
    the back half of the month uncovered. Before the pacing fix, this
    reproduced a real ~75-78% coverage crash on the last few working days."""
    set_param('default_agents_weekday', 25)  # cap (~26) exceeds the whole 20-person pool -> never binds
    for i in range(20):
        make_employee(f'Part Timer {i}', fte_percent=50)

    sched, summary = _generate(2026, 9)

    weekdays = sorted({a.date for a in ShiftAssignment.query.filter_by(schedule_id=sched.id)
                       if a.date.weekday() < 5})
    first_5, last_5 = weekdays[:5], weekdays[-5:]

    def _scheduled_on(days):
        return sum(1 for a in ShiftAssignment.query.filter_by(schedule_id=sched.id, status='work')
                  if a.date in days)

    first_count = _scheduled_on(set(first_5))
    last_count = _scheduled_on(set(last_5))

    assert last_count >= 0.6 * first_count, (
        f"coverage crashed near month-end: first 5 weekdays={first_count}, "
        f"last 5 weekdays={last_count} scheduled shifts")


def _consecutive_work_gaps_hours(sched_id, emp_id):
    """Gap in hours, in calendar order, between each pair of consecutive
    'work' assignments for an employee (skipping non-work days in between,
    same definition the rest-period rule itself uses)."""
    rows = (ShiftAssignment.query.filter_by(schedule_id=sched_id, employee_id=emp_id, status='work')
            .order_by(ShiftAssignment.date).all())
    gaps = []
    for prev, nxt in zip(rows, rows[1:]):
        gap_days = (nxt.date - prev.date).days
        gap_minutes = gap_days * 24 * 60 + _time_to_minutes(nxt.shift_start) - _time_to_minutes(prev.shift_end)
        gaps.append(gap_minutes / 60)
    return gaps


def test_minimum_rest_period_is_enforced(default_template):
    """An employee forced (via day-of-week overrides) onto a late shift one
    day and an early shift the next must not actually be scheduled to work
    both — the rest-period hard exclusion must win over the override."""
    afternoon = ShiftTemplate(name='Afternoon', start_time='14:00', end_time='22:00', hours=8, color='#f59e0b')
    morning = ShiftTemplate(name='Morning', start_time='08:00', end_time='16:00', hours=8, color='#3b82f6')
    db.session.add_all([afternoon, morning])
    db.session.commit()

    emp = make_employee('Tight Turnaround', fte_percent=100)
    # Monday afternoon (ends 22:00) -> Tuesday morning (starts 08:00) is only
    # a 10-hour gap, under the 11-hour minimum.
    db.session.add(DayRestriction(employee_id=emp.id, day_of_week=0, shift_type='afternoon'))
    db.session.add(DayRestriction(employee_id=emp.id, day_of_week=1, shift_type='morning'))
    db.session.commit()

    sched, _ = _generate(2026, 9)

    gaps = _consecutive_work_gaps_hours(sched.id, emp.id)
    assert gaps, "expected at least one pair of consecutive work days to check"
    assert all(g >= 11.0 - 1e-9 for g in gaps), f"a rest-period gap was too short: {gaps}"


def test_sufficient_rest_is_not_blocked(default_template):
    """The rest-period rule must not be overly conservative — a pairing with
    a genuinely legitimate gap should schedule normally, every eligible day,
    same as the no-rotation baseline."""
    morning = ShiftTemplate(name='Morning', start_time='08:00', end_time='14:00', hours=6, color='#3b82f6')
    db.session.add(morning)
    db.session.commit()

    emp = make_employee('Comfortable Turnaround', fte_percent=100, shift_template_id=morning.id)

    sched, summary = _generate(2026, 9)

    gaps = _consecutive_work_gaps_hours(sched.id, emp.id)
    assert gaps, "expected consecutive work days for a 100% FTE employee"
    assert all(g >= 11.0 for g in gaps)

    emp_summary = next(e for e in summary['employee_summary'] if e['name'] == 'Comfortable Turnaround')
    assert emp_summary['assigned_wd'] >= emp_summary['target_wd']


def test_polish_pass_smooths_synchronized_coverage_dips(default_template):
    """Employees who share the same FTE% independently compute identical
    pace math and can all land on the same day as their discretionary day
    off, creating a coverage dip on that day even though the month's total
    capacity is enough to cover it if spread differently. The post-hoc
    polish pass should close that gap by relocating discretionary days off
    onto the shorted day from days with coverage to spare.

    Population/requirement sizes matter here, verified by hand: the 105%
    cap only creates real integer headroom above `required` (needed for any
    day to ever have "surplus" to donate from) once `required` is large
    enough — at small values int(required*1.05) rounds right back down to
    required, leaving zero surplus anywhere, no matter the population mix.
    54 employees at 60% FTE + 36 at 75%, required=58 (cap=60) was confirmed
    directly: without the polish pass this scenario dips to 82.8% on some
    weekdays; with it, every weekday lands at ~100%."""
    set_param('default_agents_weekday', 58)
    for i in range(54):
        make_employee(f'Sixty Percent {i}', fte_percent=60)
    for i in range(36):
        make_employee(f'Seventy Five Percent {i}', fte_percent=75)

    sched, summary = _generate(2026, 9)

    weekdays = sorted({a.date for a in ShiftAssignment.query.filter_by(schedule_id=sched.id)
                       if a.date.weekday() < 5})
    pcts = [summary['daily_coverage'][str(d)]['pct'] for d in weekdays]
    assert min(pcts) >= 95.0, f"a day dipped well below the rest after polishing: {pcts}"

    # The core guarantee must still hold — polishing only relocates days,
    # never adds or removes them.
    short = [e for e in summary['employee_summary'] if e['assigned_wd'] < e['target_wd']]
    assert short == []


def test_polish_pass_never_touches_schedule_groups(default_template):
    """A schedule group's discretionary day off must never be relocated —
    moving one member alone would break "the group always moves together",
    even when the conditions would otherwise make them an obvious polish
    candidate.

    Population size matters here, verified by hand: embedding the group in
    a large pool (like the dip-smoothing test's 90 employees) means some
    other ordinary candidate almost always satisfies any given deficit day
    before the group is ever even considered — the protection is real but
    never actually exercised, making the test pass whether or not the
    protection code is even there (confirmed: removing the group filter
    entirely didn't change the outcome at that scale). This smaller pool
    (32 employees) makes the group a large enough fraction of candidates
    that removing the protection demonstrably breaks their sync — confirmed
    by hand before writing this assertion."""
    set_param('default_agents_weekday', 21)
    grp = ScheduleGroup(name='Polish-proof Group')
    db.session.add(grp)
    db.session.flush()
    a = make_employee('Grouped A', fte_percent=60, schedule_group_id=grp.id)
    b = make_employee('Grouped B', fte_percent=60, schedule_group_id=grp.id)
    for i in range(16):
        make_employee(f'Sixty Percent {i}', fte_percent=60)
    for i in range(14):
        make_employee(f'Seventy Five Percent {i}', fte_percent=75)

    sched, _ = _generate(2026, 9)

    pattern_a = [x.status for x in ShiftAssignment.query.filter_by(schedule_id=sched.id, employee_id=a.id)
                .order_by(ShiftAssignment.date)]
    pattern_b = [x.status for x in ShiftAssignment.query.filter_by(schedule_id=sched.id, employee_id=b.id)
                .order_by(ShiftAssignment.date)]
    assert pattern_a == pattern_b, "grouped members must stay in sync even after polishing"
    assert 'day_off' in pattern_a, (
        "test setup issue: the group needs at least one discretionary day off "
        "for this to meaningfully test that polish leaves it alone")


def test_polish_pass_never_creates_a_rest_period_violation(default_template):
    """Even under conditions engineered to make relocating onto a specific
    day both tempting (a real, forced deficit there) and dangerous (a tight
    day-of-week shift pairing), the polish pass must never place someone on
    a day that violates the minimum rest period.

    This specifically exercises polish's own rest-period re-check, not the
    main pass's (already covered by test_minimum_rest_period_is_enforced) —
    those are two different code paths. A day-of-week override alone isn't
    enough to prove it: the main pass's own forward-only rest check already
    excludes the dangerous day as a hard exclusion before polish ever sees
    it, so a naive version of this test passes whether or not polish's own
    check is even there (confirmed by hand). What actually exercises
    polish's check is a conflict introduced by relocation itself — day X
    was 'day_off' (no conflict existed) when its neighbor was originally
    decided, so the main pass's forward-only check never had a reason to
    flag it; only polish's bidirectional neighbor check catches this. Forcing
    a specific real DailyForecast requirement on a Monday (via a real
    ForecastPeriod, same as forecast_new() would create) makes that Monday a
    guaranteed deficit day for a population with a Monday-afternoon /
    Tuesday-morning override (10h gap) — confirmed by hand this reproduces
    4 real violations if polish's rest-check is bypassed, and 0 with it in
    place."""
    afternoon = ShiftTemplate(name='Afternoon', start_time='14:00', end_time='22:00', hours=8, color='#f59e0b')
    morning = ShiftTemplate(name='Morning', start_time='08:00', end_time='16:00', hours=8, color='#3b82f6')
    db.session.add_all([afternoon, morning])
    db.session.commit()

    set_param('default_agents_weekday', 58)
    period = ForecastPeriod(name='p', start_date=date(2026, 9, 1), end_date=date(2026, 9, 30))
    db.session.add(period)
    db.session.flush()
    # 2026-09-07 is a Monday. Forcing a high requirement here (well above
    # what this population can naturally supply) guarantees it's a real
    # deficit day polish will actively try to fill.
    db.session.add(DailyForecast(period_id=period.id, date=date(2026, 9, 7), day_of_week='Mon',
                                 required_ks_agents=95))
    db.session.commit()

    for i in range(54):
        emp = make_employee(f'Sixty Percent {i}', fte_percent=60)
        db.session.add(DayRestriction(employee_id=emp.id, day_of_week=0, shift_type='afternoon'))
        db.session.add(DayRestriction(employee_id=emp.id, day_of_week=1, shift_type='morning'))
    for i in range(36):
        make_employee(f'Seventy Five Percent {i}', fte_percent=75)
    db.session.commit()

    sched = Schedule(name='rest-period-polish-test', year=2026, month=9, period_id=period.id)
    db.session.add(sched)
    db.session.commit()
    generate_schedule(sched.id, 2026, 9)

    all_gaps = []
    for emp in Employee.query.all():
        all_gaps.extend(_consecutive_work_gaps_hours(sched.id, emp.id))
    assert all_gaps, "expected at least some consecutive work days to check"
    assert all(g >= 11.0 - 1e-9 for g in all_gaps), f"polishing introduced a rest-period violation: {all_gaps}"
