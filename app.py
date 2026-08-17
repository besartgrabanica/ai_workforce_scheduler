import calendar
import glob
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, send_file, session as flask_session, url_for)
from flask_babel import Babel, gettext as _, get_locale
from sqlalchemy import create_engine

load_dotenv()

logger = logging.getLogger(__name__)

from scheduler.models import (BusinessParam, ChatMessage, ChatSession,
                               DailyForecast, DayRestriction, Document,
                               Employee, EmployeeScheduleSummary, ExcludedDate, ForecastPeriod,
                               GlobalSetting, HalfHourlyForecast, IdentityUser,
                               ImportLog, Invitation, PasswordReset, ProjectMeta,
                               ProjectRole, PublicHoliday, RotationCycleWeek,
                               RotationPattern, Schedule, ScheduleGroup,
                               ShiftAssignment, ShiftTemplate, Team, db)
from scheduler.algorithm import set_holidays
from scheduler import mailer

# ────────────────────────────────────────────────────────────────────────────
# Project registry — one running app now serves every client engagement, each
# still fully isolated in its own database (never a shared schema). All
# per-project config is real data now: branding lives in ProjectMeta (identity
# bind, editable via the /projects admin page), public holidays live in each
# project's own PublicHoliday table (editable via /public-holidays). A
# project with no ProjectMeta row yet gets safe generic branding defaults.
# Identity (who can log in, which projects they can access) lives on its own
# permanently-fixed database, entirely separate from business data — see
# scheduler/models.py's ProjectScopedSession for how a single running process
# safely serves many databases per request.
# ────────────────────────────────────────────────────────────────────────────

_GENERIC_PROJECT_DEFAULTS = {
    'company': 'KiKxxl-evroTarget', 'client': '', 'site': '', 'param_category': 'Operations',
}


def project_config(key):
    base = dict(_GENERIC_PROJECT_DEFAULTS)
    base['display_name'] = key.capitalize()
    # A project created (or backfilled) via the /projects admin page fills in
    # real values over the generic fallback — the try/except covers the very
    # first startup call, before the identity DB's tables exist.
    try:
        meta = ProjectMeta.query.filter_by(key=key).first()
    except Exception:
        meta = None
    if meta:
        base.update({
            'display_name': meta.display_name,
            'company': meta.company or base['company'],
            'client': meta.client or '',
            'site': meta.site or '',
            'param_category': meta.param_category or base['param_category'],
        })
    return base


_PROJECT_COLOR_PALETTE = ['#2563eb', '#16a34a', '#d97706', '#7c3aed', '#0891b2', '#db2777']


def project_color(key):
    """Deterministic accent color per project, for the switcher UI."""
    import hashlib
    idx = int(hashlib.md5(key.encode()).hexdigest(), 16) % len(_PROJECT_COLOR_PALETTE)
    return _PROJECT_COLOR_PALETTE[idx]


def discover_projects():
    """Every project this running app can serve: anything already provisioned
    on disk (instance/<name>/workforce.db) plus anything registered via the
    /projects admin page (ProjectMeta) — the try/except covers the very first
    startup call, before the identity DB's tables exist."""
    instance_dir = os.path.join(os.path.dirname(__file__), 'instance')
    on_disk = {
        os.path.basename(os.path.dirname(p))
        for p in glob.glob(os.path.join(instance_dir, '*', 'workforce.db'))
    }
    try:
        db_registered = {p.key for p in ProjectMeta.query.all()}
    except Exception:
        db_registered = set()
    return sorted(on_disk | db_registered)


app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-kikxxl-2026')
# The "default" bind is never actually queried for business data (every
# request substitutes g.active_engine — see ProjectScopedSession) — this
# just needs to be a syntactically valid URI to satisfy Flask-SQLAlchemy's
# init-time requirement. Business tables are created per-project explicitly
# below, never through this default bind.
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///_unused_default.db'
app.config['SQLALCHEMY_BINDS'] = {'identity': 'sqlite:///identity.db'}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB upload

os.makedirs(app.instance_path, exist_ok=True)

db.init_app(app)
app.jinja_env.globals.update(enumerate=enumerate)

# ────────────────────────────────────────────────────────────────────────────
# Internationalization — English (default) / German / Albanian
# ────────────────────────────────────────────────────────────────────────────
app.config['BABEL_DEFAULT_LOCALE'] = 'en'
app.config['BABEL_TRANSLATION_DIRECTORIES'] = 'translations'


def _select_locale():
    user = getattr(g, 'current_user', None)
    if user and user.ui_locale in IdentityUser.UI_LOCALES:
        return user.ui_locale
    session_locale = flask_session.get('locale')
    if session_locale in IdentityUser.UI_LOCALES:
        return session_locale
    return request.accept_languages.best_match(IdentityUser.UI_LOCALES) or 'en'


babel = Babel(app, locale_selector=_select_locale)
# flask_babel.get_locale() resolves via the selector above and caches per
# request — expose it to templates so the language switcher can mark the
# active button (it isn't a Jinja global by default, unlike _()/gettext).
app.jinja_env.globals['get_locale'] = get_locale


def _unique(iterable):
    seen = []
    for item in iterable:
        if item not in seen:
            seen.append(item)
    return seen

app.jinja_env.filters['unique'] = _unique

_ROLE_LEVELS = {'viewer': 1, 'editor': 2, 'admin': 3, 'superadmin': 4}


