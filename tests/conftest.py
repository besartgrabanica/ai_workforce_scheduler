"""
Shared fixtures for the scheduling-algorithm regression suite.

Every test runs against a fresh, throwaway SQLite file (never the real
instance/eon or instance/freenet databases) bound as g.active_engine for the
duration of the test, using the exact same routing mechanism
(scheduler.models.ProjectScopedSession) the real app uses per-request. This
gives full test isolation while exercising the real ORM models and the real
generate_schedule() code path, not a mock of it.
"""
import os
import tempfile

import pytest
from sqlalchemy import create_engine

from app import app as flask_app
from app import db
from scheduler.algorithm import set_holidays
from scheduler.models import BusinessParam, Employee, ShiftTemplate


@pytest.fixture
def ctx():
    """A Flask app context with g.active_engine pointed at a fresh, empty
    SQLite file with the full schema created. Torn down and deleted after
    the test. Holidays are reset to empty so tests are independent."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    engine = create_engine(f'sqlite:///{path}')
    db.metadatas[None].create_all(bind=engine)

    with flask_app.app_context():
        from flask import g
        g.active_engine = engine
        g.active_project = 'test'
        set_holidays({})
        yield

    engine.dispose()
    os.remove(path)


@pytest.fixture
def identity_ctx():
    """A Flask app context with the identity bind (IdentityUser/ProjectRole/
    GlobalSetting/...) pointed at a fresh, empty throwaway SQLite file
    instead of the real instance/identity.db. These models carry
    __bind_key__='identity', so ProjectScopedSession routes them straight to
    db.engines['identity'] regardless of g.active_engine/g.active_project —
    unlike `ctx`, this fixture doesn't need either of those set. Does not
    depend on `ctx` and can be combined with it freely in the same test."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    engine = create_engine(f'sqlite:///{path}')
    db.metadatas['identity'].create_all(bind=engine)

    with flask_app.app_context():
        prev_engine = db.engines.get('identity')
        db.engines['identity'] = engine
        yield
        db.engines['identity'] = prev_engine

    engine.dispose()
    os.remove(path)


@pytest.fixture
def default_template(ctx):
    """The fallback template Employee.effective_shift_template resolves to
    when an employee has no shift_template_id of their own set."""
    tpl = ShiftTemplate(name='Full', start_time='08:00', end_time='16:30',
                         hours=7.5, color='#22c55e', is_default=True)
    db.session.add(tpl)
    db.session.commit()
    return tpl


def make_employee(name, fte_percent=100, fte_mode='days', **kwargs):
    """Creates and persists an Employee with sane weekday-only defaults,
    overridable via kwargs. Callers still need the `default_template`
    fixture active so effective_shift_template resolves to something."""
    kwargs.setdefault('works_saturday', 'no')
    kwargs.setdefault('works_sunday', 'no')
    kwargs.setdefault('works_holidays', False)
    kwargs.setdefault('status', 'active')
    kwargs.setdefault('team', 'TestTeam')
    emp = Employee(name=name, fte_percent=fte_percent, fte_mode=fte_mode, **kwargs)
    db.session.add(emp)
    db.session.commit()
    return emp


def set_param(key, value, label=None, category='Test'):
    """Insert or update a BusinessParam row (generate_schedule reads these
    with a fallback default, so tests only need to set the ones they care
    about deviating from the built-in defaults)."""
    p = BusinessParam.query.filter_by(key=key).first()
    if p:
        p.value = str(value)
    else:
        db.session.add(BusinessParam(key=key, value=str(value), label=label or key, category=category))
    db.session.commit()
