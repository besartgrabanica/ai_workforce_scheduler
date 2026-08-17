"""
Regression suite for the identity/role model (scheduler/models.py's
IdentityUser/ProjectRole) and the app.py decorators that gate on it
(require_role/require_technical_admin/require_global_admin).

Uses the `identity_ctx` fixture (tests/conftest.py) — a throwaway SQLite
file swapped in for db.engines['identity'] — so nothing here ever touches
the real instance/identity.db. The decorators are tested directly (wrapping
a trivial dummy view) rather than through a specific business route: the
decorators themselves are the unit under test, and their behavior doesn't
depend on which view they happen to wrap.
"""
from flask import g

from app import app as flask_app
from app import db, require_global_admin, require_role, require_technical_admin
from scheduler.models import IdentityUser, ProjectRole


def _make_user(username, global_role=None):
    user = IdentityUser(username=username, email=f'{username}@example.com', global_role=global_role)
    user.set_password('irrelevant')
    db.session.add(user)
    db.session.commit()
    return user


def _grant(user, project, role):
    db.session.add(ProjectRole(user_id=user.id, project=project, role=role))
    db.session.commit()


# ── IdentityUser.role_for() composition ─────────────────────────────────────

def test_role_for_uses_project_grant_when_no_global_role(identity_ctx):
    user = _make_user('project_editor')
    _grant(user, 'eon', 'editor')

    assert user.role_for('eon') == 'editor'
    assert user.role_for('freenet') is None


def test_role_for_uses_global_role_when_no_project_grant(identity_ctx):
    user = _make_user('global_viewer', global_role='viewer')

    assert user.role_for('eon') == 'viewer'
    assert user.role_for('freenet') == 'viewer'


def test_role_for_composes_as_the_higher_of_global_and_project(identity_ctx):
    """A global viewer with a per-project editor grant on their own team's
    project is 'editor' there specifically, but only 'viewer' elsewhere —
    the two grants compose (higher wins) rather than one replacing the other."""
    user = _make_user('mixed', global_role='viewer')
    _grant(user, 'eon', 'editor')

    assert user.role_for('eon') == 'editor'
    assert user.role_for('freenet') == 'viewer'


def test_project_role_ceiling_is_admin_even_with_higher_global_role(identity_ctx):
    """A per-project grant can never itself be 'superadmin' — that tier only
    exists at the global level. Directly setting one to 'admin' (the
    documented ceiling) must still resolve correctly against a higher
    global_role via the 'higher wins' rule."""
    user = _make_user('capped', global_role='superadmin')
    _grant(user, 'eon', 'admin')

    assert user.role_for('eon') == 'superadmin'  # global superadmin still wins here
    assert user.has_role('eon', 'superadmin')


# ── IdentityUser.accessible_projects() / has_role() ─────────────────────────

def test_accessible_projects_global_role_sees_everything_known(identity_ctx):
    user = _make_user('global_admin', global_role='admin')
    assert user.accessible_projects(['eon', 'freenet']) == ['eon', 'freenet']


def test_accessible_projects_without_global_role_is_grants_only(identity_ctx):
    user = _make_user('scoped')
    _grant(user, 'eon', 'viewer')
    # A stale grant pointing at a project that no longer exists must not leak through.
    _grant(user, 'decommissioned', 'admin')

    assert user.accessible_projects(['eon', 'freenet']) == ['eon']


def test_has_role_respects_minimum_threshold(identity_ctx):
    user = _make_user('viewer_only')
    _grant(user, 'eon', 'viewer')

    assert user.has_role('eon', 'viewer') is True
    assert user.has_role('eon', 'editor') is False


def test_is_technical_admin_requires_true_global_superadmin(identity_ctx):
    project_admin = _make_user('project_admin')
    _grant(project_admin, 'eon', 'admin')
    global_admin = _make_user('global_admin', global_role='admin')
    global_superadmin = _make_user('global_superadmin', global_role='superadmin')

    assert project_admin.is_technical_admin is False
    assert global_admin.is_technical_admin is False  # 'admin' is not enough, only 'superadmin'
    assert global_superadmin.is_technical_admin is True


# ── Decorators (app.py) ──────────────────────────────────────────────────────

def _dummy_view():
    return 'allowed'


def test_require_role_blocks_below_threshold_and_allows_at_or_above(identity_ctx):
    user = _make_user('editor_in_eon')
    _grant(user, 'eon', 'editor')
    view = require_role('admin')(_dummy_view)

    with flask_app.test_request_context('/'):
        g.current_user = user
        g.active_project = 'eon'
        resp = view()
        assert resp != 'allowed'  # editor < admin -> redirected away, not the real view's return value

    view_ok = require_role('editor')(_dummy_view)
    with flask_app.test_request_context('/'):
        g.current_user = user
        g.active_project = 'eon'
        assert view_ok() == 'allowed'


def test_require_role_blocks_when_no_active_project():
    user = IdentityUser(username='no_project', global_role='superadmin')
    view = require_role('viewer')(_dummy_view)

    with flask_app.test_request_context('/'):
        g.current_user = user
        g.active_project = None
        assert view() != 'allowed'


def test_require_technical_admin_blocks_global_admin_but_allows_superadmin(identity_ctx):
    global_admin = _make_user('ta_admin', global_role='admin')
    global_superadmin = _make_user('ta_superadmin', global_role='superadmin')
    view = require_technical_admin(_dummy_view)

    with flask_app.test_request_context('/'):
        g.current_user = global_admin
        assert view() != 'allowed'

    with flask_app.test_request_context('/'):
        g.current_user = global_superadmin
        assert view() == 'allowed'


def test_require_global_admin_blocks_project_admin_but_allows_global_admin(identity_ctx):
    project_admin = _make_user('ga_project_admin')
    _grant(project_admin, 'eon', 'admin')
    global_admin = _make_user('ga_global_admin', global_role='admin')
    view = require_global_admin(_dummy_view)

    with flask_app.test_request_context('/'):
        g.current_user = project_admin
        g.active_project = 'eon'
        assert view() != 'allowed'  # per-project admin must never reach a global-admin gate

    with flask_app.test_request_context('/'):
        g.current_user = global_admin
        assert view() == 'allowed'
