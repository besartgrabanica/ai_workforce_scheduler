"""
Regression suite for the new dashboard JSON endpoints (app.py): coverage
trend, intraday heatmap, and import health. These are plain GET routes with
no auth decorator (same as the pre-existing dashboard_forecast_trend), so
they're called directly here inside a test_request_context + the `ctx`
fixture's throwaway engine, bypassing the login flow entirely — consistent
with how the rest of this suite exercises real app/DB code without needing
a full HTTP round trip.
"""
from datetime import date, datetime, timedelta

from app import app as flask_app
from app import db
from scheduler.models import (
    DailyForecast,
    ForecastPeriod,
    HalfHourlyForecast,
    ImportLog,
    Schedule,
    ShiftAssignment,
    ShiftTemplate,
)
from tests.conftest import make_employee


def _call(endpoint, query_string=''):
    with flask_app.test_request_context(f'/{query_string}'):
        return flask_app.view_functions[endpoint]().json


def test_coverage_trend_uses_daily_forecast_and_falls_back_to_business_param(default_template):
    from tests.conftest import set_param
    set_param('default_agents_weekday', 42)

    emp = make_employee('Agron Krasniqi', fte_percent=100)
    sched = Schedule(name='Test', year=2026, month=6)
    db.session.add(sched)
    db.session.commit()

    tpl = ShiftTemplate.query.filter_by(is_default=True).first()
    db.session.add(ShiftAssignment(schedule_id=sched.id, employee_id=emp.id, date=date(2026, 6, 1),
                                    status='work', shift_start='08:00', shift_end='16:30',
                                    hours_worked=7.5, shift_template_id=tpl.id))

    period = ForecastPeriod(name='June', start_date=date(2026, 6, 1), end_date=date(2026, 6, 30))
    db.session.add(period)
    db.session.flush()
    db.session.add(DailyForecast(period_id=period.id, date=date(2026, 6, 2), required_ks_agents=7))
    db.session.commit()

    data = _call('dashboard_coverage_trend', f'?schedule_id={sched.id}')

    assert data['labels'][0] == '01.06'
    assert data['required'][0] == 42   # June 1, 2026 is a Monday -> BusinessParam fallback
    assert data['scheduled'][0] == 1
    assert data['required'][1] == 7    # June 2 has a DailyForecast row -> that wins
    assert data['scheduled'][1] == 0


def test_coverage_trend_with_no_schedules_returns_empty(default_template):
    data = _call('dashboard_coverage_trend')
    assert data == {'labels': [], 'required': [], 'scheduled': []}


def test_intraday_heatmap_expands_shift_span_across_slots(default_template):
    emp = make_employee('Blerta Hoxha', fte_percent=100)
    sched = Schedule(name='Test', year=2026, month=6)
    db.session.add(sched)
    db.session.commit()
    db.session.add(ShiftAssignment(schedule_id=sched.id, employee_id=emp.id, date=date(2026, 6, 1),
                                    status='work', shift_start='08:00', shift_end='09:00'))

    period = ForecastPeriod(name='June', start_date=date(2026, 6, 1), end_date=date(2026, 6, 30))
    db.session.add(period)
    db.session.flush()
    fc = DailyForecast(period_id=period.id, date=date(2026, 6, 1))
    db.session.add(fc)
    db.session.flush()
    db.session.add_all([
        HalfHourlyForecast(daily_id=fc.id, slot_time='08:00', sync_volume=10),
        HalfHourlyForecast(daily_id=fc.id, slot_time='08:30', sync_volume=5),
        HalfHourlyForecast(daily_id=fc.id, slot_time='09:00', sync_volume=2),
    ])
    db.session.commit()

    data = _call('dashboard_intraday_heatmap', '?date=2026-06-01')

    assert data['date'] == '2026-06-01'
    assert data['slots'] == ['08:00', '08:30', '09:00']
    assert data['demand'] == [10.0, 5.0, 2.0]
    # 08:00-09:00 shift covers the 08:00 and 08:30 slots, not the 09:00 slot (end is exclusive).
    assert data['scheduled'] == [1, 1, 0]


def test_intraday_heatmap_with_no_forecast_returns_empty(default_template):
    data = _call('dashboard_intraday_heatmap')
    assert data == {'date': None, 'slots': [], 'demand': [], 'scheduled': []}


def test_import_health_buckets_by_day_and_level(default_template):
    today = date.today()
    db.session.add_all([
        ImportLog(source='tfc_forecast', filename='a.xlsx', level='warning', message='x',
                  created_at=datetime.combine(today, datetime.min.time())),
        ImportLog(source='tfc_forecast', filename='b.xlsx', level='error', message='y',
                  created_at=datetime.combine(today, datetime.min.time())),
        ImportLog(source='tfc_forecast', filename='c.xlsx', level='warning', message='z',
                  created_at=datetime.combine(today - timedelta(days=1), datetime.min.time())),
    ])
    db.session.commit()

    data = _call('dashboard_import_health')

    assert data['labels'][-1] == today.strftime('%d.%m')
    assert data['warnings'][-1] == 1
    assert data['errors'][-1] == 1
    assert data['warnings'][-2] == 1
    assert len(data['labels']) == 31  # 30 days back through today, inclusive
