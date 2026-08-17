"""
Core scheduling algorithm.

FTE modes
─────────
  days     : employee works  fte_percent%  of eligible weekdays; full shift each day
  hours    : employee works every eligible day but fte_percent% of standard daily hours
  combined : employee works  √(fte_percent%)  of days AND  √(fte_percent%)  of hours
             (geometric split — 75 % combined ≈ 87 % days × 87 % hours)
"""
import bisect
import calendar
from datetime import date, timedelta
from math import sqrt

from .models import (
    BusinessParam,
    DailyForecast,
    Employee,
    EmployeeScheduleSummary,
    RotationPattern,
    ShiftAssignment,
    ShiftTemplate,
    db,
)

# Fallback only — used if the 'max_coverage_pct' BusinessParam row is missing.
# The client only reimburses coverage up to this % of the required daily
# target — every agent scheduled beyond it is pure cost (paid, not billable).
# The scheduler must never knowingly staff a day above this ceiling.
_MAX_COVERAGE_PCT = 105


# Public holidays for the active project, keyed by year. Empty until app.py
# calls set_holidays() once at startup with the active project's holiday
# calendar — a new/unconfigured project has no holidays rather than silently
# inheriting another client's dates.
_ACTIVE_HOLIDAYS: dict = {}

# Fallback defaults — only used if the matching BusinessParam row is missing.
_SATURDAY_HOURS = 5.5
_SUNDAY_HOURS = 5.5
_DEFAULT_AGENTS_WEEKDAY = 220
_DEFAULT_AGENTS_SATURDAY = 55
_DEFAULT_AGENTS_SUNDAY = 0
# EU Working Time Directive daily-rest minimum — relevant given clients'
# Germany/Switzerland operations. Never knowingly schedule less rest than
# this between the end of one shift and the start of the next.
_MIN_REST_HOURS = 11


def set_holidays(holidays_by_year: dict) -> None:
    """Configure the active project's public holidays. Called once at app startup."""
    global _ACTIVE_HOLIDAYS
    _ACTIVE_HOLIDAYS = holidays_by_year


def get_holidays(year: int) -> set:
    """Return the set of public holidays for a given year, for the active project."""
    return _ACTIVE_HOLIDAYS.get(year, set())


def _days_in_month(year: int, month: int) -> list[date]:
    _, n = calendar.monthrange(year, month)
    return [date(year, month, d) for d in range(1, n + 1)]


def _add_hours(start: str, hours: float) -> str:
    sh, sm = map(int, start.split(':'))
    total_mins = sh * 60 + sm + int(round(hours * 60))
    return f'{total_mins // 60:02d}:{total_mins % 60:02d}'


def _time_to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(':'))
    return h * 60 + m


def _week_start(d: date) -> date:
    """Monday of the calendar week containing d."""
    return d - timedelta(days=d.weekday())


def _rotation_shift_for_date(pattern: RotationPattern, target_date: date) -> ShiftTemplate:
    """Which ShiftTemplate a rotation pattern prescribes for the ISO week containing
    target_date. Purely a function of the pattern's anchor_date and target_date — never
    depends on which month is currently being generated, so a rotation's week doesn't
    reset just because a new month's schedule happens to start mid-week."""
    weeks_elapsed = (_week_start(target_date) - _week_start(pattern.anchor_date)).days // 7
    position = weeks_elapsed % pattern.cycle_length
    return pattern.cycle_weeks[position].shift_template


