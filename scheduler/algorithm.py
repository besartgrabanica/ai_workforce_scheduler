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

from .models import (db, BusinessParam, Employee, DailyForecast, ExcludedDate,
                      ShiftAssignment, ShiftTemplate)

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


def _shift_details(emp: Employee, is_saturday: bool, is_sunday: bool,
                    sat_hours: float, sun_hours: float) -> tuple[str, str, float]:
    """Return (shift_start, shift_end, hours)."""
    if is_saturday:
        return '08:00', _add_hours('08:00', sat_hours), sat_hours

    if is_sunday:
        return '08:00', _add_hours('08:00', sun_hours), sun_hours

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
        """None if this employee no longer needs to work this day-type this month.
        Otherwise, how "at risk" they are of falling short of their monthly target —
        remaining shifts still owed divided by remaining chances to work them.
        Rises automatically the more days they get deferred, which is what lets a
        capped-out day self-correct on a later one instead of permanently shorting
        someone's hours."""
        t = targets[emp_id]
        remaining_needed = t[f'target_{day_type}'] - t[f'assigned_{day_type}']
        if remaining_needed <= 0:
            return None
        remaining_opps = _remaining_opportunities(emp_id, day_type, day)
        if remaining_opps <= 0:
            return float('inf')
        return remaining_needed / remaining_opps

    def _unit_shift(representative, dow, is_saturday, is_sunday):
        """The single shift/hours a schedule-group unit shares, computed from
        whichever member represents the group today (same idea as before: one
        member's eligibility/settings decide the shared outcome)."""
        shift_start, shift_end, base_hours = _shift_details(
            representative, is_saturday, is_sunday, sat_hours, sun_hours
        )
        t = targets[representative.id]
        if t['hours_factor'] < 1.0 and not is_saturday and not is_sunday:
            shift_end, base_hours = _adjust_shift_for_hours_mode(
                shift_start, base_hours, t['hours_factor']
            )
        dow_restriction = restriction_map[representative.id].get(dow)
        if dow_restriction and dow_restriction.shift_type:
            tpl = ShiftTemplate.query.filter_by(name=dow_restriction.shift_type.capitalize()).first()
            if tpl:
                shift_start, shift_end, base_hours = tpl.start_time, tpl.end_time, tpl.hours
                emp_tpl_id = tpl.id
            else:
                emp_tpl_id = representative.shift_template_id
        else:
            emp_tpl_id = representative.shift_template_id
        return shift_start, shift_end, round(base_hours, 2), emp_tpl_id

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

        # ── Pass 4: record everything ────────────────────────────────────────
        for emp in employees:
            if emp.id in excluded_today:
                status, notes = excluded_today[emp.id]
                _record(emp, status, notes=notes)
                continue

            if emp.id not in selected_ids:
                t = targets[emp.id]
                remaining_needed = t[f'target_{day_type}'] - t[f'assigned_{day_type}']
                notes = 'Coverage cap reached' if remaining_needed > 0 else None
                _record(emp, 'day_off', notes=notes)
                continue

        # Assign shifts per selected unit (grouped units share one computed shift).
        for u in units:
            member_ids = {m.id for m in u['members']}
            if not member_ids & selected_ids:
                continue
            representative = u['members'][0]
            shift_start, shift_end, base_hours, emp_tpl_id = _unit_shift(
                representative, dow, is_saturday, is_sunday
            )
            for m in u['members']:
                _record(m, 'work', shift_start=shift_start, shift_end=shift_end,
                        hours_worked=base_hours, shift_template_id=emp_tpl_id)
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

    # Build summary
    emp_summary = []
    for emp in employees:
        t = targets[emp.id]
        std_start, std_end, std_h = _shift_details(emp, False, False, sat_hours, sun_hours)
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