def require_role(min_role):
    """Gate by effective role IN THE ACTIVE PROJECT (the higher of global_role
    and any project-specific grant) — use for ordinary per-project permissions."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = getattr(g, 'current_user', None)
            role = user.role_for(g.active_project) if user and getattr(g, 'active_project', None) else None
            if _ROLE_LEVELS.get(role, 0) < _ROLE_LEVELS.get(min_role, 99):
                flash(_('You do not have permission to perform this action.'), 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_technical_admin(f):
    """Gate by TRUE global superadmin only — for AI/system config and granting
    global-scope roles. Never satisfiable via a per-project grant, no matter
    how high, since technical config is cross-cutting and not project-scoped."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = getattr(g, 'current_user', None)
        if not user or not user.is_technical_admin:
            flash(_('Only a global superadmin can do that.'), 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def require_global_admin(f):
    """Gate by GLOBAL role specifically (admin or superadmin) — never
    satisfiable via a per-project grant. For creating/managing projects
    themselves: a cross-cutting action, not something a per-project admin
    (who only controls the one project they were granted) should reach."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = getattr(g, 'current_user', None)
        if not user or user.global_role not in ('admin', 'superadmin'):
            flash(_('Only a global admin or superadmin can do that.'), 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


_SHIFTPLANNER_API_KEY = os.environ.get('SHIFTPLANNER_API_KEY', '')


def require_api_key(f):
    """Auth gate for the read-only /api/v1/* routes Cerebro's Shiftplanner
    adapter calls — a static shared secret (SHIFTPLANNER_API_KEY) via an
    X-API-Key header, mirroring the pattern Cerebro's own Plane adapter
    already uses against Plane's API. Deliberately NOT session/cookie based
    like every other route in this file: this is a machine-to-machine
    credential for another internal app, not a per-user login. See
    load_user()'s endpoint.startswith('api_') exemption — these routes skip
    the cookie-session logic entirely and bind g.active_engine themselves via
    _bind_project_or_404, scoped to whichever project key is in the URL, not
    the caller's session (there is no caller session)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _SHIFTPLANNER_API_KEY:
            return jsonify({'error': 'API not configured on this server'}), 503
        supplied = request.headers.get('X-API-Key', '')
        if not secrets.compare_digest(supplied, _SHIFTPLANNER_API_KEY):
            return jsonify({'error': 'unauthorized'}), 401
        try:
            return f(*args, **kwargs)
        except Exception:
            logger.exception('API error in %s', f.__name__)
            return jsonify({'error': 'internal error'}), 500
    return decorated


def _bind_project_or_404(project_key):
    """The API equivalent of what load_user() does from the session for
    human routes: validate project_key against discover_projects() and bind
    g.active_project/g.active_engine for this request. Returns a Flask
    response if invalid, else None."""
    if project_key not in discover_projects():
        return jsonify({'error': f'unknown project "{project_key}"'}), 404
    g.active_project = project_key
    g.active_engine = ENGINES[project_key]
    return None


def upload_folder():
    path = os.path.join(os.path.dirname(__file__), 'uploads', g.active_project)
    os.makedirs(path, exist_ok=True)
    return path


def docs_folder():
    path = os.path.join(upload_folder(), 'documents')
    os.makedirs(path, exist_ok=True)
    return path


MONTH_NAMES = ['', 'January', 'February', 'March', 'April', 'May', 'June',
               'July', 'August', 'September', 'October', 'November', 'December']


def _month_name_translations():
    """Never called — exists only so `pybabel extract` registers these as
    translatable msgids. MONTH_NAMES itself must stay English/untranslated
    (schedule default names like "Schedule January 2026" are built from it
    and stored as data) — display code calls _(MONTH_NAMES[i]) at render
    time, which looks up the same msgid this registers."""
    return [_('January'), _('February'), _('March'), _('April'), _('May'), _('June'),
            _('July'), _('August'), _('September'), _('October'), _('November'), _('December')]


def get_teams():
    """Return all team names from DB, sorted."""
    return [t.name for t in Team.query.order_by(Team.name).all()]


_LOGO_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.svg', '.webp'}


def project_logo_path(project=None):
    """Path to a project's uploaded logo file, if one exists."""
    project = project or g.active_project
    branding_dir = os.path.join(os.path.dirname(__file__), 'uploads', project, 'branding')
    matches = glob.glob(os.path.join(branding_dir, 'logo.*'))
    return matches[0] if matches else None


def _seed_defaults(project_key):
    """Insert default shift templates, business params, and teams if this
    project's database is empty. Assumes g.active_engine is already set."""
    if ShiftTemplate.query.count() == 0:
        defaults = [
            ShiftTemplate(name='Full',      start_time='08:00', end_time='16:30', hours=7.5,  color='#22c55e', is_default=True),
            ShiftTemplate(name='Morning',   start_time='08:00', end_time='14:00', hours=6.0,  color='#3b82f6'),
            ShiftTemplate(name='Afternoon', start_time='14:00', end_time='20:00', hours=6.0,  color='#f59e0b'),
            ShiftTemplate(name='Saturday',  start_time='08:00', end_time='13:30', hours=5.5,  color='#8b5cf6'),
        ]
        db.session.add_all(defaults)

    if BusinessParam.query.count() == 0:
        cat = project_config(project_key)['param_category']
        params = [
            BusinessParam(key='aht_sync',      value='10',   label='AHT Synchron (min)',       category=cat),
            BusinessParam(key='aht_async',     value='15',   label='AHT Asynchron (min)',      category=cat),
            BusinessParam(key='stk_per_hour',  value='3.5',  label='Stk./Std.',                category=cat),
            BusinessParam(key='hours_day',     value='8',    label='Std./Tag (weekday)',        category=cat),
            BusinessParam(key='hours_saturday',value='5.5',  label='Std./Tag (Saturday)',       category=cat),
            BusinessParam(key='absence_rate',  value='0.15', label='Krank/Urlaub/Fluk (%)',    category=cat),
            BusinessParam(key='default_agents_weekday',  value='220', label='Default agents (weekday)', category=cat),
            BusinessParam(key='default_agents_saturday', value='55',  label='Default agents (Saturday)', category=cat),
            BusinessParam(key='hours_sunday',            value='5.5', label='Std./Tag (Sunday)',        category=cat),
            BusinessParam(key='default_agents_sunday',   value='0',   label='Default agents (Sunday)',  category=cat),
            BusinessParam(key='max_coverage_pct', value='105',
                          label='Max Coverage (% of client target)', category=cat),
            BusinessParam(key='aht_chat',            value='12',   label='AHT Chat (min)', category=cat),
            BusinessParam(key='target_service_level', value='0.80', label='Target Service Level (%)', category=cat),
            BusinessParam(key='target_asa',          value='20',   label='Target ASA (seconds)', category=cat),
            BusinessParam(key='max_occupancy',       value='0.85', label='Max Agent Occupancy (%)', category=cat),
            BusinessParam(key='min_rest_hours',      value='11',   label='Minimum Rest Between Shifts (hours)', category=cat),
        ]
        db.session.add_all(params)

    # Auto-create a Team row for any team name already referenced by an
    # employee but missing its own row (e.g. after an import) — not a
    # pre-seeding mechanism; new projects start with zero teams and an admin
    # adds them via the Teams page.
    existing_team_names = {t.name for t in Team.query.all()}
    sources = {e.team for e in Employee.query.all() if e.team}
    for name in sorted(sources):
        if name not in existing_team_names:
            db.session.add(Team(name=name))

    # One-time migration of EON's real 2026 public holidays, from back when
    # they were hardcoded in this file — every other project starts with zero
    # holidays and an admin adds their own via /public-holidays. Guarded by
    # count==0 so this never re-fires or duplicates on later boots, and never
    # overwrites holidays an admin has since edited/deleted.
    if project_key == 'eon' and PublicHoliday.query.count() == 0:
        db.session.add_all([
            PublicHoliday(date=date(2026, 1, 1), name='New Year'),
            PublicHoliday(date=date(2026, 1, 2), name='New Year'),
            PublicHoliday(date=date(2026, 1, 7), name='Orthodox Christmas'),
            PublicHoliday(date=date(2026, 2, 17), name='Independence Day'),
            PublicHoliday(date=date(2026, 4, 9), name='Constitution Day'),
            PublicHoliday(date=date(2026, 5, 1), name='Labour Day'),
            PublicHoliday(date=date(2026, 5, 9), name='Europe Day'),
            PublicHoliday(date=date(2026, 11, 28), name='Albania Flag Day'),
            PublicHoliday(date=date(2026, 11, 29), name='Liberation Day'),
            PublicHoliday(date=date(2026, 12, 25), name='Christmas'),
        ])

    db.session.commit()


def _migrate_schema(project_key):
    """Idempotently add columns/rows introduced after the DB was first created.
    db.create_all() only creates missing tables, not columns on existing ones,
    and there's no Alembic here — so this runs a one-off ALTER TABLE per new
    column, guarded by a PRAGMA table_info check, safe to run on every boot.
    Assumes g.active_engine is already set to this project's engine."""
    engine = g.active_engine
    with engine.connect() as conn:
        from sqlalchemy import text

        def existing_columns(table):
            rows = conn.execute(text(f'PRAGMA table_info({table})')).fetchall()
            return {r[1] for r in rows}

        emp_cols = existing_columns('employee')
        if 'works_saturday' not in emp_cols:
            conn.execute(text("ALTER TABLE employee ADD COLUMN works_saturday VARCHAR(10) DEFAULT 'no'"))
            conn.execute(text("UPDATE employee SET works_saturday = works_weekends"))
        if 'works_sunday' not in emp_cols:
            conn.execute(text("ALTER TABLE employee ADD COLUMN works_sunday VARCHAR(10) DEFAULT 'no'"))
        if 'schedule_group_id' not in emp_cols:
            conn.execute(text("ALTER TABLE employee ADD COLUMN schedule_group_id INTEGER"))
        if 'custom_hours' not in emp_cols:
            conn.execute(text("ALTER TABLE employee ADD COLUMN custom_hours FLOAT"))
        if 'rotation_pattern_id' not in emp_cols:
            conn.execute(text("ALTER TABLE employee ADD COLUMN rotation_pattern_id INTEGER"))
        if 'employee_number' not in emp_cols:
            conn.execute(text("ALTER TABLE employee ADD COLUMN employee_number VARCHAR(30)"))

        sg_cols = existing_columns('schedule_group')
        if 'rotation_pattern_id' not in sg_cols:
            conn.execute(text("ALTER TABLE schedule_group ADD COLUMN rotation_pattern_id INTEGER"))

        # Saturday/Sunday availability collapsed from yes/no/sometimes to yes/no —
        # 'sometimes' was already treated identically to 'yes' everywhere it was read.
        conn.execute(text("UPDATE employee SET works_saturday = 'yes' WHERE works_saturday = 'sometimes'"))
        conn.execute(text("UPDATE employee SET works_sunday = 'yes' WHERE works_sunday = 'sometimes'"))

        # Default shift template's span extended 08:00-16:00 -> 08:00-16:30 to
        # account for a 1h unpaid break (net hours 8.0 -> 7.5). Scoped to the
        # exact untouched seed values so a manually-edited default isn't clobbered.
        conn.execute(text(
            "UPDATE shift_template SET end_time = '16:30', hours = 7.5 "
            "WHERE is_default = 1 AND start_time = '08:00' AND end_time = '16:00' AND hours = 8.0"
        ))

        # Employees with no shift_template_id and no custom hours were already
        # working the default template via the fallback in Employee.effective_shift_template
        # — make that explicit on the record instead of leaving it an implicit NULL,
        # so "no shift assigned" no longer exists as an ambiguous state.
        conn.execute(text(
            "UPDATE employee SET shift_template_id = "
            "(SELECT id FROM shift_template WHERE is_default = 1 LIMIT 1) "
            "WHERE shift_template_id IS NULL AND (custom_start IS NULL OR custom_start = '') "
            "AND EXISTS (SELECT 1 FROM shift_template WHERE is_default = 1)"
        ))

        # 'NEWBIE' removed as a selectable team — every employee must belong to
        # a real team now. Only drop the row if nobody's actually on it.
        conn.execute(text(
            "DELETE FROM team WHERE name = 'NEWBIE' "
            "AND NOT EXISTS (SELECT 1 FROM employee WHERE employee.team = 'NEWBIE')"
        ))

        # Legacy per-project AI BusinessParam rows — AI config moved to the
        # global (cross-project) GlobalSetting table a while back, but these
        # were only ever read from (as a one-time seed source), never cleaned
        # up. Dead weight that clutters the Business Parameters page now that
        # it's a dedicated one.
        conn.execute(text("DELETE FROM business_param WHERE category = 'AI'"))

        dr_cols = existing_columns('day_restriction')
        if 'is_off' not in dr_cols:
            conn.execute(text("ALTER TABLE day_restriction ADD COLUMN is_off BOOLEAN DEFAULT 0"))

        doc_cols = existing_columns('document')
        if 'extracted_text' not in doc_cols:
            conn.execute(text("ALTER TABLE document ADD COLUMN extracted_text TEXT"))
        if 'extraction_error' not in doc_cols:
            conn.execute(text("ALTER TABLE document ADD COLUMN extraction_error VARCHAR(200)"))

        il_cols = existing_columns('import_log')
        if 'tier' not in il_cols:
            conn.execute(text("ALTER TABLE import_log ADD COLUMN tier VARCHAR(20)"))

        conn.commit()

    # New BusinessParam keys may need adding to a DB that was already seeded
    # before these keys existed. Only backfill into an already-populated table —
    # a brand-new (empty) database is handled comprehensively by _seed_defaults()
    # below, which already includes these same keys in its main seed list.
    if BusinessParam.query.count() > 0:
        cat = project_config(project_key)['param_category']
        extra_params = [
            ('hours_sunday',          '5.5', 'Std./Tag (Sunday)',       cat),
            ('default_agents_sunday', '0',   'Default agents (Sunday)', cat),
            ('max_coverage_pct',      '105', 'Max Coverage (% of client target)', cat),
            ('aht_chat',              '12',   'AHT Chat (min)', cat),
            ('target_service_level',  '0.80', 'Target Service Level (%)', cat),
            ('target_asa',            '20',   'Target ASA (seconds)', cat),
            ('max_occupancy',         '0.85', 'Max Agent Occupancy (%)', cat),
            ('min_rest_hours',        '11',   'Minimum Rest Between Shifts (hours)', cat),
        ]
        for key, value, label, category in extra_params:
            if not BusinessParam.query.filter_by(key=key).first():
                db.session.add(BusinessParam(key=key, value=value, label=label, category=category))
        db.session.commit()


def _migrate_identity_schema():
    """Idempotently add columns introduced after identity.db was first created
    (same pattern as _migrate_schema, applied to the fixed identity bind)."""
    with db.engines['identity'].connect() as conn:
        from sqlalchemy import text
        cols = {r[1] for r in conn.execute(text('PRAGMA table_info(identity_user)')).fetchall()}
        if 'global_role' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN global_role VARCHAR(20)"))
            # Backfill from the old boolean if it exists on this DB.
            if 'is_global_admin' in cols:
                conn.execute(text(
                    "UPDATE identity_user SET global_role = 'superadmin' WHERE is_global_admin = 1"
                ))
        if 'email' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN email VARCHAR(200)"))
        if 'ui_theme' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN ui_theme VARCHAR(20) DEFAULT 'light'"))
        if 'ui_locale' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN ui_locale VARCHAR(10) DEFAULT 'en'"))
        if 'first_name' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN first_name VARCHAR(80)"))
        if 'last_name' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN last_name VARCHAR(80)"))
        if 'position' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN position VARCHAR(100)"))
        if 'avatar_filename' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN avatar_filename VARCHAR(200)"))
        if 'work_context_type' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN work_context_type VARCHAR(20)"))
        if 'work_context_value' not in cols:
            conn.execute(text("ALTER TABLE identity_user ADD COLUMN work_context_value VARCHAR(100)"))
        # Partial unique index — NULLs (legacy accounts with no email yet) don't
        # collide with each other, but two accounts can never share a real email.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_user_email "
            "ON identity_user(email) WHERE email IS NOT NULL"
        ))

        # One-time backfill of EON's branding into ProjectMeta — this project
        # predates ProjectMeta and used to be hardcoded in a PROJECTS dict.
        eon_row = conn.execute(text("SELECT 1 FROM project_meta WHERE key = 'eon'")).fetchone()
        if not eon_row:
            conn.execute(text(
                "INSERT INTO project_meta (key, display_name, company, client, site, param_category, created_at) "
                "VALUES ('eon', 'E.ON Kosovo', 'KiKxxl-evroTarget', "
                "'E.ON (energy company, Germany/Switzerland)', 'Kosovo (Prishtina)', 'Kosovo', :now)"
            ), {'now': datetime.utcnow()})

        conn.commit()


# Legacy role names -> current 4-tier scale (viewer/editor/admin/superadmin).
# 'user' was renamed 'editor'; 'dev' folded into 'admin' (AI config is global-only
# now, so the per-project 'dev' tier has nothing left to gate); a per-project
# 'superadmin' grant is capped down to 'admin' (there's no per-project superadmin
# any more — that's what global_role is for).
_LEGACY_ROLE_MAP = {'user': 'editor', 'dev': 'admin', 'superadmin': 'admin'}


def _migrate_identities():
    """Remaps any already-migrated ProjectRole rows still holding a pre-rename
    role name ('user'/'dev'/legacy per-project 'superadmin') to the current
    4-tier scale. Safe and idempotent to run every boot — once remapped, the
    filter below simply matches nothing on subsequent runs."""
    for pr in ProjectRole.query.filter(ProjectRole.role.in_(_LEGACY_ROLE_MAP.keys())).all():
        pr.role = _LEGACY_ROLE_MAP[pr.role]
    db.session.commit()

    # One-time-only: import each project's legacy per-project app_user table
    # (raw sqlite3 — those rows are no longer read via the ORM) into the new
    # identity system. Gated on the identity DB being completely empty, NOT
    # on a per-username check — the latter silently resurrected a stale
    # duplicate account the first time a migrated identity was later renamed,
    # since the old username stopped matching anything already-migrated.
    # Once at least one real identity exists, this whole block never runs
    # again, regardless of what usernames appear in the legacy tables.
    if IdentityUser.query.count() > 0:
        return

    legacy_by_username = {}   # username -> {'password_hash', 'projects': {project: role}}
    for key in discover_projects():
        db_path = os.path.join(app.instance_path, key, 'workforce.db')
        if not os.path.exists(db_path):
            continue
        try:
            con = sqlite3.connect(db_path)
            rows = con.execute('SELECT username, password_hash, role FROM app_user').fetchall()
            con.close()
        except sqlite3.OperationalError:
            continue
        for username, password_hash, role in rows:
            entry = legacy_by_username.setdefault(username, {'password_hash': password_hash, 'projects': {}})
            entry['projects'][key] = _LEGACY_ROLE_MAP.get(role, role)

    for username, info in legacy_by_username.items():
        is_shared = len(info['projects']) > 1
        identity = IdentityUser(
            username=username,
            password_hash=info['password_hash'],
            is_active=True,
            global_role='superadmin' if is_shared else None,
        )
        db.session.add(identity)
        db.session.flush()
        if not is_shared:
            (project_key, role), = info['projects'].items()
            db.session.add(ProjectRole(user_id=identity.id, project=project_key, role=role))
    db.session.commit()


def _seed_global_settings():
    """Seed the AI provider/model GlobalSetting rows once. If a project still has
    legacy per-project ai_* BusinessParam rows from before AI config was made
    global, migrate one project's values in as the starting point instead of
    hardcoded defaults."""
    defaults = [
        ('ai_provider',        'AI Provider',      'mistral'),
        ('ai_mistral_model',   'Mistral Model',    'mistral-large-latest'),
        ('ai_ollama_url',      'Ollama Base URL',  'http://localhost:11434'),
        ('ai_ollama_model',    'Ollama Model',     'llama3.2'),
        ('ai_anthropic_model', 'Claude Model',     'claude-opus-4-8'),
        ('ai_openai_model',    'OpenAI Model',     'gpt-4o'),
        ('ai_gemini_model',    'Gemini Model',      'gemini-1.5-pro'),
    ]

    if GlobalSetting.query.count() > 0:
        # Already seeded in an earlier session — only backfill keys that didn't
        # exist yet (e.g. a newly added provider); never touch existing values.
        for key, label, default in defaults:
            if not GlobalSetting.query.filter_by(key=key).first():
                db.session.add(GlobalSetting(key=key, label=label, value=default))
        db.session.commit()
        return

    legacy_values = {}
    for key, engine in ENGINES.items():
        try:
            with engine.connect() as conn:
                from sqlalchemy import text
                rows = conn.execute(text(
                    "SELECT key, value FROM business_param WHERE category='AI'"
                )).fetchall()
                if rows:
                    legacy_values = dict(rows)
                    break
        except Exception:
            continue

    for key, label, default in defaults:
        db.session.add(GlobalSetting(key=key, label=label, value=legacy_values.get(key, default)))
    db.session.commit()


ENGINES = {}
for _key in discover_projects():
    _db_dir = os.path.join(app.instance_path, _key)
    os.makedirs(_db_dir, exist_ok=True)
    ENGINES[_key] = create_engine(f"sqlite:///{os.path.join(_db_dir, 'workforce.db')}")

with app.app_context():
    db.create_all(bind_key='identity')
    _migrate_identity_schema()
    for _key, _engine in ENGINES.items():
        db.metadatas[None].create_all(bind=_engine)
        g.active_engine = _engine
        _migrate_schema(_key)
        _seed_defaults(_key)
    g.active_engine = None
    _migrate_identities()
    _seed_global_settings()
    # One-time switch to Mistral as the default AI provider (EU data-residency
    # requirement). Guarded by a marker row so this never re-fires and clobbers
    # an admin's later, deliberate choice of a different provider.
    if not GlobalSetting.query.filter_by(key='_migrated_default_provider_to_mistral').first():
        _provider_setting = GlobalSetting.query.filter_by(key='ai_provider').first()
        if _provider_setting:
            _provider_setting.value = 'mistral'
        db.session.add(GlobalSetting(key='_migrated_default_provider_to_mistral',
                                     label='(internal marker)', value='1'))
        db.session.commit()
    # Bootstrap: if no identity exists at all (brand-new install), seed one
    # global superadmin account so there's always a way in.
    if IdentityUser.query.count() == 0:
        admin = IdentityUser(username='besart.grabanica', email='besart.grabanica@evrotarget.com',
                             is_active=True, global_role='superadmin')
        admin.set_password('besart')
        db.session.add(admin)
        db.session.commit()


# ────────────────────────────────────────────────────────────────────────────
# Auth
# ────────────────────────────────────────────────────────────────────────────

def _load_active_holidays():
    """Feed scheduler/algorithm.py's holiday set from the active project's own
    PublicHoliday rows (g.active_engine must already be bound)."""
    by_year = {}
    for h in PublicHoliday.query.all():
        by_year.setdefault(h.date.year, set()).add(h.date)
    set_holidays(by_year)


@app.before_request
def load_user():
    g.current_user = None
    g.active_project = None
    g.active_engine = None
    if request.endpoint in ('login', 'logout', 'static', 'no_access', 'switch_project',
                             'forgot_password', 'reset_password', 'accept_invite', 'set_locale') \
            or (request.endpoint and request.endpoint.startswith('api_')):
        if request.endpoint in ('switch_project', 'set_locale'):
            uid = flask_session.get('user_id')
            g.current_user = IdentityUser.query.get(uid) if uid else None
        return
    uid = flask_session.get('user_id')
    if not uid:
        return redirect(url_for('login', next=request.path))
    user = IdentityUser.query.get(uid)
    if not user or not user.is_active:
        flask_session.clear()
        return redirect(url_for('login'))
    g.current_user = user

    known = discover_projects()
    accessible = user.accessible_projects(known)
    if not accessible:
        return redirect(url_for('no_access'))

    active = flask_session.get('project')
    if active not in accessible:
        active = sorted(accessible)[0]
        flask_session['project'] = active

    g.active_project = active
    g.active_engine = ENGINES[active]
    _load_active_holidays()

    # Viewers are read-only — except for their own personal account (appearance,
    # name/photo, password), which isn't business data.
    if request.method == 'POST' and user.role_for(active) == 'viewer' \
            and request.endpoint not in ('set_ui_theme', 'account'):
        flash(_('Viewer accounts cannot make changes.'), 'warning')
        return redirect(request.referrer or url_for('dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if flask_session.get('user_id'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = IdentityUser.query.filter_by(username=username, is_active=True).first()
        if user and user.check_password(password):
            flask_session['user_id'] = user.id
            flask_session.pop('project', None)  # re-resolve default project on next request
            next_url = request.form.get('next') or url_for('dashboard')
            return redirect(next_url)
        flash(_('Invalid username or password.'), 'danger')
    next_url = request.args.get('next', '')
    return render_template('login.html', next_url=next_url)


@app.route('/logout', methods=['POST'])
def logout():
    flask_session.clear()
    return redirect(url_for('login'))


@app.route('/account/theme', methods=['POST'])
def set_ui_theme():
    theme = request.form.get('theme', '')
    if theme in IdentityUser.UI_THEMES:
        g.current_user.ui_theme = theme
        db.session.commit()
    next_url = request.form.get('next') or url_for('dashboard')
    return redirect(next_url)


@app.route('/account/locale', methods=['POST'])
def set_locale():
    locale = request.form.get('locale', '')
    if locale in IdentityUser.UI_LOCALES:
        flask_session['locale'] = locale
        if g.current_user:
            g.current_user.ui_locale = locale
            db.session.commit()
    next_url = request.form.get('next') or url_for('dashboard')
    return redirect(next_url)


_AVATAR_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}


def avatar_folder():
    """Project-independent — identity (and its avatar) isn't tied to any one project."""
    path = os.path.join(os.path.dirname(__file__), 'uploads', '_identity', 'avatars')
    os.makedirs(path, exist_ok=True)
    return path


@app.route('/account', methods=['GET', 'POST'])
def account():
    user = g.current_user
    if not user:
        return redirect(url_for('login'))

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'save_profile':
            user.first_name = request.form.get('first_name', '').strip()
            user.last_name = request.form.get('last_name', '').strip()
            user.position = request.form.get('position', '').strip()

            work_type = request.form.get('work_context_type', '')
            if work_type == 'project':
                value = request.form.get('work_project', '')
                if value in discover_projects():
                    user.work_context_type = 'project'
                    user.work_context_value = value
            elif work_type == 'department':
                value = request.form.get('work_department', '')
                if value in IdentityUser.DEPARTMENTS:
                    user.work_context_type = 'department'
                    user.work_context_value = value
            else:
                user.work_context_type = None
                user.work_context_value = None

            db.session.commit()
            flash(_('Profile updated.'), 'success')

        elif action == 'change_password':
            current = request.form.get('current_password', '')
            new = request.form.get('new_password', '')
            confirm = request.form.get('confirm_password', '')
            if not user.check_password(current):
                flash(_('Current password is incorrect.'), 'danger')
            elif len(new) < 6:
                flash(_('New password must be at least 6 characters.'), 'danger')
            elif new != confirm:
                flash(_('New passwords do not match.'), 'danger')
            else:
                user.set_password(new)
                db.session.commit()
                flash(_('Password changed.'), 'success')

        elif action == 'send_reset_email':
            if user.email:
                token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                db.session.add(PasswordReset(
                    user_id=user.id, token_hash=token_hash,
                    expires_at=datetime.utcnow() + timedelta(hours=1),
                    request_ip=request.remote_addr,
                ))
                db.session.commit()
                try:
                    mailer.send_password_reset_email(to=user.email, token=token, username=user.username)
                    flash(_('Password reset email sent to %(email)s.', email=user.email), 'success')
                except Exception as e:
                    flash(_('Could not send reset email: %(error)s', error=e), 'danger')
            else:
                flash(_('No email on file for this account.'), 'danger')

        elif action == 'upload_avatar':
            file = request.files.get('avatar')
            if not file or not file.filename:
                flash(_('Please choose an image file.'), 'danger')
            else:
                ext = os.path.splitext(file.filename)[1].lower()
                if ext not in _AVATAR_EXTENSIONS:
                    flash(_('Photo must be PNG, JPG, or WEBP.'), 'danger')
                else:
                    for old in glob.glob(os.path.join(avatar_folder(), f'{user.id}.*')):
                        os.remove(old)
                    stored = f'{user.id}{ext}'
                    file.save(os.path.join(avatar_folder(), stored))
                    user.avatar_filename = stored
                    db.session.commit()
                    flash(_('Photo updated.'), 'success')

        elif action == 'remove_avatar':
            for old in glob.glob(os.path.join(avatar_folder(), f'{user.id}.*')):
                os.remove(old)
            user.avatar_filename = None
            db.session.commit()
            flash(_('Photo removed.'), 'info')

        return redirect(url_for('account'))

    known = discover_projects()
    if user.global_role:
        access = [{'project': project_config(p)['display_name'], 'role': user.global_role}
                  for p in sorted(known)]
    else:
        access = [{'project': project_config(pr.project)['display_name'], 'role': pr.role}
                  for pr in user.project_roles if pr.project in known]

    all_projects = [{'key': p, 'display_name': project_config(p)['display_name']} for p in sorted(known)]
    return render_template('account.html', user=user, access=access,
                           all_projects=all_projects, departments=IdentityUser.DEPARTMENTS)


@app.route('/account/avatar/<int:user_id>')
def account_avatar(user_id):
    user = IdentityUser.query.get_or_404(user_id)
    if not user.avatar_filename:
        return '', 404
    path = os.path.join(avatar_folder(), user.avatar_filename)
    if not os.path.exists(path):
        return '', 404
    return send_file(path)


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        if email:
            user = IdentityUser.query.filter(
                db.func.lower(IdentityUser.email) == email.lower(),
                IdentityUser.is_active == True,  # noqa: E712
            ).first()
            if user:
                token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                db.session.add(PasswordReset(
                    user_id=user.id, token_hash=token_hash,
                    expires_at=datetime.utcnow() + timedelta(hours=1),
                    request_ip=request.remote_addr,
                ))
                db.session.commit()
                try:
                    mailer.send_password_reset_email(to=user.email, token=token, username=user.username)
                except Exception as e:
                    print(f'[mailer] password reset email failed: {e}')
        # Always show the same message, whether or not that email matched —
        # don't let this endpoint be used to discover which emails have accounts.
        flash(_('If an account exists for that email, a reset link is on its way. Check your inbox in the next few minutes.'), 'info')
        return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')


@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    token = request.values.get('token', '')
    token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ''
    pr = PasswordReset.query.filter_by(token_hash=token_hash).first() if token_hash else None

    error = None
    if not pr:
        error = _('Invalid or unknown reset link.')
    elif pr.used_at:
        error = _('This reset link has already been used.')
    elif pr.expires_at < datetime.utcnow():
        error = _('This reset link has expired. Request a new one.')

    if request.method == 'POST':
        if error:
            flash(error, 'danger')
            return redirect(url_for('forgot_password'))
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if len(password) < 6:
            flash(_('Password must be at least 6 characters.'), 'danger')
            return render_template('reset_password.html', token=token, username=pr.user.username)
        if password != confirm:
            flash(_('Passwords do not match.'), 'danger')
            return render_template('reset_password.html', token=token, username=pr.user.username)
        pr.user.set_password(password)
        pr.used_at = datetime.utcnow()
        db.session.commit()
        flash(_('Password updated. You can now sign in with your new password.'), 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token, error=error,
                           username=pr.user.username if pr and not error else None)


@app.route('/no-access')
def no_access():
    return render_template('no_access.html')


@app.route('/switch-project', methods=['POST'])
def switch_project():
    user = g.current_user
    if not user:
        return redirect(url_for('login'))
    target = request.form.get('project', '')
    accessible = user.accessible_projects(discover_projects())
    if target in accessible:
        flask_session['project'] = target
    else:
        flash(_('You do not have access to that project.'), 'danger')
    return redirect(request.referrer or url_for('dashboard'))


# ────────────────────────────────────────────────────────────────────────────
# User management
#
# Two distinct scopes, deliberately gated differently:
#   - Per-project access (ProjectRole rows, ceiling 'admin') — managed by that
#     project's own 'admin'-or-higher users. Contained to the project they
#     actually control.
#   - Global-scope roles (IdentityUser.global_role, incl. technical/system
#     config) — exclusively managed by a true global superadmin
#     (require_technical_admin), since granting global reach hands out access
#     to every project, including ones the granter might not control.
# ────────────────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_PROJECT_KEY_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


# ────────────────────────────────────────────────────────────────────────────
# Projects (global admin / superadmin only) — create new client engagements
# without touching code. Each gets its own fully isolated database,
# provisioned immediately (no restart needed).
# ────────────────────────────────────────────────────────────────────────────

@app.route('/projects', methods=['GET', 'POST'])
@require_global_admin
def projects_admin():
    if request.method == 'POST':
        key = request.form.get('key', '').strip().lower()
        display_name = request.form.get('display_name', '').strip()
        company = request.form.get('company', '').strip() or 'KiKxxl-evroTarget'
        client = request.form.get('client', '').strip()
        site = request.form.get('site', '').strip()
        param_category = request.form.get('param_category', '').strip() or 'Operations'

        if not key or not _PROJECT_KEY_RE.match(key):
            flash(_('Project key must start with a letter/number and contain only lowercase letters, numbers, - or _.'), 'danger')
        elif key in discover_projects():
            flash(_('A project with that key already exists.'), 'danger')
        elif not display_name:
            flash(_('Display name is required.'), 'danger')
        else:
            db.session.add(ProjectMeta(
                key=key, display_name=display_name, company=company,
                client=client, site=site, param_category=param_category,
            ))
            db.session.commit()

            # Provision its database right now — usable immediately, no restart.
            db_dir = os.path.join(app.instance_path, key)
            os.makedirs(db_dir, exist_ok=True)
            engine = create_engine(f"sqlite:///{os.path.join(db_dir, 'workforce.db')}")
            ENGINES[key] = engine
            db.metadatas[None].create_all(bind=engine)
            prev_engine = g.active_engine
            g.active_engine = engine
            try:
                _migrate_schema(key)
                _seed_defaults(key)
            finally:
                g.active_engine = prev_engine

            flash(_('Project "%(name)s" created and ready to use.', name=display_name), 'success')
        return redirect(url_for('projects_admin'))

    projects = [{'key': key, **project_config(key)} for key in discover_projects()]
    return render_template('projects_admin.html', projects=projects)


# ────────────────────────────────────────────────────────────────────────────
# Internal read-only API (Cerebro AI Hub integration) — API-key authenticated,
# never session/cookie based (see require_api_key/load_user above). Read-only
# by construction: only GET routes, no route here ever calls db.session.add/
# delete/commit. Kept as plain routes in this file rather than a Blueprint to
# minimize footprint, consistent with the rest of this file's style. Every
# view function name here must start with api_ (see load_user()'s exemption).
# ────────────────────────────────────────────────────────────────────────────

def _employee_api_dict(e):
    return {
        'id': e.id, 'name': e.name, 'team': e.team,
        'fte_percent': e.fte_percent, 'fte_mode': e.fte_mode,
        'shift_label': e.shift_label,
        'works_saturday': e.works_saturday, 'works_sunday': e.works_sunday,
        'works_holidays': e.works_holidays,
        'status': e.status,
    }


@app.route('/api/v1/projects')
@require_api_key
def api_projects():
    result = []
    for key in discover_projects():
        g.active_engine = ENGINES[key]
        cfg = project_config(key)
        result.append({
            'key': key, **cfg,
            'employee_count': Employee.query.filter_by(status='active').count(),
        })
    g.active_engine = None
    return jsonify({'projects': result})


@app.route('/api/v1/<project_key>/employees')
@require_api_key
def api_employees(project_key):
    err = _bind_project_or_404(project_key)
    if err:
        return err
    status = request.args.get('status', 'active')
    q = Employee.query if status == 'all' else Employee.query.filter_by(status=status)
    team = request.args.get('team')
    if team:
        q = q.filter_by(team=team)
    employees = [_employee_api_dict(e) for e in q.order_by(Employee.name).all()]
    return jsonify({'project': project_key, 'employees': employees})


@app.route('/api/v1/<project_key>/teams')
@require_api_key
def api_teams(project_key):
    err = _bind_project_or_404(project_key)
    if err:
        return err
    teams = [{'id': t.id, 'name': t.name, 'member_count': t.member_count}
             for t in Team.query.order_by(Team.name).all()]
    return jsonify({'project': project_key, 'teams': teams})


@app.route('/api/v1/<project_key>/schedules')
@require_api_key
def api_schedules(project_key):
    err = _bind_project_or_404(project_key)
    if err:
        return err
    q = Schedule.query
    if request.args.get('year'):
        q = q.filter_by(year=int(request.args['year']))
    if request.args.get('month'):
        q = q.filter_by(month=int(request.args['month']))
    schedules = [{
        'id': s.id, 'name': s.name, 'year': s.year, 'month': s.month,
        'period_id': s.period_id, 'notes': s.notes,
        'generated_at': s.generated_at.isoformat() if s.generated_at else None,
        'assignment_count': len(s.assignments),
    } for s in q.order_by(Schedule.generated_at.desc()).all()]
    return jsonify({'project': project_key, 'schedules': schedules})


@app.route('/api/v1/<project_key>/schedules/<int:schedule_id>/assignments')
@require_api_key
def api_assignments(project_key, schedule_id):
    err = _bind_project_or_404(project_key)
    if err:
        return err
    schedule = Schedule.query.get(schedule_id)
    if not schedule:
        return jsonify({'error': 'unknown schedule'}), 404
    q = ShiftAssignment.query.filter_by(schedule_id=schedule_id)
    if request.args.get('date_from'):
        q = q.filter(ShiftAssignment.date >= date.fromisoformat(request.args['date_from']))
    if request.args.get('date_to'):
        q = q.filter(ShiftAssignment.date <= date.fromisoformat(request.args['date_to']))
    if request.args.get('status'):
        q = q.filter_by(status=request.args['status'])
    assignments = [{
        'employee_id': a.employee_id, 'employee_name': a.employee.name if a.employee else None,
        'team': a.employee.team if a.employee else None,
        'date': a.date.isoformat(), 'status': a.status,
        'shift_start': a.shift_start, 'shift_end': a.shift_end,
        'hours_worked': a.hours_worked, 'display_code': a.display_code,
    } for a in q.order_by(ShiftAssignment.date).all()]
    return jsonify({'project': project_key, 'schedule_id': schedule_id, 'assignments': assignments})


@app.route('/users')
@require_role('admin')
def users_list():
    # Everyone with access to the CURRENTLY ACTIVE project — global-role
    # holders, plus anyone with an explicit ProjectRole for it.
    active = g.active_project
    users = [
        u for u in IdentityUser.query.order_by(IdentityUser.username).all()
        if u.global_role or u.role_for(active) is not None
    ]
    other_projects = [p for p in discover_projects() if p != active]
    all_identities = IdentityUser.query.order_by(IdentityUser.username).all() if g.current_user.is_technical_admin else []
    project_invites = (Invitation.query.filter_by(project=active)
                       .order_by(Invitation.created_at.desc()).all())
    global_invites = (Invitation.query.filter_by(project=None).order_by(Invitation.created_at.desc()).all()
                      if g.current_user.is_technical_admin else [])
    return render_template('users.html', users=users, roles=IdentityUser.ROLES,
                           other_projects=other_projects, all_identities=all_identities,
                           project_invites=project_invites, global_invites=global_invites,
                           mail_dry_run=mailer.DRY_RUN)


def _create_invite(email, project, role, invited_by):
    """Shared by user_invite and invitation_resend. Returns (invitation, error)."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    inv = Invitation(email=email, project=project, role=role, token_hash=token_hash,
                     invited_by=invited_by.id, expires_at=datetime.utcnow() + timedelta(days=30))
    db.session.add(inv)
    db.session.commit()
    project_label = project_config(project)['display_name'] if project else None
    try:
        mailer.send_invite_email(to=email, role=role, token=token,
                                 invited_by_name=invited_by.username, project_label=project_label)
    except Exception as e:
        print(f'[mailer] invite email failed: {e}')
    return inv


@app.route('/users/invite', methods=['POST'])
@require_role('admin')
def user_invite():
    email = request.form.get('email', '').strip()
    role = request.form.get('role', 'editor')
    scope = request.form.get('scope', 'project')
    active = g.active_project

    if not email or not _EMAIL_RE.match(email):
        flash(_('A valid email address is required.'), 'danger')
        return redirect(url_for('users_list'))

    if scope == 'global':
        if not g.current_user.is_technical_admin:
            flash(_('Only a global superadmin can send a global-scope invite.'), 'danger')
            return redirect(url_for('users_list'))
        if role not in IdentityUser.ROLES:
            flash(_('Unknown role.'), 'danger')
            return redirect(url_for('users_list'))
        target_project = None
    else:
        if role == 'superadmin':
            flash(_('"superadmin" is a global-only role — invite as admin instead, or use a global invite.'), 'danger')
            return redirect(url_for('users_list'))
        if role not in IdentityUser.ROLES:
            flash(_('Unknown role.'), 'danger')
            return redirect(url_for('users_list'))
        target_project = active

    existing = IdentityUser.query.filter(db.func.lower(IdentityUser.email) == email.lower()).first()
    if existing:
        # Already a verified identity — grant directly, no need to re-invite.
        if scope == 'global':
            existing.global_role = role
            db.session.commit()
            flash(_('"%(username)s" already has an account — set their global role to %(role)s.', username=existing.username, role=role), 'success')
        elif existing.role_for(target_project) is not None:
            flash(_('"%(username)s" already has access to this project.', username=existing.username), 'warning')
        else:
            db.session.add(ProjectRole(user_id=existing.id, project=target_project, role=role))
            db.session.commit()
            flash(_('"%(username)s" already has an account — granted access to this project as %(role)s.', username=existing.username, role=role), 'success')
        return redirect(url_for('users_list'))

    _create_invite(email, target_project, role, g.current_user)
    note = _(' (dry-run — check the server log for the link)') if mailer.DRY_RUN else ''
    flash(_('Invite sent to %(email)s%(note)s.', email=email, note=note), 'success')
    return redirect(url_for('users_list'))


@app.route('/invitations/<int:invite_id>/resend', methods=['POST'])
@require_role('admin')
def invitation_resend(invite_id):
    inv = Invitation.query.get_or_404(invite_id)
    if inv.project != g.active_project and not (inv.project is None and g.current_user.is_technical_admin):
        flash(_('You do not have permission to manage that invite.'), 'danger')
        return redirect(url_for('users_list'))
    if inv.status not in ('pending', 'expired'):
        flash(_('Only pending or expired invites can be resent.'), 'warning')
        return redirect(url_for('users_list'))
    token = secrets.token_urlsafe(32)
    inv.token_hash = hashlib.sha256(token.encode()).hexdigest()
    inv.expires_at = datetime.utcnow() + timedelta(days=30)
    db.session.commit()
    project_label = project_config(inv.project)['display_name'] if inv.project else None
    try:
        mailer.send_invite_email(to=inv.email, role=inv.role, token=token,
                                 invited_by_name=g.current_user.username, project_label=project_label)
    except Exception as e:
        print(f'[mailer] invite resend failed: {e}')
    flash(_('Invite resent to %(email)s.', email=inv.email), 'success')
    return redirect(url_for('users_list'))


@app.route('/invitations/<int:invite_id>/revoke', methods=['POST'])
@require_role('admin')
def invitation_revoke(invite_id):
    inv = Invitation.query.get_or_404(invite_id)
    if inv.project != g.active_project and not (inv.project is None and g.current_user.is_technical_admin):
        flash(_('You do not have permission to manage that invite.'), 'danger')
        return redirect(url_for('users_list'))
    if inv.accepted_at:
        flash(_('Cannot revoke an invite that was already accepted.'), 'danger')
        return redirect(url_for('users_list'))
    inv.revoked_at = datetime.utcnow()
    db.session.commit()
    flash(_('Invite to %(email)s revoked.', email=inv.email), 'info')
    return redirect(url_for('users_list'))


@app.route('/accept-invite', methods=['GET', 'POST'])
def accept_invite():
    token = request.values.get('token', '')
    token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ''
    inv = Invitation.query.filter_by(token_hash=token_hash).first() if token_hash else None

    error = None
    if not inv:
        error = _('Invalid or unknown invite link.')
    elif inv.status == 'accepted':
        error = _('This invite has already been used.')
    elif inv.status == 'revoked':
        error = _('This invite has been revoked.')
    elif inv.status == 'expired':
        error = _('This invite has expired. Ask an admin to resend it.')

    if request.method == 'POST':
        if error:
            flash(error, 'danger')
            return redirect(url_for('login'))
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        project_label = project_config(inv.project)['display_name'] if inv.project else 'every project'

        if len(username) < 2:
            flash(_('Username must be at least 2 characters.'), 'danger')
            return render_template('accept_invite.html', token=token, invite=inv, project_label=project_label)
        if IdentityUser.query.filter(db.func.lower(IdentityUser.username) == username.lower()).first():
            flash(_('That username is already taken.'), 'danger')
            return render_template('accept_invite.html', token=token, invite=inv, project_label=project_label)
        if IdentityUser.query.filter(db.func.lower(IdentityUser.email) == inv.email.lower()).first():
            flash(_('That email is already in use by another account.'), 'danger')
            return render_template('accept_invite.html', token=token, invite=inv, project_label=project_label)
        if len(password) < 6:
            flash(_('Password must be at least 6 characters.'), 'danger')
            return render_template('accept_invite.html', token=token, invite=inv, project_label=project_label)
        if password != confirm:
            flash(_('Passwords do not match.'), 'danger')
            return render_template('accept_invite.html', token=token, invite=inv, project_label=project_label)

        user = IdentityUser(username=username, email=inv.email, is_active=True,
                            global_role=inv.role if inv.project is None else None)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        if inv.project:
            db.session.add(ProjectRole(user_id=user.id, project=inv.project, role=inv.role))
        inv.accepted_at = datetime.utcnow()
        inv.accepted_by = user.id
        db.session.commit()

        flask_session['user_id'] = user.id
        flask_session.pop('project', None)
        flash(_('Account activated — welcome!'), 'success')
        return redirect(url_for('dashboard'))

    project_label = project_config(inv.project)['display_name'] if inv and inv.project else 'every project'
    return render_template('accept_invite.html', token=token, invite=inv, error=error, project_label=project_label)


@app.route('/users/<int:user_id>/edit', methods=['POST'])
@require_role('admin')
def user_edit(user_id):
    """Sets/updates a per-project role OVERRIDE for the active project. This is
    independent of any global_role the user might hold — effective role is
    always the higher of the two (IdentityUser.role_for), so a global-viewer
    can still be given e.g. an 'editor' override on one specific project."""
    user = IdentityUser.query.get_or_404(user_id)
    active = g.active_project
    role = request.form.get('role', '')
    if role:
        pr = ProjectRole.query.filter_by(user_id=user.id, project=active).first()
        if pr:
            pr.role = role
        else:
            db.session.add(ProjectRole(user_id=user.id, project=active, role=role))
    user.email = request.form.get('email', '').strip() or None
    user.is_active = request.form.get('is_active') == '1'
    password = request.form.get('password', '').strip()
    if password:
        user.set_password(password)
    db.session.commit()
    flash(_('User "%(username)s" updated.', username=user.username), 'success')
    return redirect(url_for('users_list'))


@app.route('/users/<int:user_id>/delete', methods=['POST'])
@require_role('admin')
def user_delete(user_id):
    """Removes any per-project role OVERRIDE for the active project. If the
    user has a global_role, that still applies afterward — this can't revoke
    global access (use Global Roles for that)."""
    user = IdentityUser.query.get_or_404(user_id)
    active = g.active_project
    if user.id == flask_session.get('user_id'):
        flash(_('Cannot remove your own access to this project.'), 'danger')
        return redirect(url_for('users_list'))

    pr = ProjectRole.query.filter_by(user_id=user.id, project=active).first()
    if not pr:
        flash(_('"%(username)s" has no project-specific override here — '
                'their global role (if any) is what grants their access.', username=user.username), 'warning')
        return redirect(url_for('users_list'))

    db.session.delete(pr)
    db.session.commit()
    if user.global_role:
        flash(_('Removed "%(username)s"\'s project-specific override — '
                'their global %(role)s role still applies here.', username=user.username, role=user.global_role), 'info')
    else:
        flash(_('Removed "%(username)s"\'s access to this project.', username=user.username), 'info')
    return redirect(url_for('users_list'))


@app.route('/users/<int:user_id>/grant', methods=['POST'])
@require_role('admin')
def user_grant_project(user_id):
    user = IdentityUser.query.get_or_404(user_id)
    target = request.form.get('project', '')
    role = request.form.get('role', 'editor')
    if target not in discover_projects():
        flash(_('Unknown project.'), 'danger')
    elif user.role_for(target) is not None:
        flash(_('"%(username)s" already has access to %(project)s.', username=user.username, project=project_config(target)["display_name"]), 'warning')
    else:
        db.session.add(ProjectRole(user_id=user.id, project=target, role=role))
        db.session.commit()
        flash(_('Granted "%(username)s" access to %(project)s as %(role)s.', username=user.username, project=project_config(target)["display_name"], role=role), 'success')
    return redirect(url_for('users_list'))


@app.route('/users/<int:user_id>/global-role', methods=['POST'])
@require_technical_admin
def user_set_global_role(user_id):
    user = IdentityUser.query.get_or_404(user_id)
    new_role = request.form.get('global_role', '').strip()
    if user.id == flask_session.get('user_id') and new_role != 'superadmin':
        flash(_('Cannot remove your own global superadmin access.'), 'danger')
    elif new_role and new_role not in IdentityUser.ROLES:
        flash(_('Unknown role.'), 'danger')
    else:
        user.global_role = new_role or None
        db.session.commit()
        role_display = _(new_role.capitalize()) if new_role else _('none')
        flash(_('"%(username)s"\'s global role is now %(role)s.', username=user.username, role=role_display), 'success')
    return redirect(url_for('users_list'))


# ────────────────────────────────────────────────────────────────────────────
# Context processors
# ────────────────────────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    active = getattr(g, 'active_project', None)
    user = getattr(g, 'current_user', None)
    base = {
        'today': date.today(),
        'current_year': date.today().year,
        'current_month': date.today().month,
        'month_names': MONTH_NAMES,
        'current_user': user,
        'current_role': user.role_for(active) if user and active else None,
        'active_project': active,
        'PROJECT': project_config(active) if active else project_config(''),
    }
    if not active:
        return base
    accessible = user.accessible_projects(discover_projects()) if user else []
    base.update({
        'active_employees': Employee.query.filter_by(status='active').count(),
        'all_teams': Team.query.order_by(Team.name).all(),
        'accessible_projects': [(p, project_config(p)['display_name']) for p in sorted(accessible)],
        'project_colors': {p: project_color(p) for p in accessible},
        'project_logo_urls': {
            p: url_for('project_logo', project=p) for p in accessible if project_logo_path(p)
        },
    })
    return base


# ────────────────────────────────────────────────────────────────────────────
# Dashboard
# ────────────────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    recent_schedules = Schedule.query.order_by(Schedule.generated_at.desc()).limit(5).all()
    recent_periods = ForecastPeriod.query.order_by(ForecastPeriod.created_at.desc()).limit(3).all()
    all_periods = ForecastPeriod.query.order_by(ForecastPeriod.start_date.desc()).all()
    all_schedules = Schedule.query.order_by(Schedule.generated_at.desc()).all()
    employee_count = Employee.query.filter_by(status='active').count()

    # Team breakdown with employee lists for expandable view
    teams_data = []
    for team_obj in Team.query.order_by(Team.name).all():
        members = (Employee.query
                   .filter_by(team=team_obj.name, status='active')
                   .order_by(Employee.name)
                   .all())
        if members:
            teams_data.append({
                'id': team_obj.id,
                'name': team_obj.name,
                'count': len(members),
                'members': members,
            })

    return render_template('dashboard.html',
                           recent_schedules=recent_schedules,
                           recent_periods=recent_periods,
                           all_periods=all_periods,
                           all_schedules=all_schedules,
                           employee_count=employee_count,
                           teams_data=teams_data)


_TREND_VARIABLES = {
    'required_agents': {'label': 'Required KS Agents', 'color': '#e34948', 'dark_color': '#e66767'},
    'total_contacts':  {'label': 'Total Contacts',      'color': '#2a78d6', 'dark_color': '#3987e5'},
    'total_sync':      {'label': 'Synchron Contacts',   'color': '#1baf7a', 'dark_color': '#199e70'},
    'total_async':     {'label': 'Asynchron Contacts',  'color': '#eda100', 'dark_color': '#c98500'},
    'total_chat':      {'label': 'Chat Contacts',       'color': '#4a3aa7', 'dark_color': '#9085e9'},
    'germany_contribution': {'label': 'Germany Contribution', 'color': '#008300', 'dark_color': '#008300'},
}


def _dashboard_trend_translations():
    """Never called — registers _TREND_VARIABLES labels for pybabel extraction,
    same reasoning as _doc_category_translations() above."""
    return [_('Required KS Agents'), _('Total Contacts'), _('Synchron Contacts'),
            _('Asynchron Contacts'), _('Chat Contacts'), _('Germany Contribution')]


@app.route('/dashboard/forecast-trend')
def dashboard_forecast_trend():
    period_id = request.args.get('period_id', type=int)
    variable = request.args.get('variable', 'required_agents')
    if variable not in _TREND_VARIABLES:
        variable = 'required_agents'

    if period_id:
        period = ForecastPeriod.query.get(period_id)
    else:
        period = ForecastPeriod.query.order_by(ForecastPeriod.start_date.desc()).first()

    if not period:
        return jsonify({'labels': [], 'values': [], 'label': _TREND_VARIABLES[variable]['label']})

    forecasts = period.daily_forecasts  # already ordered by date
    labels = [f.date.strftime('%d.%m') for f in forecasts]
    if variable == 'total_contacts':
        values = [round(f.total_contacts, 1) for f in forecasts]
    elif variable == 'required_agents':
        values = [f.required_ks_agents for f in forecasts]
    else:
        values = [round(getattr(f, variable), 1) for f in forecasts]

    meta = _TREND_VARIABLES[variable]
    return jsonify({
        'labels': labels,
        'values': values,
        'label': _(meta['label']),
        'color': meta['color'],
        'dark_color': meta['dark_color'],
    })


@app.route('/dashboard/coverage-trend')
def dashboard_coverage_trend():
    """Required vs. scheduled headcount per day, for a given (already
    generated) Schedule — recomputed on demand from ShiftAssignment +
    DailyForecast/BusinessParam rather than persisted, using the exact same
    required-agents fallback scheduler/algorithm.py's generate_schedule()
    itself uses (BusinessParam defaults, not output.py's older/stale
    hardcoded 220/55/0 export-time fallback)."""
    from scheduler.algorithm import get_holidays

    schedule_id = request.args.get('schedule_id', type=int)
    if schedule_id:
        sched = Schedule.query.get(schedule_id)
    else:
        sched = Schedule.query.order_by(Schedule.generated_at.desc()).first()

    if not sched:
        return jsonify({'labels': [], 'required': [], 'scheduled': []})

    _, days_in_month = calendar.monthrange(sched.year, sched.month)
    all_days = [date(sched.year, sched.month, d) for d in range(1, days_in_month + 1)]
    holidays = get_holidays(sched.year)

    params = {p.key: p.value for p in BusinessParam.query.all()}
    default_agents_wd = int(float(params.get('default_agents_weekday', 220)))
    default_agents_sat = int(float(params.get('default_agents_saturday', 55)))
    default_agents_sun = int(float(params.get('default_agents_sunday', 0)))

    forecasts = {
        f.date: f for f in DailyForecast.query.filter(
            DailyForecast.date >= all_days[0], DailyForecast.date <= all_days[-1],
        ).all()
    }
    scheduled_counts = {}
    for a in ShiftAssignment.query.filter_by(schedule_id=sched.id, status='work').all():
        scheduled_counts[a.date] = scheduled_counts.get(a.date, 0) + 1

    labels, required, scheduled = [], [], []
    for day in all_days:
        dow = day.weekday()
        is_saturday, is_sunday = dow == 5, dow == 6
        is_workday = not is_saturday and not is_sunday and day not in holidays

        fc = forecasts.get(day)
        if fc and fc.required_ks_agents:
            req = fc.required_ks_agents
        elif is_workday:
            req = default_agents_wd
        elif is_saturday:
            req = default_agents_sat
        elif is_sunday:
            req = default_agents_sun
        else:
            req = 0

        labels.append(day.strftime('%d.%m'))
        required.append(req)
        scheduled.append(scheduled_counts.get(day, 0))

    return jsonify({'labels': labels, 'required': required, 'scheduled': scheduled})


@app.route('/dashboard/intraday-heatmap')
def dashboard_intraday_heatmap():
    """Half-hourly demand (from HalfHourlyForecast) vs. scheduled headcount
    (derived by expanding each work assignment's shift span across that
    day's half-hour slots) for a single day."""
    from scheduler.coverage import intraday_coverage

    day_str = request.args.get('date')
    if day_str:
        try:
            day = datetime.strptime(day_str, '%Y-%m-%d').date()
        except ValueError:
            day = None
    else:
        day = None

    if day is None:
        latest = (DailyForecast.query
                  .filter(DailyForecast.half_hourly.any())
                  .order_by(DailyForecast.date.desc()).first())
        day = latest.date if latest else None

    if day is None:
        return jsonify({'date': None, 'slots': [], 'demand': [], 'scheduled': []})

    slots, demand, scheduled = intraday_coverage(day)
    return jsonify({'date': day.isoformat(), 'slots': slots, 'demand': demand, 'scheduled': scheduled})


@app.route('/dashboard/import-health')
def dashboard_import_health():
    """Daily count of ImportLog warning/error entries over the last 30 days —
    surfaces at a glance whether recent imports have been clean or noisy."""
    since = date.today() - timedelta(days=30)
    rows = (ImportLog.query
            .filter(ImportLog.created_at >= datetime.combine(since, datetime.min.time()))
            .all())

    by_day = {}
    for r in rows:
        d = r.created_at.date()
        bucket = by_day.setdefault(d, {'warning': 0, 'error': 0})
        bucket[r.level] = bucket.get(r.level, 0) + 1

    days = [since + timedelta(days=i) for i in range((date.today() - since).days + 1)]
    labels = [d.strftime('%d.%m') for d in days]
    warnings = [by_day.get(d, {}).get('warning', 0) for d in days]
    errors = [by_day.get(d, {}).get('error', 0) for d in days]

    return jsonify({'labels': labels, 'warnings': warnings, 'errors': errors})


@app.route('/dashboard/fairness/<int:schedule_id>')
def dashboard_fairness(schedule_id):
    """Assigned vs. target total days for every employee on this schedule —
    only populated for schedules generated after EmployeeScheduleSummary was
    introduced (generate_schedule() writes these rows itself; the target
    math depends on the scheduler's own FTE/eligibility logic and isn't
    safely re-derivable after the fact for older schedules)."""
    rows = (EmployeeScheduleSummary.query
            .filter_by(schedule_id=schedule_id)
            .join(Employee).order_by(Employee.name).all())

    names = [r.employee.name for r in rows]
    target = [r.target_wd + r.target_sat + r.target_sun for r in rows]
    actual = [r.assigned_wd + r.assigned_sat + r.assigned_sun for r in rows]

    return jsonify({'names': names, 'target': target, 'actual': actual})


# ────────────────────────────────────────────────────────────────────────────
# Import format-change detection
# ────────────────────────────────────────────────────────────────────────────

def _log_import_warnings(source, filename, warnings):
    """Persist each parser-reported structural warning to ImportLog (so the AI
    assistant and future admins can see it after the fact) and return a single
    flash-ready string, or None if there was nothing to report."""
    if not warnings:
        return None
    for w in warnings:
        db.session.add(ImportLog(source=source, filename=filename, level='warning', message=w))
    db.session.commit()
    return ' '.join(warnings)


def _log_import_result(source, filename, warnings, tier):
    """Like _log_import_warnings, but for the two file types the 3-tier
    import resolution covers (tfc_forecast, abnahme_de — see
    scheduler/import_mapping.py): always records which tier resolved the
    import, even when there's nothing wrong to warn about, per
    docs/specs/2026-08-import-mapping-detection.md acceptance criterion 6.
    Returns a flash-ready warning string, or None if there was nothing to
    flag to the user."""
    if warnings:
        for w in warnings:
            db.session.add(ImportLog(source=source, filename=filename, level='warning', message=w, tier=tier))
    else:
        db.session.add(ImportLog(
            source=source, filename=filename, level='info',
            message=f"Resolved via {tier} mapping.", tier=tier,
        ))
    db.session.commit()
    return ' '.join(warnings) if warnings else None


# ────────────────────────────────────────────────────────────────────────────
# Forecast
# ────────────────────────────────────────────────────────────────────────────

@app.route('/forecast')
def forecast_list():
    periods = ForecastPeriod.query.order_by(ForecastPeriod.created_at.desc()).all()
    return render_template('forecast_list.html', periods=periods)


@app.route('/forecast/new', methods=['GET', 'POST'])
@require_role('admin')
def forecast_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        tfc_file = request.files.get('tfc_file')
        abnahme_file = request.files.get('abnahme_file')
        fc_calc_file = request.files.get('fc_calc_file')

        if not name:
            flash(_('Period name is required.'), 'danger')
            return redirect(url_for('forecast_new'))

        from scheduler.parsers import get_parser_module
        parser_mod = get_parser_module(g.active_project)
        if parser_mod is None or not hasattr(parser_mod, 'parse_tfc_file'):
            flash(_("Forecast import isn't set up for this project yet — its file format "
                     "needs a parser written for it first."), 'warning')
            return redirect(url_for('forecast_new'))

        # Save uploaded files
        tfc_path = abnahme_path = fc_path = None
        if tfc_file and tfc_file.filename:
            tfc_path = os.path.join(upload_folder(), f'tfc_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx')
            tfc_file.save(tfc_path)
        if abnahme_file and abnahme_file.filename:
            abnahme_path = os.path.join(upload_folder(), f'abnahme_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx')
            abnahme_file.save(abnahme_path)
        if fc_calc_file and fc_calc_file.filename:
            fc_path = os.path.join(upload_folder(), f'fccalc_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx')
            fc_calc_file.save(fc_path)

        if not tfc_path:
            flash(_('Client Forecast file is required.'), 'danger')
            return redirect(url_for('forecast_new'))

        from scheduler.algorithm import get_holidays
        from scheduler.import_mapping import resolve_import

        parse_abnahme_de_file = getattr(parser_mod, 'parse_abnahme_de_file', None)
        parse_forecast_calculation_file = getattr(parser_mod, 'parse_forecast_calculation_file', None)

        try:
            tfc_rows, tfc_warnings, tfc_tier = resolve_import('tfc_forecast', tfc_path, parser_mod.parse_tfc_file)
            if abnahme_path and parse_abnahme_de_file:
                de_map, de_warnings, de_tier = resolve_import('abnahme_de', abnahme_path, parse_abnahme_de_file)
            elif abnahme_path:
                de_map, de_warnings, de_tier = {}, [_("This project's parser doesn't support an Abnahme DE file — it was ignored.")], 'deterministic'
            else:
                de_map, de_warnings, de_tier = {}, [], 'deterministic'
            if fc_path and parse_forecast_calculation_file:
                ks_agents_map, fc_warnings = parse_forecast_calculation_file(fc_path)
            elif fc_path:
                ks_agents_map, fc_warnings = {}, [_("This project's parser doesn't support a Forecast Calculation file — it was ignored.")]
            else:
                ks_agents_map, fc_warnings = {}, []
        except Exception as e:
            flash(_('Error parsing files: %(error)s', error=e), 'danger')
            return redirect(url_for('forecast_new'))

        if not tfc_rows:
            flash(_('No data found in Client Forecast file.'), 'danger')
            return redirect(url_for('forecast_new'))

        combined_warning = _log_import_result(
            'tfc_forecast', tfc_file.filename, tfc_warnings, tfc_tier
        )
        if abnahme_path:
            w = _log_import_result('abnahme_de', abnahme_file.filename, de_warnings, de_tier)
            combined_warning = f'{combined_warning} {w}' if combined_warning and w else (combined_warning or w)
        if fc_path:
            w = _log_import_warnings('forecast_calc', fc_calc_file.filename, fc_warnings)
            combined_warning = f'{combined_warning} {w}' if combined_warning and w else (combined_warning or w)
        if combined_warning:
            flash(_('Format warning: %(msg)s', msg=combined_warning), 'warning')

        start_d = min(r['date'] for r in tfc_rows)
        end_d = max(r['date'] for r in tfc_rows)

        period = ForecastPeriod(name=name, start_date=start_d, end_date=end_d)
        db.session.add(period)
        db.session.flush()

        # Business params for Kosovo agent calculation
        from scheduler.erlang import required_agents as erlang_required_agents

        params = {p.key: float(p.value) for p in BusinessParam.query.all()}
        stk = params.get('stk_per_hour', 3.5)
        std_tag = params.get('hours_day', 8.0)
        absence = params.get('absence_rate', 0.15)
        default_wd = int(params.get('default_agents_weekday', 220))
        default_sat = int(params.get('default_agents_saturday', 55))
        default_sun = int(params.get('default_agents_sunday', 0))
        aht_sync = params.get('aht_sync', 10.0)
        aht_async = params.get('aht_async', 15.0)
        aht_chat = params.get('aht_chat', 12.0)
        target_sl = params.get('target_service_level', 0.80)
        target_asa_min = params.get('target_asa', 20.0) / 60.0
        max_occ = params.get('max_occupancy', 0.85)

        holidays = get_holidays(start_d.year)

        def _erlang_required_for_day(slots, de_frac):
            """Peak-interval Erlang C requirement across a day's half-hourly
            slots, prorating Germany's daily contribution evenly across slots
            by volume share (no half-hourly breakdown of it exists). Returns
            None if there's no slot data to work with, so the caller falls
            back to the simpler daily-aggregate formula — some parsers may
            not supply half-hourly slots at all."""
            if not slots:
                return None
            peak = 0
            for vals in slots.values():
                sync_v = vals.get('sync', 0) * (1 - de_frac)
                async_v = vals.get('async', 0) * (1 - de_frac)
                chat_v = vals.get('chat', 0) * (1 - de_frac)
                slot_total = sync_v + async_v + chat_v
                if slot_total <= 0:
                    continue
                blended_aht = (sync_v * aht_sync + async_v * aht_async + chat_v * aht_chat) / slot_total
                agents = erlang_required_agents(
                    transactions=slot_total, aht_minutes=blended_aht, asa_minutes=target_asa_min,
                    interval_minutes=30, shrinkage=absence, service_level=target_sl, max_occupancy=max_occ,
                )
                peak = max(peak, agents)
            return peak

        for row in tfc_rows:
            d = row['date']
            total_contacts = row['total_sync'] + row['total_async'] + row['total_chat']
            de_contrib = de_map.get(d, 0)
            ks_contacts = max(0, total_contacts - de_contrib)
            de_frac = (de_contrib / total_contacts) if total_contacts else 0.0

            # Required agents: from Forecast Calc file (preferred); else Erlang C
            # sized per half-hourly interval, taking the day's peak (falls back
            # to the simpler daily-aggregate estimate if a parser doesn't supply
            # half-hourly slots); else the flat default.
            if d in ks_agents_map:
                required = ks_agents_map[d]
            elif d.weekday() < 5 and d not in holidays:
                required = _erlang_required_for_day(row.get('slots'), de_frac)
                if not required:
                    eff_hours = std_tag * (1 - absence)
                    required = int(round(ks_contacts / (stk * eff_hours))) if ks_contacts else default_wd
                required = required or default_wd
            elif d.weekday() == 5:
                required = default_sat
            elif d.weekday() == 6:
                required = default_sun
            else:
                required = 0

            day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
            df = DailyForecast(
                period_id=period.id,
                date=d,
                day_of_week=day_names[d.weekday()],
                calendar_week=row.get('kw'),
                total_sync=row['total_sync'],
                total_async=row['total_async'],
                total_chat=row['total_chat'],
                germany_contribution=de_contrib,
                required_ks_agents=required,
            )
            db.session.add(df)
            db.session.flush()

            for slot_time, vals in row.get('slots', {}).items():
                db.session.add(HalfHourlyForecast(
                    daily_id=df.id,
                    slot_time=slot_time,
                    sync_volume=vals.get('sync', 0),
                    async_volume=vals.get('async', 0),
                    chat_volume=vals.get('chat', 0),
                ))

        db.session.commit()
        flash(_('Forecast period "%(name)s" imported successfully (%(n)s days).', name=name, n=len(tfc_rows)), 'success')
        return redirect(url_for('forecast_detail', period_id=period.id))

    return render_template('forecast_new.html')


@app.route('/forecast/<int:period_id>')
def forecast_detail(period_id):
    period = ForecastPeriod.query.get_or_404(period_id)
    forecasts = period.daily_forecasts

    # Chart data: dates + total contacts per day
    chart_labels = [f.date.strftime('%d.%m') for f in forecasts]
    chart_sync   = [round(f.total_sync, 0)  for f in forecasts]
    chart_async  = [round(f.total_async, 0) for f in forecasts]
    chart_chat   = [round(f.total_chat, 0)  for f in forecasts]
    chart_agents = [f.required_ks_agents     for f in forecasts]

    return render_template('forecast_detail.html',
                           period=period,
                           forecasts=forecasts,
                           chart_labels=json.dumps(chart_labels),
                           chart_sync=json.dumps(chart_sync),
                           chart_async=json.dumps(chart_async),
                           chart_chat=json.dumps(chart_chat),
                           chart_agents=json.dumps(chart_agents))


@app.route('/forecast/<int:period_id>/delete', methods=['POST'])
def forecast_delete(period_id):
    period = ForecastPeriod.query.get_or_404(period_id)
    db.session.delete(period)
    db.session.commit()
    flash(_('Forecast period deleted.'), 'info')
    return redirect(url_for('forecast_list'))


# ────────────────────────────────────────────────────────────────────────────
# Employees
# ────────────────────────────────────────────────────────────────────────────

@app.route('/employees')
def employees_list():
    status_filter = request.args.get('status', 'active')
    team_filter = request.args.get('team', '')
    search = request.args.get('q', '').strip()
    query = Employee.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    if team_filter:
        query = query.filter_by(team=team_filter)
    if search:
        query = query.filter(Employee.name.ilike(f'%{search}%'))
    employees = query.order_by(Employee.team, Employee.name).all()
    shift_templates = ShiftTemplate.query.all()
    return render_template('employees_list.html',
                           employees=employees,
                           shift_templates=shift_templates,
                           teams=get_teams(),
                           status_filter=status_filter,
                           team_filter=team_filter,
                           search=search)


@app.route('/employees/groups')
def employee_groups():
    schedule_groups = ScheduleGroup.query.order_by(ScheduleGroup.name).all()
    rotation_patterns = RotationPattern.query.order_by(RotationPattern.name).all()
    return render_template('employee_groups.html', schedule_groups=schedule_groups,
                           rotation_patterns=rotation_patterns)


@app.route('/employees/import', methods=['GET', 'POST'])
def employees_import():
    if request.method == 'POST':
        file = request.files.get('employee_file')
        use_ai = request.form.get('use_ai') == '1'

        if not file or not file.filename:
            flash(_('Please select a file.'), 'danger')
            return redirect(url_for('employees_import'))

        from scheduler.parsers import get_parser_module
        parser_mod = get_parser_module(g.active_project)
        if parser_mod is None or not hasattr(parser_mod, 'parse_employee_file'):
            flash(_("Employee import isn't set up for this project yet — its file format "
                     "needs a parser written for it first."), 'warning')
            return redirect(url_for('employees_import'))

        path = os.path.join(upload_folder(), f'emp_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx')
        file.save(path)

        raw_employees, emp_warnings = parser_mod.parse_employee_file(path)

        combined_warning = _log_import_warnings('employee_spec', file.filename, emp_warnings)
        if combined_warning:
            flash(_('Format warning: %(msg)s', msg=combined_warning), 'warning')

        constraints_list = [None] * len(raw_employees)
        if use_ai:
            from scheduler.ai_parser import parse_employee_notes
            api_key = os.environ.get('ANTHROPIC_API_KEY', '')
            if api_key:
                constraints_list = parse_employee_notes(raw_employees, api_key)
                flash(_('AI parsed %(n)s employees.', n=len(raw_employees)), 'info')
            else:
                flash(_('ANTHROPIC_API_KEY not set — using defaults.'), 'warning')

        # Store parsed data in session for the review step
        preview = []
        for i, emp_raw in enumerate(raw_employees):
            c = constraints_list[i] or {}
            preview.append({
                'name': emp_raw['name'],
                'team': emp_raw['team'],
                'raw_notes': emp_raw['raw_notes'],
                'works_saturday': emp_raw['works_saturday'],
                'works_sunday': emp_raw['works_sunday'],
                'works_holidays': emp_raw['works_holidays'],
                'fte_percent': int((emp_raw['fte_fraction'] or 1.0) * 100),
                'shift_type': c.get('shift_type', 'full'),
                'custom_start': c.get('custom_start') or '',
                'custom_end': c.get('custom_end') or '',
                'notes': c.get('notes', ''),
                'excluded_dates': c.get('excluded_dates', []),
                'day_restrictions': c.get('day_restrictions', []),
                'same_schedule_as': c.get('same_schedule_as') or '',
            })

        return render_template('employees_import_review.html', preview=preview,
                               shift_templates=ShiftTemplate.query.all())

    return render_template('employees_import.html')


@app.route('/employees/import/confirm', methods=['POST'])
def employees_import_confirm():
    shift_templates = {t.name.lower(): t.id for t in ShiftTemplate.query.all()}
    count = 0
    i = 0
    while f'name_{i}' in request.form:
        name = request.form.get(f'name_{i}', '').strip()
        if not name:
            i += 1
            continue

        team = request.form.get(f'team_{i}', '')
        fte_percent = int(request.form.get(f'fte_percent_{i}', 100))
        fte_mode = request.form.get(f'fte_mode_{i}', 'days')
        shift_type = request.form.get(f'shift_type_{i}', 'full')
        custom_start = request.form.get(f'custom_start_{i}', '') or None
        custom_end = request.form.get(f'custom_end_{i}', '') or None
        works_saturday = request.form.get(f'works_saturday_{i}', 'no')
        works_sunday = request.form.get(f'works_sunday_{i}', 'no')
        works_holidays = request.form.get(f'works_holidays_{i}') == '1'
        raw_notes = request.form.get(f'raw_notes_{i}', '')
        contract_notes = request.form.get(f'notes_{i}', '')
        same_schedule_as = request.form.get(f'same_schedule_as_{i}', '').strip()

        # Map shift_type name to template id
        tpl_id = shift_templates.get(shift_type.lower())

        # Check if employee already exists (update) or create new
        emp = Employee.query.filter_by(name=name).first()
        if not emp:
            emp = Employee(name=name)
            db.session.add(emp)

        emp.team = team
        emp.fte_percent = fte_percent
        emp.fte_mode = fte_mode
        emp.shift_template_id = tpl_id
        emp.custom_start = custom_start
        emp.custom_end = custom_end
        emp.works_saturday = works_saturday
        emp.works_sunday = works_sunday
        emp.works_holidays = works_holidays
        emp.raw_notes = raw_notes
        emp.contract_notes = contract_notes
        emp.updated_at = datetime.utcnow()
        db.session.flush()

        # Handle excluded dates
        excl_raw = request.form.get(f'excluded_dates_{i}', '')
        if excl_raw:
            ExcludedDate.query.filter_by(employee_id=emp.id).delete()
            for ds in excl_raw.split(','):
                ds = ds.strip()
                if ds:
                    try:
                        d = datetime.strptime(ds, '%Y-%m-%d').date()
                        db.session.add(ExcludedDate(employee_id=emp.id, date=d))
                    except ValueError:
                        pass

        # Handle day restrictions (AI-parsed, reviewable per day-of-week)
        DayRestriction.query.filter_by(employee_id=emp.id).delete()
        for dow in range(7):
            val = request.form.get(f'restriction_{i}_{dow}')
            if val == 'off':
                db.session.add(DayRestriction(employee_id=emp.id, day_of_week=dow, is_off=True))
            elif val:
                db.session.add(DayRestriction(employee_id=emp.id, day_of_week=dow, shift_type=val))

        # Handle "scheduled together" — find-or-create a ScheduleGroup for the pair
        if same_schedule_as:
            partner = Employee.query.filter_by(name=same_schedule_as).first()
            if partner:
                group = partner.schedule_group
                if not group:
                    group = ScheduleGroup(name=f'{partner.name} / {emp.name}')
                    db.session.add(group)
                    db.session.flush()
                    partner.schedule_group_id = group.id
                emp.schedule_group_id = group.id

        count += 1
        i += 1

    db.session.commit()
    flash(_('%(n)s employees imported/updated.', n=count), 'success')
    return redirect(url_for('employees_list'))


@app.route('/employees/new', methods=['GET', 'POST'])
def employee_new():
    if request.method == 'POST':
        return _save_employee(None)
    shift_templates = ShiftTemplate.query.all()
    return render_template('employee_detail.html', employee=None,
                           shift_templates=shift_templates, teams=get_teams(),
                           schedule_groups=ScheduleGroup.query.order_by(ScheduleGroup.name).all(),
                           rotation_patterns=RotationPattern.query.order_by(RotationPattern.name).all(),
                           day_names=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'])


@app.route('/employees/<int:emp_id>', methods=['GET', 'POST'])
def employee_detail(emp_id):
    emp = Employee.query.get_or_404(emp_id)
    if request.method == 'POST':
        return _save_employee(emp)
    shift_templates = ShiftTemplate.query.all()
    return render_template('employee_detail.html', employee=emp,
                           shift_templates=shift_templates, teams=get_teams(),
                           schedule_groups=ScheduleGroup.query.order_by(ScheduleGroup.name).all(),
                           rotation_patterns=RotationPattern.query.order_by(RotationPattern.name).all(),
                           day_names=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'])


def _save_employee(emp):
    is_new = emp is None
    if is_new:
        emp = Employee()
        db.session.add(emp)

    first_name = request.form.get('first_name', '').strip()
    last_name = request.form.get('last_name', '').strip()
    emp.name = f'{first_name} {last_name}'.strip()
    emp.employee_number = request.form.get('employee_number', '').strip() or None
    emp.team = request.form.get('team', '')
    emp.fte_percent = int(request.form.get('fte_percent', 100))
    emp.fte_mode = request.form.get('fte_mode', 'days')
    tpl_id = request.form.get('shift_template_id')
    emp.shift_template_id = int(tpl_id) if tpl_id else None
    emp.custom_start = request.form.get('custom_start', '') or None
    emp.custom_end = request.form.get('custom_end', '') or None
    custom_hours = request.form.get('custom_hours')
    try:
        emp.custom_hours = float(custom_hours) if emp.custom_start and emp.custom_end and custom_hours else None
    except ValueError:
        emp.custom_hours = None
    emp.works_saturday = request.form.get('works_saturday', 'no')
    emp.works_sunday = request.form.get('works_sunday', 'no')
    emp.works_holidays = request.form.get('works_holidays') == '1'
    emp.status = request.form.get('status', 'active')
    emp.raw_notes = request.form.get('raw_notes', '')
    emp.contract_notes = request.form.get('contract_notes', '')
    group_id = request.form.get('schedule_group_id')
    emp.schedule_group_id = int(group_id) if group_id else None
    rotation_id = request.form.get('rotation_pattern_id')
    emp.rotation_pattern_id = int(rotation_id) if rotation_id else None
    emp.updated_at = datetime.utcnow()

    db.session.flush()

    # Day restrictions
    DayRestriction.query.filter_by(employee_id=emp.id).delete()
    for dow in range(7):
        val = request.form.get(f'restriction_{dow}')
        if val == 'off':
            db.session.add(DayRestriction(employee_id=emp.id, day_of_week=dow, is_off=True))
        elif val:
            db.session.add(DayRestriction(employee_id=emp.id, day_of_week=dow, shift_type=val))

    # Excluded dates
    ExcludedDate.query.filter_by(employee_id=emp.id).delete()
    excl_raw = request.form.get('excluded_dates', '')
    for ds in excl_raw.split(','):
        ds = ds.strip()
        if ds:
            try:
                d = datetime.strptime(ds, '%Y-%m-%d').date()
                reason = request.form.get(f'excl_reason_{ds}', '')
                db.session.add(ExcludedDate(employee_id=emp.id, date=d, reason=reason))
            except ValueError:
                pass

    db.session.commit()
    action = _('created') if is_new else _('updated')
    flash(_('Employee %(name)s %(action)s.', name=emp.name, action=action), 'success')
    if is_new:
        return redirect(url_for('employees_list'))
    return redirect(url_for('employee_detail', emp_id=emp.id))


@app.route('/employees/<int:emp_id>/delete', methods=['POST'])
def employee_delete(emp_id):
    emp = Employee.query.get_or_404(emp_id)
    name = emp.name
    db.session.delete(emp)
    db.session.commit()
    flash(_('%(name)s deleted.', name=name), 'info')
    return redirect(url_for('employees_list'))


@app.route('/employees/bulk-update', methods=['POST'])
def employees_bulk_update():
    """Update the common per-row fields (team, FTE, shift, weekends, holidays,
    status) for many employees at once from the Employees list's Edit Mode.
    Day restrictions, excluded dates, custom hours, notes, and schedule group
    stay on the single-employee detail page — bulk mode only covers what's
    already a column in the list."""
    valid_shift_ids = {t.id for t in ShiftTemplate.query.all()}
    emp_ids = request.form.getlist('emp_ids')
    count = 0
    for id_str in emp_ids:
        emp = Employee.query.get(int(id_str))
        if not emp:
            continue

        emp.employee_number = request.form.get(f'employee_number_{id_str}', '').strip() or None
        emp.team = request.form.get(f'team_{id_str}', emp.team)

        fte = request.form.get(f'fte_percent_{id_str}')
        if fte:
            emp.fte_percent = int(fte)

        mode = request.form.get(f'fte_mode_{id_str}')
        if mode:
            emp.fte_mode = mode

        tpl_id = request.form.get(f'shift_template_id_{id_str}')
        emp.shift_template_id = int(tpl_id) if tpl_id and int(tpl_id) in valid_shift_ids else None

        sat = request.form.get(f'works_saturday_{id_str}')
        if sat:
            emp.works_saturday = sat

        sun = request.form.get(f'works_sunday_{id_str}')
        if sun:
            emp.works_sunday = sun

        emp.works_holidays = request.form.get(f'works_holidays_{id_str}') == '1'

        status = request.form.get(f'status_{id_str}')
        if status:
            emp.status = status

        emp.updated_at = datetime.utcnow()
        count += 1

    db.session.commit()
    flash(_('%(n)s employees updated.', n=count), 'success')

    status_filter = request.form.get('status_filter', '')
    team_filter = request.form.get('team_filter', '')
    search_filter = request.form.get('search_filter', '')
    return redirect(url_for('employees_list', status=status_filter, team=team_filter, q=search_filter))


# ────────────────────────────────────────────────────────────────────────────
# Schedule
# ────────────────────────────────────────────────────────────────────────────

@app.route('/schedule')
def schedule_list():
    schedules = Schedule.query.order_by(Schedule.generated_at.desc()).all()
    periods = ForecastPeriod.query.order_by(ForecastPeriod.start_date.desc()).all()
    return render_template('schedule_list.html', schedules=schedules, periods=periods,
                           month_names=MONTH_NAMES)


@app.route('/schedule/generate', methods=['POST'])
def schedule_generate():
    year = int(request.form.get('year', date.today().year))
    month = int(request.form.get('month', date.today().month))
    period_id = request.form.get('period_id') or None
    name = request.form.get('name', '').strip() or f'Schedule {MONTH_NAMES[month]} {year}'

    sched = Schedule(
        period_id=int(period_id) if period_id else None,
        name=name, year=year, month=month,
    )
    db.session.add(sched)
    db.session.commit()

    from scheduler.algorithm import generate_schedule
    summary = generate_schedule(sched.id, year, month)
    flash(_('Schedule generated: %(n)s employees, %(pct)s%% avg coverage.',
            n=summary["total_employees"], pct=summary["avg_coverage_pct"]), 'success')
    return redirect(url_for('schedule_detail', schedule_id=sched.id))


@app.route('/schedule/<int:schedule_id>')
def schedule_detail(schedule_id):
    sched = Schedule.query.get_or_404(schedule_id)
    year, month = sched.year, sched.month
    _, days_in_month = calendar.monthrange(year, month)
    all_days = [date(year, month, d) for d in range(1, days_in_month + 1)]

    employees = (Employee.query.filter_by(status='active')
                 .order_by(Employee.team, Employee.name).all())

    # Index assignments
    assignments_map: dict[tuple, ShiftAssignment] = {}
    for a in ShiftAssignment.query.filter_by(schedule_id=schedule_id).all():
        assignments_map[(a.employee_id, a.date)] = a

    # Build rows: team separator + employee rows
    rows = []
    prev_team = None
    for emp in employees:
        if emp.team != prev_team:
            rows.append({'type': 'team', 'team': emp.team or '—'})
            prev_team = emp.team
        day_cells = []
        for day in all_days:
            a = assignments_map.get((emp.id, day))
            day_cells.append({
                'day': day,
                'assignment': a,
                'code': a.display_code if a else '',
                'color': a.cell_color if a else '#f8fafc',
                'status': a.status if a else '',
            })
        rows.append({'type': 'employee', 'employee': emp, 'cells': day_cells})

    # Daily coverage stats
    daily_stats = []
    for day in all_days:
        working = sum(
            1 for (eid, d), a in assignments_map.items()
            if d == day and a.status == 'work'
        )
        fc = DailyForecast.query.filter_by(date=day).first()
        req = fc.required_ks_agents if fc and fc.required_ks_agents else (
            220 if day.weekday() < 5 else 55 if day.weekday() == 5 else 0
        )
        daily_stats.append({
            'date': day.isoformat(), 'required': req, 'scheduled': working,
            'pct': round(working / req * 100, 1) if req else 100,
        })

    return render_template('schedule_detail.html',
                           schedule=sched, all_days=all_days,
                           rows=rows, daily_stats=daily_stats,
                           month_name=MONTH_NAMES[month])


@app.route('/schedule/<int:schedule_id>/export')
def schedule_export(schedule_id):
    from scheduler.output import export_schedule
    buf = export_schedule(schedule_id, project_config(g.active_project)['display_name'])
    sched = Schedule.query.get_or_404(schedule_id)
    filename = f'schedule_{MONTH_NAMES[sched.month]}_{sched.year}.xlsx'
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/schedule/<int:schedule_id>/delete', methods=['POST'])
def schedule_delete(schedule_id):
    sched = Schedule.query.get_or_404(schedule_id)
    db.session.delete(sched)
    db.session.commit()
    flash(_('Schedule deleted.'), 'info')
    return redirect(url_for('schedule_list'))


@app.route('/schedule/<int:schedule_id>/override', methods=['POST'])
def assignment_override(schedule_id):
    """Manually override a single cell in the schedule."""
    emp_id = int(request.form.get('employee_id'))
    day_str = request.form.get('date')
    new_status = request.form.get('status', 'work')
    shift_start = request.form.get('shift_start') or None
    shift_end = request.form.get('shift_end') or None

    d = datetime.strptime(day_str, '%Y-%m-%d').date()
    a = ShiftAssignment.query.filter_by(
        schedule_id=schedule_id, employee_id=emp_id, date=d).first()
    if a is None:
        a = ShiftAssignment(schedule_id=schedule_id, employee_id=emp_id, date=d, status='work')
        db.session.add(a)

    a.status = new_status
    a.shift_start = shift_start
    a.shift_end = shift_end
    a.is_manual = True
    if shift_start and shift_end:
        sh, sm = map(int, shift_start.split(':'))
        eh, em = map(int, shift_end.split(':'))
        a.hours_worked = (eh * 60 + em - sh * 60 - sm) / 60
    db.session.commit()
    return jsonify({'ok': True, 'code': a.display_code, 'color': a.cell_color})


# ────────────────────────────────────────────────────────────────────────────
# Settings
# ────────────────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@require_role('admin')
def settings():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'upload_logo':
            file = request.files.get('logo')
            if not file or not file.filename:
                flash(_('Please choose an image file.'), 'danger')
            else:
                ext = os.path.splitext(file.filename)[1].lower()
                if ext not in _LOGO_EXTENSIONS:
                    flash(_('Logo must be PNG, JPG, SVG, or WEBP.'), 'danger')
                else:
                    branding_dir = os.path.join(upload_folder(), 'branding')
                    os.makedirs(branding_dir, exist_ok=True)
                    for old in glob.glob(os.path.join(branding_dir, 'logo.*')):
                        os.remove(old)
                    file.save(os.path.join(branding_dir, f'logo{ext}'))
                    flash(_('Logo updated.'), 'success')

        elif action == 'remove_logo':
            branding_dir = os.path.join(upload_folder(), 'branding')
            for old in glob.glob(os.path.join(branding_dir, 'logo.*')):
                os.remove(old)
            flash(_('Logo removed.'), 'info')

        return redirect(url_for('settings'))

    return render_template('settings.html', has_logo=bool(project_logo_path()))


# ────────────────────────────────────────────────────────────────────────────
# Business parameters
# ────────────────────────────────────────────────────────────────────────────

@app.route('/business-parameters', methods=['GET', 'POST'])
@require_role('admin')
def business_parameters():
    if request.method == 'POST':
        for param in BusinessParam.query.all():
            val = request.form.get(f'param_{param.key}')
            if val is not None:
                param.value = val.strip()
        db.session.commit()
        flash(_('Business parameters saved.'), 'success')
        return redirect(url_for('business_parameters'))

    params = BusinessParam.query.order_by(BusinessParam.category, BusinessParam.label).all()
    return render_template('business_parameters.html', params=params)


# ────────────────────────────────────────────────────────────────────────────
# AI settings (global — every project, superadmin only)
# ────────────────────────────────────────────────────────────────────────────

@app.route('/ai/settings', methods=['GET', 'POST'])
@require_technical_admin
def ai_settings():
    if request.method == 'POST':
        for setting in GlobalSetting.query.all():
            val = request.form.get(f'global_{setting.key}')
            if val is not None:
                setting.value = val.strip()
        db.session.commit()
        flash(_('AI settings saved (applies to every project).'), 'success')
        return redirect(url_for('ai_settings'))

    global_settings = [s for s in GlobalSetting.query.order_by(GlobalSetting.key).all()
                       if not s.key.startswith('_')]
    return render_template('ai_settings.html', global_settings=global_settings)


# ────────────────────────────────────────────────────────────────────────────
# Shift templates
# ────────────────────────────────────────────────────────────────────────────

@app.route('/shifts')
def shift_templates_list():
    shift_templates = ShiftTemplate.query.order_by(ShiftTemplate.start_time).all()
    return render_template('shift_templates.html', shift_templates=shift_templates)


@app.route('/shifts/add', methods=['POST'])
@require_role('admin')
def shift_template_add():
    name = request.form.get('shift_name', '').strip()
    start = request.form.get('shift_start', '08:00')
    end = request.form.get('shift_end', '16:00')
    color = request.form.get('shift_color', '#6366f1')
    make_default = request.form.get('shift_is_default') == '1'
    try:
        hours = float(request.form.get('shift_hours'))
    except (TypeError, ValueError):
        sh, sm = map(int, start.split(':'))
        eh, em = map(int, end.split(':'))
        hours = (eh * 60 + em - sh * 60 - sm) / 60
    if name:
        if make_default:
            ShiftTemplate.query.update({'is_default': False})
        tpl = ShiftTemplate(name=name, start_time=start, end_time=end,
                             hours=hours, color=color, is_default=make_default)
        db.session.add(tpl)
        db.session.commit()
        flash(_('Shift template "%(name)s" added.', name=name), 'success')
    return redirect(url_for('shift_templates_list'))


@app.route('/shifts/<int:tpl_id>/edit', methods=['POST'])
@require_role('admin')
def shift_template_edit(tpl_id):
    tpl = ShiftTemplate.query.get_or_404(tpl_id)
    tpl.name = request.form.get('shift_name', tpl.name).strip()
    tpl.start_time = request.form.get('shift_start', tpl.start_time)
    tpl.end_time = request.form.get('shift_end', tpl.end_time)
    tpl.color = request.form.get('shift_color', tpl.color)
    try:
        tpl.hours = float(request.form.get('shift_hours'))
    except (TypeError, ValueError):
        sh, sm = map(int, tpl.start_time.split(':'))
        eh, em = map(int, tpl.end_time.split(':'))
        tpl.hours = (eh * 60 + em - sh * 60 - sm) / 60
    db.session.commit()
    flash(_('Shift "%(name)s" updated.', name=tpl.name), 'success')
    return redirect(url_for('shift_templates_list'))


@app.route('/shifts/<int:tpl_id>/delete', methods=['POST'])
@require_role('admin')
def shift_template_delete(tpl_id):
    tpl = ShiftTemplate.query.get(tpl_id)
    if tpl and not tpl.is_default:
        db.session.delete(tpl)
        db.session.commit()
        flash(_('Shift "%(name)s" deleted.', name=tpl.name), 'info')
    else:
        flash(_('Cannot delete a default shift template.'), 'warning')
    return redirect(url_for('shift_templates_list'))


@app.route('/shifts/<int:tpl_id>/set-default', methods=['POST'])
@require_role('admin')
def shift_template_set_default(tpl_id):
    tpl = ShiftTemplate.query.get_or_404(tpl_id)
    ShiftTemplate.query.update({'is_default': False})
    tpl.is_default = True
    db.session.commit()
    flash(_('"%(name)s" is now the default shift template.', name=tpl.name), 'success')
    return redirect(url_for('shift_templates_list'))


# ────────────────────────────────────────────────────────────────────────────
# Rotation patterns (weekly-alternating shifts, e.g. Morning one week / Afternoon
# the next). The cycle's "current week" is computed purely from anchor_date +
# real calendar weeks (scheduler/algorithm.py::_rotation_shift_for_date), so it
# stays continuous across month boundaries without any special-casing here.
# ────────────────────────────────────────────────────────────────────────────

@app.route('/rotation-patterns')
def rotation_patterns_list():
    rotation_patterns = RotationPattern.query.order_by(RotationPattern.name).all()
    shift_templates = ShiftTemplate.query.order_by(ShiftTemplate.start_time).all()
    return render_template('rotation_patterns.html',
                            rotation_patterns=rotation_patterns,
                            shift_templates=shift_templates,
                            shift_templates_json=[t.to_dict() for t in shift_templates])


@app.route('/rotation-patterns/add', methods=['POST'])
@require_role('admin')
def rotation_pattern_add():
    name = request.form.get('name', '').strip()
    anchor_date = request.form.get('anchor_date', '')
    tpl_ids = [t for t in request.form.getlist('week_shift') if t]
    if not name:
        flash(_('Rotation pattern name cannot be empty.'), 'danger')
    elif RotationPattern.query.filter_by(name=name).first():
        flash(_('A rotation pattern named "%(name)s" already exists.', name=name), 'warning')
    elif not anchor_date:
        flash(_('Please choose a start date for the rotation.'), 'danger')
    elif len(tpl_ids) < 1:
        flash(_('A rotation needs at least one week in its cycle.'), 'danger')
    else:
        pattern = RotationPattern(name=name, anchor_date=date.fromisoformat(anchor_date))
        db.session.add(pattern)
        db.session.flush()
        for i, tid in enumerate(tpl_ids):
            db.session.add(RotationCycleWeek(rotation_pattern_id=pattern.id, position=i,
                                              shift_template_id=int(tid)))
        db.session.commit()
        flash(_('Rotation pattern "%(name)s" created.', name=name), 'success')
    return redirect(url_for('rotation_patterns_list'))


@app.route('/rotation-patterns/<int:pattern_id>/edit', methods=['POST'])
@require_role('admin')
def rotation_pattern_edit(pattern_id):
    pattern = RotationPattern.query.get_or_404(pattern_id)
    name = request.form.get('name', '').strip()
    anchor_date = request.form.get('anchor_date', '')
    tpl_ids = [t for t in request.form.getlist('week_shift') if t]
    if not name:
        flash(_('Rotation pattern name cannot be empty.'), 'danger')
    elif RotationPattern.query.filter(RotationPattern.name == name, RotationPattern.id != pattern_id).first():
        flash(_('A rotation pattern named "%(name)s" already exists.', name=name), 'warning')
    elif not anchor_date:
        flash(_('Please choose a start date for the rotation.'), 'danger')
    elif len(tpl_ids) < 1:
        flash(_('A rotation needs at least one week in its cycle.'), 'danger')
    else:
        pattern.name = name
        pattern.anchor_date = date.fromisoformat(anchor_date)
        RotationCycleWeek.query.filter_by(rotation_pattern_id=pattern.id).delete()
        for i, tid in enumerate(tpl_ids):
            db.session.add(RotationCycleWeek(rotation_pattern_id=pattern.id, position=i,
                                              shift_template_id=int(tid)))
        db.session.commit()
        flash(_('Rotation pattern "%(name)s" updated.', name=pattern.name), 'success')
    return redirect(url_for('rotation_patterns_list'))


@app.route('/rotation-patterns/<int:pattern_id>/delete', methods=['POST'])
@require_role('admin')
def rotation_pattern_delete(pattern_id):
    pattern = RotationPattern.query.get_or_404(pattern_id)
    in_use = (Employee.query.filter_by(rotation_pattern_id=pattern.id).count() +
              ScheduleGroup.query.filter_by(rotation_pattern_id=pattern.id).count())
    if in_use:
        flash(_('Cannot delete "%(name)s" — it is still assigned to %(n)s employee(s)/group(s). '
                 'Reassign them first.', name=pattern.name, n=in_use), 'warning')
    else:
        db.session.delete(pattern)
        db.session.commit()
        flash(_('Rotation pattern "%(name)s" deleted.', name=pattern.name), 'info')
    return redirect(url_for('rotation_patterns_list'))


# ────────────────────────────────────────────────────────────────────────────
# Public holidays — per-project, feeds scheduler/algorithm.py via
# _load_active_holidays(). Editable data now instead of a hardcoded dict, so
# next year's dates are a form submission, not a code deploy.
# ────────────────────────────────────────────────────────────────────────────

@app.route('/public-holidays')
def public_holidays_list():
    holidays = PublicHoliday.query.order_by(PublicHoliday.date).all()
    return render_template('public_holidays.html', holidays=holidays)


@app.route('/public-holidays/add', methods=['POST'])
@require_role('admin')
def public_holiday_add():
    name = request.form.get('name', '').strip()
    date_str = request.form.get('date', '')
    if not name:
        flash(_('Holiday name cannot be empty.'), 'danger')
    elif not date_str:
        flash(_('Please choose a date.'), 'danger')
    else:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            flash(_('Invalid date.'), 'danger')
            return redirect(url_for('public_holidays_list'))
        if PublicHoliday.query.filter_by(date=d).first():
            flash(_('A holiday is already set for that date.'), 'warning')
        else:
            db.session.add(PublicHoliday(date=d, name=name))
            db.session.commit()
            _load_active_holidays()
            flash(_('Holiday "%(name)s" added.', name=name), 'success')
    return redirect(url_for('public_holidays_list'))


@app.route('/public-holidays/<int:holiday_id>/delete', methods=['POST'])
@require_role('admin')
def public_holiday_delete(holiday_id):
    holiday = PublicHoliday.query.get_or_404(holiday_id)
    db.session.delete(holiday)
    db.session.commit()
    _load_active_holidays()
    flash(_('Holiday "%(name)s" removed.', name=holiday.name), 'info')
    return redirect(url_for('public_holidays_list'))


@app.route('/branding/logo')
@app.route('/branding/logo/<project>')
def project_logo(project=None):
    user = g.current_user
    if not user:
        return '', 404
    target = project or g.active_project
    if target not in user.accessible_projects(discover_projects()):
        return '', 404
    path = project_logo_path(target)
    if not path:
        return '', 404
    return send_file(path)


# ────────────────────────────────────────────────────────────────────────────
# Team management
# ────────────────────────────────────────────────────────────────────────────

@app.route('/settings/teams/add', methods=['POST'])
@require_role('admin')
def team_add():
    name = request.form.get('name', '').strip()
    if not name:
        flash(_('Team name cannot be empty.'), 'danger')
    elif Team.query.filter_by(name=name).first():
        flash(_('A team named "%(name)s" already exists.', name=name), 'warning')
    else:
        db.session.add(Team(name=name))
        db.session.commit()
        flash(_('Team "%(name)s" created.', name=name), 'success')
    return redirect(url_for('teams_list'))


@app.route('/settings/teams/<int:team_id>/rename', methods=['POST'])
@require_role('admin')
def team_rename(team_id):
    team = Team.query.get_or_404(team_id)
    new_name = request.form.get('name', '').strip()
    if not new_name:
        flash(_('Team name cannot be empty.'), 'danger')
    elif Team.query.filter(Team.name == new_name, Team.id != team_id).first():
        flash(_('A team named "%(name)s" already exists.', name=new_name), 'warning')
    else:
        old_name = team.name
        # Rename in employee records too
        Employee.query.filter_by(team=old_name).update({'team': new_name})
        team.name = new_name
        db.session.commit()
        flash(_('Team renamed from "%(old)s" to "%(new)s".', old=old_name, new=new_name), 'success')
    return redirect(url_for('teams_list') + f'#team-card-{team_id}')


@app.route('/settings/teams/<int:team_id>/delete', methods=['POST'])
@require_role('admin')
def team_delete(team_id):
    team = Team.query.get_or_404(team_id)
    member_count = Employee.query.filter_by(team=team.name).count()
    if member_count:
        flash(_('Cannot delete "%(name)s" — it has %(n)s employees. '
                'Reassign or move them first.', name=team.name, n=member_count), 'danger')
    else:
        db.session.delete(team)
        db.session.commit()
        flash(_('Team "%(name)s" deleted.', name=team.name), 'info')
    return redirect(url_for('teams_list'))


# ────────────────────────────────────────────────────────────────────────────
# Schedule group management ("wants to be scheduled together")
# ────────────────────────────────────────────────────────────────────────────

@app.route('/settings/schedule-groups/add', methods=['POST'])
@require_role('admin')
def schedule_group_add():
    name = request.form.get('name', '').strip()
    rotation_id = request.form.get('rotation_pattern_id')
    if not name:
        flash(_('Group name cannot be empty.'), 'danger')
    elif ScheduleGroup.query.filter_by(name=name).first():
        flash(_('A schedule group named "%(name)s" already exists.', name=name), 'warning')
    else:
        db.session.add(ScheduleGroup(name=name, rotation_pattern_id=int(rotation_id) if rotation_id else None))
        db.session.commit()
        flash(_('Schedule group "%(name)s" created.', name=name), 'success')
    return redirect(url_for('employee_groups'))


@app.route('/settings/schedule-groups/<int:group_id>/set-rotation', methods=['POST'])
@require_role('admin')
def schedule_group_set_rotation(group_id):
    group = ScheduleGroup.query.get_or_404(group_id)
    rotation_id = request.form.get('rotation_pattern_id')
    group.rotation_pattern_id = int(rotation_id) if rotation_id else None
    db.session.commit()
    flash(_('Rotation updated for "%(name)s".', name=group.name), 'success')
    return redirect(url_for('employee_groups') + f'#group-card-{group_id}')


@app.route('/settings/schedule-groups/<int:group_id>/rename', methods=['POST'])
@require_role('admin')
def schedule_group_rename(group_id):
    group = ScheduleGroup.query.get_or_404(group_id)
    new_name = request.form.get('name', '').strip()
    if not new_name:
        flash(_('Group name cannot be empty.'), 'danger')
    elif ScheduleGroup.query.filter(ScheduleGroup.name == new_name, ScheduleGroup.id != group_id).first():
        flash(_('A schedule group named "%(name)s" already exists.', name=new_name), 'warning')
    else:
        old_name = group.name
        group.name = new_name
        db.session.commit()
        flash(_('Schedule group renamed from "%(old)s" to "%(new)s".', old=old_name, new=new_name), 'success')
    return redirect(url_for('employee_groups') + f'#group-card-{group_id}')


@app.route('/settings/schedule-groups/<int:group_id>/delete', methods=['POST'])
@require_role('admin')
def schedule_group_delete(group_id):
    group = ScheduleGroup.query.get_or_404(group_id)
    member_count = group.member_count
    if member_count:
        flash(_('Cannot delete "%(name)s" — it has %(n)s employees. '
                'Reassign or move them first.', name=group.name, n=member_count), 'danger')
    else:
        db.session.delete(group)
        db.session.commit()
        flash(_('Schedule group "%(name)s" deleted.', name=group.name), 'info')
    return redirect(url_for('employee_groups'))


# ────────────────────────────────────────────────────────────────────────────
# Teams
# ────────────────────────────────────────────────────────────────────────────

@app.route('/teams')
def teams_list():
    teams = Team.query.order_by(Team.name).all()
    teams_data = []
    for team in teams:
        members = (Employee.query.filter_by(team=team.name, status='active')
                   .order_by(Employee.name).all())
        avg_fte = (sum(e.fte_percent for e in members) / len(members)) if members else 0
        we_count = sum(1 for e in members if e.works_saturday == 'yes' or e.works_sunday == 'yes')
        shift_dist = {}
        for e in members:
            key = e.shift_label
            shift_dist[key] = shift_dist.get(key, 0) + 1
        teams_data.append({
            'obj': team,
            'members': members,
            'avg_fte': round(avg_fte, 1),
            'we_count': we_count,
            'shift_dist': shift_dist,
        })
    return render_template('teams_list.html', teams_data=teams_data)


@app.route('/teams/<int:team_id>')
def team_detail(team_id):
    team = Team.query.get_or_404(team_id)
    members = (Employee.query.filter_by(team=team.name)
               .order_by(Employee.status, Employee.name).all())
    shift_templates = ShiftTemplate.query.all()
    we_count = sum(1 for e in members if e.status == 'active'
                   and (e.works_saturday == 'yes' or e.works_sunday == 'yes'))
    return render_template('team_detail.html', team=team, members=members,
                           shift_templates=shift_templates, we_count=we_count)


# ────────────────────────────────────────────────────────────────────────────
# Documents
# ────────────────────────────────────────────────────────────────────────────

DOC_CATEGORIES = ['Contract', 'Correspondence', 'Reference Data', 'Miscellaneous']


def _doc_category_translations():
    """Never called — registers DOC_CATEGORIES for pybabel extraction, same
    reasoning as _month_name_translations() above. The stored Document.category
    value stays English (it's a DB value used for filtering); templates call
    _(cat) / _(doc.category) at render time to look up this same msgid."""
    return [_('Contract'), _('Correspondence'), _('Reference Data'), _('Miscellaneous')]


def _department_translations():
    """Never called — registers IdentityUser.DEPARTMENTS for pybabel extraction,
    same reasoning as _doc_category_translations() above."""
    return [_('Workforce Management'), _('Human Resources'), _('Executive'),
            _('Quality Management'), _('Learning & Development'), _('AI & Automation')]


@app.route('/documents')
def documents_list():
    docs = Document.query.order_by(Document.uploaded_at.desc()).all()
    return render_template('documents.html', docs=docs, categories=DOC_CATEGORIES)


@app.route('/documents/upload', methods=['POST'])
def documents_upload():
    file = request.files.get('file')
    category    = request.form.get('category', 'Other')
    description = request.form.get('description', '')

    if not file or not file.filename:
        flash(_('No file selected.'), 'danger')
        return redirect(url_for('documents_list'))

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    stored = f'doc_{datetime.now().strftime("%Y%m%d%H%M%S")}_{file.filename}'
    path   = os.path.join(docs_folder(), stored)
    file.save(path)
    size = os.path.getsize(path)

    from scheduler.doc_extract import extract_text
    extracted_text, extraction_error = extract_text(path, ext)

    db.session.add(Document(
        original_name=file.filename, stored_name=stored,
        file_type=ext, file_size=size,
        category=category, description=description,
        extracted_text=extracted_text, extraction_error=extraction_error,
    ))
    db.session.commit()
    flash(_('"%(name)s" uploaded successfully.', name=file.filename), 'success')
    return redirect(url_for('documents_list'))


@app.route('/documents/<int:doc_id>/download')
def documents_download(doc_id):
    doc = Document.query.get_or_404(doc_id)
    path = os.path.join(docs_folder(), doc.stored_name)
    return send_file(path, as_attachment=True, download_name=doc.original_name)


@app.route('/documents/<int:doc_id>/delete', methods=['POST'])
def documents_delete(doc_id):
    doc = Document.query.get_or_404(doc_id)
    path = os.path.join(docs_folder(), doc.stored_name)
    if os.path.exists(path):
        os.remove(path)
    db.session.delete(doc)
    db.session.commit()
    flash(_('"%(name)s" deleted.', name=doc.original_name), 'info')
    return redirect(url_for('documents_list'))


# ────────────────────────────────────────────────────────────────────────────
# AI Chat
# ────────────────────────────────────────────────────────────────────────────

def _get_active_provider():
    p = GlobalSetting.query.filter_by(key='ai_provider').first()
    return p.value if p else 'mistral'


@app.route('/ai')
def ai_chat():
    sessions = ChatSession.query.order_by(ChatSession.updated_at.desc()).all()
    return render_template('ai_chat.html', sessions=sessions,
                           active_provider=_get_active_provider())


@app.route('/ai/session/new', methods=['POST'])
def ai_session_new():
    s = ChatSession(title='New conversation')
    db.session.add(s)
    db.session.commit()
    return redirect(url_for('ai_session', session_id=s.id))


@app.route('/ai/session/<int:session_id>')
def ai_session(session_id):
    session  = ChatSession.query.get_or_404(session_id)
    sessions = ChatSession.query.order_by(ChatSession.updated_at.desc()).all()
    return render_template('ai_chat.html', sessions=sessions,
                           active_session=session,
                           active_provider=_get_active_provider())


@app.route('/ai/session/<int:session_id>/delete', methods=['POST'])
def ai_session_delete(session_id):
    s = ChatSession.query.get_or_404(session_id)
    db.session.delete(s)
    db.session.commit()
    return redirect(url_for('ai_chat'))


def _ai_tool_executor(name, args):
    """Executes a tool call requested by the AI assistant. Runs inside the
    active request's Flask/DB context (see chat_with_tools call site below)."""
    if name == 'list_documents':
        docs = Document.query.order_by(Document.uploaded_at.desc()).all()
        if not docs:
            return 'No documents have been uploaded for this project.'
        return json.dumps([{
            'id': d.id, 'name': d.original_name, 'category': d.category,
            'description': d.description or '', 'file_type': d.file_type,
            'readable': bool(d.extracted_text),
        } for d in docs])

    if name == 'read_document':
        doc = Document.query.get(args.get('document_id'))
        if not doc:
            return 'No document with that id.'
        if doc.extracted_text:
            return doc.extracted_text
        return f'Could not read this document: {doc.extraction_error or "no extracted text available"}'

    if name == 'read_import_log':
        limit = args.get('limit') or 20
        logs = ImportLog.query.order_by(ImportLog.created_at.desc()).limit(limit).all()
        if not logs:
            return 'No import log entries — no format issues have been detected on any upload.'
        return '\n'.join(
            f'[{l.created_at.strftime("%Y-%m-%d %H:%M")}] {l.level.upper()} '
            f'({l.source}, {l.filename}): {l.message}'
            for l in logs
        )

    if name == 'get_team_roster':
        team_name = args.get('team_name', '')
        members = (Employee.query.filter_by(team=team_name, status='active')
                   .order_by(Employee.name).all())
        if not members:
            return f'No active employees found on team "{team_name}".'
        lines = []
        for e in members:
            notes = f' | NOTE: {e.contract_notes[:80]}' if e.contract_notes else ''
            lines.append(
                f'{e.name}: {e.fte_percent}% FTE ({e.fte_mode}), shift={e.shift_label}, '
                f'saturday={e.works_saturday}, sunday={e.works_sunday}, '
                f'holidays={e.works_holidays}{notes}'
            )
        return '\n'.join(lines)

    if name == 'find_employee':
        query = args.get('name', '')
        matches = Employee.query.filter(Employee.name.ilike(f'%{query}%')).limit(10).all()
        if not matches:
            return f'No employee found matching "{query}".'
        lines = []
        for e in matches:
            notes = f' | NOTE: {e.contract_notes[:150]}' if e.contract_notes else ''
            lines.append(
                f'{e.name} ({e.team}, {e.status}): {e.fte_percent}% FTE ({e.fte_mode}), '
                f'shift={e.shift_label}, saturday={e.works_saturday}, sunday={e.works_sunday}, '
                f'holidays={e.works_holidays}{notes}'
            )
        return '\n'.join(lines)

    return f'Unknown tool: {name}'


@app.route('/ai/session/<int:session_id>/ask', methods=['POST'])
def ai_ask(session_id):
    """Streaming SSE endpoint — yields response chunks as text/event-stream."""
    from flask import Response, stream_with_context
    from scheduler.llm import chat_with_tools, build_workforce_context, TOOLS

    chat_session = ChatSession.query.get_or_404(session_id)
    user_text    = request.form.get('message', '').strip()
    provider     = _get_active_provider()

    if not user_text:
        return Response('data: {"error": "empty"}\n\n', mimetype='text/event-stream')

    # Save user message
    db.session.add(ChatMessage(session_id=session_id, role='user', content=user_text))

    # Auto-title the session after first message
    if len(chat_session.messages) == 0:
        chat_session.title = user_text[:60] + ('…' if len(user_text) > 60 else '')

    chat_session.updated_at = datetime.utcnow()
    db.session.commit()

    # Build conversation history (last 20 turns)
    history = chat_session.messages[-40:]  # 20 user + 20 assistant
    messages = [{'role': m.role, 'content': m.content} for m in history]

    system = build_workforce_context(project_config(g.active_project))
    # A fresh app_context() in the finally block below starts with an empty g,
    # so it doesn't know which project's database to write to — capture the
    # engine now, while the real request context still has it.
    active_engine = g.active_engine

    def generate():
        full_response = []
        try:
            for chunk in chat_with_tools(messages, system=system, provider=provider,
                                         tools=TOOLS, tool_executor=_ai_tool_executor):
                full_response.append(chunk)
                payload = json.dumps({'text': chunk})
                yield f'data: {payload}\n\n'
        except Exception as e:
            err = json.dumps({'text': f'\n\n⚠️ Error: {e}'})
            yield f'data: {err}\n\n'
        finally:
            # Persist the complete assistant reply
            complete = ''.join(full_response)
            with app.app_context():
                g.active_engine = active_engine
                db.session.add(ChatMessage(
                    session_id=session_id, role='assistant',
                    content=complete, provider=provider,
                ))
                db.session.commit()
            yield 'data: [DONE]\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


if __name__ == '__main__':
    app.run(debug=True, port=5000)