def _shift_details(emp: Employee, is_saturday: bool, is_sunday: bool,
                    sat_hours: float, sun_hours: float, target_date: date) -> tuple[str, str, float]:
    """Return (shift_start, shift_end, hours)."""
    if is_saturday:
        return '08:00', _add_hours('08:00', sat_hours), sat_hours

    if is_sunday:
        return '08:00', _add_hours('08:00', sun_hours), sun_hours

    rotation = emp.effective_rotation_pattern
    if rotation:
        tpl = _rotation_shift_for_date(rotation, target_date)
        return tpl.start_time, tpl.end_time, tpl.hours

    if emp.custom_start and emp.custom_end:
        if emp.custom_hours is not None:
            h = emp.custom_hours
        else:
            sh, sm = map(int, emp.custom_start.split(':'))
            eh, em = map(int, emp.custom_end.split(':'))
            h = (eh * 60 + em - sh * 60 - sm) / 60
        return emp.custom_start, emp.custom_end, h

    tpl = emp.effective_shift_template
    if tpl:
        return tpl.start_time, tpl.end_time, tpl.hours

    return '08:00', '16:30', 7.5


def _adjust_shift_for_hours_mode(start: str, base_hours: float, fte_frac: float) -> tuple[str, float]:
    """Shrink shift end time proportionally for 'hours' FTE mode."""
    actual_hours = base_hours * fte_frac
    end = _add_hours(start, actual_hours)
    return end, actual_hours


def generate_schedule(schedule_id: int, year: int, month: int) -> dict:
    """
    Build ShiftAssignment rows for all active employees for the given month.
    Returns a summary dict with per-employee and per-day stats.
    """
    all_days = _days_in_month(year, month)
    holidays = get_holidays(year)

    params = {p.key: p.value for p in BusinessParam.query.all()}
    sat_hours = float(params.get('hours_saturday', _SATURDAY_HOURS))
    sun_hours = float(params.get('hours_sunday', _SUNDAY_HOURS))
    default_agents_wd  = int(float(params.get('default_agents_weekday', _DEFAULT_AGENTS_WEEKDAY)))
    default_agents_sat = int(float(params.get('default_agents_saturday', _DEFAULT_AGENTS_SATURDAY)))
    default_agents_sun = int(float(params.get('default_agents_sunday', _DEFAULT_AGENTS_SUNDAY)))
    max_coverage_ratio = float(params.get('max_coverage_pct', _MAX_COVERAGE_PCT)) / 100
    min_rest_hours = float(params.get('min_rest_hours', _MIN_REST_HOURS))

    employees = Employee.query.filter_by(status='active').order_by(Employee.team, Employee.name).all()

    # Pre-load forecasts
    start_d = date(year, month, 1)
    end_d = all_days[-1]
    forecasts: dict[date, DailyForecast] = {
        f.date: f for f in DailyForecast.query.filter(
            DailyForecast.date >= start_d,
            DailyForecast.date <= end_d,
        ).all()
    }

    # Pre-load excluded dates per employee
    excluded_map: dict[int, set] = {
        emp.id: {ed.date for ed in emp.excluded_dates} for emp in employees
    }

    # Day-of-week restrictions per employee: {emp_id: {dow: DayRestriction}}
    restriction_map: dict[int, dict] = {}
    for emp in employees:
        restriction_map[emp.id] = {
            r.day_of_week: r for r in emp.day_restrictions
        }

    def _is_day_off(emp_id, dow):
        r = restriction_map[emp_id].get(dow)
        return bool(r and r.is_off)

    # ── Calculate per-employee monthly targets ──────────────────────────────
    weekdays = [d for d in all_days if d.weekday() < 5 and d not in holidays]
    saturdays = [d for d in all_days if d.weekday() == 5]
    sundays = [d for d in all_days if d.weekday() == 6]

    # Kept per-employee (not just used locally) — needed later to work out, on any
    # given day, how many more eligible days each employee has left this month,
    # which is what lets the day-by-day pass prioritize fairly under the cap.
    emp_elig: dict[int, dict[str, list]] = {}

    targets: dict[int, dict] = {}
    for emp in employees:
        elig_wd = [
            d for d in weekdays
            if d not in excluded_map[emp.id] and not _is_day_off(emp.id, d.weekday())
        ]
        elig_sat = [
            d for d in saturdays
            if d not in excluded_map[emp.id] and emp.works_saturday == 'yes'
            and not _is_day_off(emp.id, 5)
        ]
        elig_sun = [
            d for d in sundays
            if d not in excluded_map[emp.id] and emp.works_sunday == 'yes'
            and not _is_day_off(emp.id, 6)
        ]
        emp_elig[emp.id] = {'wd': elig_wd, 'sat': elig_sat, 'sun': elig_sun}
        frac = emp.fte_percent / 100.0

        if emp.fte_mode == 'days':
            twd = round(len(elig_wd) * frac)
            tsat = round(len(elig_sat) * frac)
            tsun = round(len(elig_sun) * frac)
            hours_factor = 1.0
        elif emp.fte_mode == 'hours':
            twd = len(elig_wd)
            tsat = len(elig_sat)
            tsun = len(elig_sun)
            hours_factor = frac
        else:  # combined
            f2 = sqrt(frac)
            twd = round(len(elig_wd) * f2)
            tsat = round(len(elig_sat) * f2)
            tsun = round(len(elig_sun) * f2)
            hours_factor = f2

        targets[emp.id] = {
            'target_wd': twd, 'target_sat': tsat, 'target_sun': tsun,
            'assigned_wd': 0, 'assigned_sat': 0, 'assigned_sun': 0,
            'hours_factor': hours_factor,
            'total_hours': 0.0,
        }

    # ── Day-by-day assignment ──────────────────────────────────────────────
    assignments: list[ShiftAssignment] = []
    daily_coverage: dict[date, dict] = {}

    def _remaining_opportunities(emp_id, day_type, day):
        """How many more eligible days of this type (incl. today) remain this month."""
        elig_list = emp_elig[emp_id][day_type]
        return len(elig_list) - bisect.bisect_left(elig_list, day)

    def _urgency(emp_id, day_type, day):
        """None if this employee isn't a work candidate today — either they no longer
        need this day-type this month, or (see below) they have slack and are already
        on/ahead of an even pace, so today is deliberately their day off. Otherwise,
        how "at risk" they are of falling short of their monthly target — remaining
        shifts still owed divided by remaining chances to work them. Rises
        automatically the more days they get deferred, which is what lets a
        capped-out day self-correct on a later one instead of permanently shorting
        someone's hours."""
        t = targets[emp_id]
        remaining_needed = t[f'target_{day_type}'] - t[f'assigned_{day_type}']
        if remaining_needed <= 0:
            return None
        remaining_opps = _remaining_opportunities(emp_id, day_type, day)
        if remaining_opps <= 0:
            return float('inf')
        if remaining_needed >= remaining_opps:
            # Zero or negative slack left — must work every remaining eligible day
            # to hit target, no exceptions. Made explicit (rather than relying on
            # the ratio below happening to reach 1.0) because the pace gate right
            # after this must never be allowed to override it.
            return float('inf')

        # There's slack. With a coverage cap that's rarely tight, nothing else here
        # holds anyone back — so without this gate, slack always gets spent
        # immediately, which is exactly what front-loads a part-time employee's
        # whole month into an early work streak and dumps every day off at the end
        # instead of spreading them out. If this employee is already at or ahead of
        # an even pace through the month, take today off and revisit tomorrow.
        elig_list = emp_elig[emp_id][day_type]
        opportunities_so_far = bisect.bisect_right(elig_list, day)  # today, inclusive
        expected_by_now = t[f'target_{day_type}'] * opportunities_so_far / len(elig_list)
        if t[f'assigned_{day_type}'] >= expected_by_now:
            return None

        return remaining_needed / remaining_opps

    def _unit_shift(representative, dow, is_saturday, is_sunday, day):
        """The single shift/hours a schedule-group unit shares, computed from
        whichever member represents the group today (same idea as before: one
        member's eligibility/settings decide the shared outcome)."""
        shift_start, shift_end, base_hours = _shift_details(
            representative, is_saturday, is_sunday, sat_hours, sun_hours, day
        )
        t = targets[representative.id]
        if t['hours_factor'] < 1.0 and not is_saturday and not is_sunday:
            shift_end, base_hours = _adjust_shift_for_hours_mode(
                shift_start, base_hours, t['hours_factor']
            )
        rotation = representative.effective_rotation_pattern
        default_tpl_id = (_rotation_shift_for_date(rotation, day).id if rotation
                           else representative.shift_template_id)

        dow_restriction = restriction_map[representative.id].get(dow)
        if dow_restriction and dow_restriction.shift_type:
            tpl = ShiftTemplate.query.filter_by(name=dow_restriction.shift_type.capitalize()).first()
            if tpl:
                shift_start, shift_end, base_hours = tpl.start_time, tpl.end_time, tpl.hours
                emp_tpl_id = tpl.id
            else:
                emp_tpl_id = default_tpl_id
        else:
            emp_tpl_id = default_tpl_id
        return shift_start, shift_end, round(base_hours, 2), emp_tpl_id

    # Most recently worked (date, shift_end) per employee, updated at the end
    # of each day's Pass 4 below — lets the rest-period check compare today's
    # would-be start against whichever day they actually last worked, even if
    # that wasn't yesterday (a day off in between naturally clears the risk).
    last_work_end: dict[int, tuple] = {}

    def _today_shift_start(emp, day, dow, is_saturday, is_sunday):
        """The shift start this employee would actually get today, including
        any day-of-week override — mirrors _unit_shift's own resolution
        order (override wins regardless of weekend/rotation), just returning
        only the start time. Needed here because the rest-period gate runs
        in Pass 1, before Pass 2/3 have decided who's actually selected."""
        dow_restriction = restriction_map[emp.id].get(dow)
        if dow_restriction and dow_restriction.shift_type:
            tpl = ShiftTemplate.query.filter_by(name=dow_restriction.shift_type.capitalize()).first()
            if tpl:
                return tpl.start_time
        start, _, _ = _shift_details(emp, is_saturday, is_sunday, sat_hours, sun_hours, day)
        return start

    def _violates_rest_period(emp, day, dow, is_saturday, is_sunday):
        """True if today's shift would start less than min_rest_hours after
        this employee's most recently worked shift ended. Computed in
        absolute minutes from whichever day they last actually worked (not
        assumed to be yesterday), so a rest day in between correctly clears
        it, and a shift crossing midnight is handled the same way — no
        special-casing needed for either."""
        prev = last_work_end.get(emp.id)
        if prev is None:
            return False
        prev_date, prev_end = prev

        today_start = _today_shift_start(emp, day, dow, is_saturday, is_sunday)
        gap_days = (day - prev_date).days
        gap_minutes = gap_days * 24 * 60 + _time_to_minutes(today_start) - _time_to_minutes(prev_end)
        return gap_minutes < min_rest_hours * 60

    for day in all_days:
        dow = day.weekday()
        is_sunday = dow == 6
        is_saturday = dow == 5
        is_holiday = day in holidays
        is_workday = not is_saturday and not is_sunday and not is_holiday
        day_type = 'sat' if is_saturday else ('sun' if is_sunday else 'wd')

        fc = forecasts.get(day)
        if fc and fc.required_ks_agents:
            required = fc.required_ks_agents
        elif is_workday:
            required = default_agents_wd
        elif is_saturday:
            required = default_agents_sat
        elif is_sunday:
            required = default_agents_sun
        else:
            required = 0

        # The client only pays for coverage up to this ceiling — never knowingly
        # schedule more than this many people to work today.
        max_work_today = int(required * max_coverage_ratio)

        def _record(emp, status, shift_start=None, shift_end=None,
                    hours_worked=0, shift_template_id=None, notes=None):
            assignments.append(ShiftAssignment(
                schedule_id=schedule_id, employee_id=emp.id, date=day,
                status=status, shift_start=shift_start, shift_end=shift_end,
                hours_worked=hours_worked, shift_template_id=shift_template_id, notes=notes,
            ))

        # ── Pass 1: hard exclusions — always apply first, even for grouped
        # employees. These are never overridden by group alignment or by the
        # coverage cap. ──────────────────────────────────────────────────
        excluded_today: dict[int, tuple] = {}
        for emp in employees:
            if is_holiday and not emp.works_holidays:
                excluded_today[emp.id] = ('holiday_off', None)
            elif is_saturday and emp.works_saturday == 'no':
                excluded_today[emp.id] = ('weekend_off', None)
            elif is_sunday and emp.works_sunday == 'no':
                excluded_today[emp.id] = ('weekend_off', None)
            elif day in excluded_map[emp.id]:
                excluded_today[emp.id] = ('constraint_off', 'Excluded date')
            elif _is_day_off(emp.id, dow):
                excluded_today[emp.id] = ('constraint_off', 'Recurring day off')
            elif _violates_rest_period(emp, day, dow, is_saturday, is_sunday):
                excluded_today[emp.id] = ('constraint_off', 'Minimum rest period')

        # ── Pass 2: build work-candidate units among everyone not hard-excluded.
        # A schedule-group's members are one unit — selected or deferred together —
        # since a group is only meaningful if it isn't split by the cap. ───────
        units = []
        seen_groups = set()
        for emp in employees:
            if emp.id in excluded_today:
                continue
            if emp.schedule_group_id:
                if emp.schedule_group_id in seen_groups:
                    continue
                seen_groups.add(emp.schedule_group_id)
                members = [e for e in employees
                           if e.schedule_group_id == emp.schedule_group_id
                           and e.id not in excluded_today]
            else:
                members = [emp]

            urgencies = [u for u in (_urgency(m.id, day_type, day) for m in members) if u is not None]
            if not urgencies:
                continue  # nobody in this unit still needs this day-type this month
            units.append({'members': members, 'urgency': max(urgencies), 'size': len(members)})

        # ── Pass 3: greedily fill today's slots with the most at-risk units first,
        # never exceeding the cap. Whoever doesn't fit gets deferred, not dropped —
        # their own remaining_needed is untouched, so they're simply more urgent
        # (and thus higher-priority) the next time they're eligible. ───────────
        units.sort(key=lambda u: -u['urgency'])
        selected_ids: set[int] = set()
        scheduled_count = 0
        for u in units:
            if scheduled_count + u['size'] <= max_work_today:
                selected_ids.update(m.id for m in u['members'])
                scheduled_count += u['size']

        # Everyone who was actually a live candidate today (had urgency, i.e.
        # made it into `units`) but didn't fit under the cap — distinct from
        # anyone who was never a candidate at all today (either genuinely
        # done for the month, or paced off by _urgency's pacing gate while
        # still owing days). Both of those still-owing-but-not-competing
        # cases used to collapse into the same "Coverage cap reached" label
        # below (both have remaining_needed > 0), which was misleading — a
        # paced employee was never actually blocked by the cap at all.
        capped_ids = {m.id for u in units for m in u['members']} - selected_ids

        # ── Pass 4: record everything ────────────────────────────────────────
        for emp in employees:
            if emp.id in excluded_today:
                status, notes = excluded_today[emp.id]
                _record(emp, status, notes=notes)
                continue

            if emp.id not in selected_ids:
                notes = 'Coverage cap reached' if emp.id in capped_ids else None
                _record(emp, 'day_off', notes=notes)
                continue

        # Assign shifts per selected unit (grouped units share one computed shift).
        for u in units:
            member_ids = {m.id for m in u['members']}
            if not member_ids & selected_ids:
                continue
            representative = u['members'][0]
            shift_start, shift_end, base_hours, emp_tpl_id = _unit_shift(
                representative, dow, is_saturday, is_sunday, day
            )
            for m in u['members']:
                _record(m, 'work', shift_start=shift_start, shift_end=shift_end,
                        hours_worked=base_hours, shift_template_id=emp_tpl_id)
                last_work_end[m.id] = (day, shift_end)
                t = targets[m.id]
                if is_saturday:
                    t['assigned_sat'] += 1
                elif is_sunday:
                    t['assigned_sun'] += 1
                else:
                    t['assigned_wd'] += 1
                t['total_hours'] += base_hours

        daily_coverage[day] = {
            'required': required,
            'scheduled': scheduled_count,
            'pct': round(scheduled_count / required * 100, 1) if required else 100.0,
        }

    # Bulk-insert
    db.session.bulk_save_objects(assignments)
    db.session.commit()

    def _polish_coverage_dips():
        """Post-hoc improvement pass, run once after the day-by-day build
        above: some weekdays can still sit below their forecast requirement
        even though total capacity for the month was enough — typically
        because several employees who share the same FTE% (and thus
        identical pace math from _urgency's pacing gate) all land on the
        same day as their discretionary day off. Look for a discretionary
        day off (day_off with notes=None — NOT the "wanted to work but
        capped" case) that can be relocated onto a still-short day from a
        day with coverage to spare, without changing anyone's total
        worked-day count (so the "nobody falls short" guarantee holds by
        construction, not by re-checking it) and without violating the
        rest-period rule. Never touches schedule-group members — moving one
        member alone would break "the group always moves together".
        Weekdays only, matching where this was actually observed. Targets
        100% of requirement, not the 105% cap — this closes gaps, it
        doesn't try to maximize coverage further."""
        by_emp_date: dict[tuple, ShiftAssignment] = {
            (a.employee_id, a.date): a
            for a in ShiftAssignment.query.filter_by(schedule_id=schedule_id).all()
        }
        emp_by_id = {e.id: e for e in employees}
        polishable_ids = {e.id for e in employees if not e.schedule_group_id}

        def _rest_ok_to_work(emp_id, day, new_start, new_end):
            # Only the "moving onto day" direction needs checking — vacating
            # a day can only improve a neighbor's rest gap, never worsen it.
            # If the donor day being vacated happens to be an immediate
            # neighbor of `day`, it's still evaluated here in its pre-swap
            # (working) state — occasionally overcautious in that narrow
            # case, never unsafe: worst case a valid move is skipped, an
            # invalid one is never taken.
            prev = by_emp_date.get((emp_id, day - timedelta(days=1)))
            if prev is not None and prev.status == 'work':
                gap = _time_to_minutes(new_start) + 24 * 60 - _time_to_minutes(prev.shift_end)
                if gap < min_rest_hours * 60:
                    return False
            nxt = by_emp_date.get((emp_id, day + timedelta(days=1)))
            if nxt is not None and nxt.status == 'work':
                gap = _time_to_minutes(nxt.shift_start) + 24 * 60 - _time_to_minutes(new_end)
                if gap < min_rest_hours * 60:
                    return False
            return True

        deficit_days = sorted(
            (d for d in weekdays if daily_coverage[d]['required'] > 0
             and daily_coverage[d]['scheduled'] < daily_coverage[d]['required']),
            key=lambda d: daily_coverage[d]['pct']
        )

        for x in deficit_days:
            while daily_coverage[x]['scheduled'] < daily_coverage[x]['required']:
                moved = False
                for emp_id in polishable_ids:
                    row_x = by_emp_date.get((emp_id, x))
                    if row_x is None or row_x.status != 'day_off' or row_x.notes is not None:
                        continue

                    emp = emp_by_id[emp_id]
                    new_start, new_end, new_hours, new_tpl_id = _unit_shift(
                        emp, x.weekday(), False, False, x
                    )
                    if not _rest_ok_to_work(emp_id, x, new_start, new_end):
                        continue

                    donor_day = None
                    for y in emp_elig[emp_id]['wd']:
                        if y == x:
                            continue
                        row_y = by_emp_date.get((emp_id, y))
                        if row_y is None or row_y.status != 'work':
                            continue
                        if daily_coverage[y]['scheduled'] - 1 < daily_coverage[y]['required']:
                            continue
                        donor_day = y
                        break
                    if donor_day is None:
                        continue

                    row_y = by_emp_date[(emp_id, donor_day)]
                    old_hours = row_y.hours_worked or 0

                    row_x.status, row_x.shift_start, row_x.shift_end = 'work', new_start, new_end
                    row_x.hours_worked, row_x.shift_template_id, row_x.notes = new_hours, new_tpl_id, None

                    row_y.status, row_y.shift_start, row_y.shift_end = 'day_off', None, None
                    row_y.hours_worked, row_y.shift_template_id, row_y.notes = 0, None, None

                    daily_coverage[x]['scheduled'] += 1
                    daily_coverage[x]['pct'] = round(
                        daily_coverage[x]['scheduled'] / daily_coverage[x]['required'] * 100, 1)
                    daily_coverage[donor_day]['scheduled'] -= 1
                    req_y = daily_coverage[donor_day]['required']
                    daily_coverage[donor_day]['pct'] = (
                        round(daily_coverage[donor_day]['scheduled'] / req_y * 100, 1) if req_y else 100.0)

                    targets[emp_id]['total_hours'] += new_hours - old_hours

                    moved = True
                    break
                if not moved:
                    break

        db.session.commit()

    _polish_coverage_dips()

    # Build summary
    emp_summary = []
    for emp in employees:
        t = targets[emp.id]
        rotation = emp.effective_rotation_pattern
        elig_wd = emp_elig[emp.id]['wd']
        if rotation and elig_wd:
            # Weekday hours vary by rotation week — average across this employee's
            # actual eligible weekdays this month rather than one fixed snapshot.
            std_h = sum(_shift_details(emp, False, False, sat_hours, sun_hours, d)[2]
                        for d in elig_wd) / len(elig_wd)
        else:
            std_h = _shift_details(emp, False, False, sat_hours, sun_hours, all_days[0])[2]
        target_hours = (t['target_wd'] * std_h * t['hours_factor'] +
                        t['target_sat'] * sat_hours + t['target_sun'] * sun_hours)
        emp_summary.append({
            'id': emp.id, 'name': emp.name, 'team': emp.team,
            'fte_percent': emp.fte_percent, 'fte_mode': emp.fte_mode,
            'target_wd': t['target_wd'], 'assigned_wd': t['assigned_wd'],
            'target_sat': t['target_sat'], 'assigned_sat': t['assigned_sat'],
            'target_sun': t['target_sun'], 'assigned_sun': t['assigned_sun'],
            'target_hours': round(target_hours, 1),
            'actual_hours': round(t['total_hours'], 1),
        })
        db.session.add(EmployeeScheduleSummary(
            schedule_id=schedule_id, employee_id=emp.id,
            target_wd=t['target_wd'], assigned_wd=t['assigned_wd'],
            target_sat=t['target_sat'], assigned_sat=t['assigned_sat'],
            target_sun=t['target_sun'], assigned_sun=t['assigned_sun'],
            target_hours=round(target_hours, 1), actual_hours=round(t['total_hours'], 1),
        ))
    db.session.commit()

    return {
        'employee_summary': emp_summary,
        'daily_coverage': {
            str(d): v for d, v in daily_coverage.items()
        },
        'total_employees': len(employees),
        'avg_coverage_pct': round(
            sum(v['pct'] for v in daily_coverage.values()) / len(daily_coverage), 1
        ) if daily_coverage else 0,
    }
