from datetime import datetime

import sqlalchemy as sa
from flask import g
from flask_sqlalchemy import SQLAlchemy
from flask_sqlalchemy.session import Session as _FSASession
from flask_sqlalchemy.session import _clause_to_engine


class ProjectScopedSession(_FSASession):
    """Routes business-data queries (no __bind_key__) to whichever project's
    engine is active for the current request (g.active_engine, set in
    app.py's before_request hook), while leaving explicitly bind-keyed models
    (the identity_user/project_role tables, __bind_key__='identity') on their
    own fixed database regardless of the active project.

    Verified empirically against real data with a 20-request concurrency
    test before being relied on here — see the plan file for the full
    investigation. g is genuinely request-scoped, unlike db.engines (a
    single dict shared by the whole running app), which is why this reads
    from g rather than mutating engines directly.
    """

    def get_bind(self, mapper=None, clause=None, bind=None, **kwargs):
        if bind is not None:
            return bind

        engines = self._db.engines
        resolved = None
        if mapper is not None:
            resolved = _clause_to_engine(sa.inspect(mapper).local_table, engines)
        elif clause is not None:
            resolved = _clause_to_engine(clause, engines)

        active = getattr(g, 'active_engine', None)
        # A resolved bind of None, or exactly the static default engine, means
        # this model has no explicit __bind_key__ (i.e. it's business data) —
        # substitute the active project's engine for those. Anything else
        # (e.g. the identity bind) resolved to something explicit — leave it.
        if active is not None and (resolved is None or resolved is engines.get(None)):
            return active
        if resolved is not None:
            return resolved
        if None in engines:
            return engines[None]
        return super().get_bind(mapper=mapper, clause=clause, bind=bind, **kwargs)


db = SQLAlchemy(session_options={'class_': ProjectScopedSession})


class Team(db.Model):
    __tablename__ = 'team'
    id   = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

    @property
    def member_count(self):
        # lazy import avoids forward-reference issue; query runs at runtime
        from sqlalchemy import text
        return db.session.execute(
            text("SELECT COUNT(*) FROM employee WHERE team=:t AND status='active'"),
            {'t': self.name}
        ).scalar() or 0

    def __repr__(self):
        return f'<Team {self.name}>'


class ShiftTemplate(db.Model):
    __tablename__ = 'shift_template'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    start_time = db.Column(db.String(5), nullable=False)   # "HH:MM"
    end_time = db.Column(db.String(5), nullable=False)     # "HH:MM"
    hours = db.Column(db.Float, nullable=False)
    color = db.Column(db.String(7), default='#6366f1')     # hex color for UI
    is_default = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'start_time': self.start_time, 'end_time': self.end_time,
            'hours': self.hours, 'color': self.color,
        }


class BusinessParam(db.Model):
    __tablename__ = 'business_param'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(200), nullable=False)
    label = db.Column(db.String(100))
    category = db.Column(db.String(50))


class Employee(db.Model):
    __tablename__ = 'employee'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    team = db.Column(db.String(50))
    fte_percent = db.Column(db.Integer, default=100)   # 5–100 in steps of 5
    fte_mode = db.Column(db.String(20), default='days')  # 'days', 'hours', 'combined'
    shift_template_id = db.Column(db.Integer, db.ForeignKey('shift_template.id'))
    custom_start = db.Column(db.String(5))   # "HH:MM" override
    custom_end = db.Column(db.String(5))     # "HH:MM" override
    custom_hours = db.Column(db.Float)       # actual net worked hours for custom_start/end,
                                              # distinct from the raw clock span (e.g. accounts
                                              # for an unpaid break); falls back to the raw
                                              # span when unset
    works_weekends = db.Column(db.String(10), default='no')   # legacy, superseded by works_saturday
    works_saturday = db.Column(db.String(10), default='no')   # 'yes', 'no'
    works_sunday = db.Column(db.String(10), default='no')     # 'yes', 'no'
    works_holidays = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(20), default='active')   # 'active', 'inactive', 'trainee'
    raw_notes = db.Column(db.Text)
    contract_notes = db.Column(db.Text)
    schedule_group_id = db.Column(db.Integer, db.ForeignKey('schedule_group.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    shift_template = db.relationship('ShiftTemplate', backref='employees')
    day_restrictions = db.relationship('DayRestriction', backref='employee', cascade='all, delete-orphan')
    excluded_dates = db.relationship('ExcludedDate', backref='employee', cascade='all, delete-orphan')
    schedule_group = db.relationship('ScheduleGroup', backref='members')

    @property
    def effective_shift_template(self):
        """The template that actually governs this employee's shift: their own
        assignment if set, otherwise whichever ShiftTemplate is the project default."""
        return self.shift_template or ShiftTemplate.query.filter_by(is_default=True).first()

    @property
    def effective_shift_start(self):
        if self.custom_start:
            return self.custom_start
        tpl = self.effective_shift_template
        return tpl.start_time if tpl else '08:00'

    @property
    def effective_shift_end(self):
        if self.custom_end:
            return self.custom_end
        tpl = self.effective_shift_template
        return tpl.end_time if tpl else '16:30'

    @property
    def effective_hours(self):
        if self.custom_start and self.custom_end:
            if self.custom_hours is not None:
                return self.custom_hours
            sh, sm = map(int, self.custom_start.split(':'))
            eh, em = map(int, self.custom_end.split(':'))
            return (eh * 60 + em - sh * 60 - sm) / 60
        tpl = self.effective_shift_template
        return tpl.hours if tpl else 7.5

    @property
    def shift_label(self):
        if self.custom_start and self.custom_end:
            return f'{self.custom_start}–{self.custom_end}'
        tpl = self.effective_shift_template
        return tpl.name if tpl else '—'

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'team': self.team,
            'fte_percent': self.fte_percent, 'fte_mode': self.fte_mode,
            'shift_label': self.shift_label,
            'works_weekends': self.works_weekends,
            'works_holidays': self.works_holidays,
            'status': self.status,
        }


class DayRestriction(db.Model):
    __tablename__ = 'day_restriction'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id', ondelete='CASCADE'), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)   # 0=Mon … 6=Sun
    shift_type = db.Column(db.String(20))                 # 'morning'/'afternoon'/None
    is_off = db.Column(db.Boolean, default=False)          # True = not scheduled that day-of-week

    DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    @property
    def day_name(self):
        return self.DAY_NAMES[self.day_of_week]


class ScheduleGroup(db.Model):
    __tablename__ = 'schedule_group'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

    @property
    def member_count(self):
        return len(self.members)


class ExcludedDate(db.Model):
    __tablename__ = 'excluded_date'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id', ondelete='CASCADE'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.String(200))


class ForecastPeriod(db.Model):
    __tablename__ = 'forecast_period'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    daily_forecasts = db.relationship('DailyForecast', backref='period', cascade='all, delete-orphan',
                                      order_by='DailyForecast.date')
    schedules = db.relationship('Schedule', backref='period')


class DailyForecast(db.Model):
    __tablename__ = 'daily_forecast'
    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey('forecast_period.id', ondelete='CASCADE'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    day_of_week = db.Column(db.String(3))    # 'Mon', 'Tue', …
    calendar_week = db.Column(db.Integer)
    total_sync = db.Column(db.Float, default=0)
    total_async = db.Column(db.Float, default=0)
    total_chat = db.Column(db.Float, default=0)
    germany_contribution = db.Column(db.Float, default=0)
    required_ks_agents = db.Column(db.Integer, default=0)

    half_hourly = db.relationship('HalfHourlyForecast', backref='daily',
                                  cascade='all, delete-orphan', order_by='HalfHourlyForecast.slot_time')

    @property
    def total_contacts(self):
        return self.total_sync + self.total_async + self.total_chat

    @property
    def ks_contacts(self):
        return max(0, self.total_contacts - self.germany_contribution)


class HalfHourlyForecast(db.Model):
    __tablename__ = 'half_hourly_forecast'
    id = db.Column(db.Integer, primary_key=True)
    daily_id = db.Column(db.Integer, db.ForeignKey('daily_forecast.id', ondelete='CASCADE'), nullable=False)
    slot_time = db.Column(db.String(5), nullable=False)   # "HH:MM"
    sync_volume = db.Column(db.Float, default=0)
    async_volume = db.Column(db.Float, default=0)
    chat_volume = db.Column(db.Float, default=0)

    @property
    def total(self):
        return self.sync_volume + self.async_volume + self.chat_volume


class Schedule(db.Model):
    __tablename__ = 'schedule'
    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey('forecast_period.id'))
    name = db.Column(db.String(100), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)

    assignments = db.relationship('ShiftAssignment', backref='schedule',
                                  cascade='all, delete-orphan')


class ShiftAssignment(db.Model):
    __tablename__ = 'shift_assignment'
    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey('schedule.id', ondelete='CASCADE'), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    # 'work', 'day_off', 'weekend_off', 'holiday_off', 'constraint_off'
    status = db.Column(db.String(20), nullable=False)
    shift_start = db.Column(db.String(5))
    shift_end = db.Column(db.String(5))
    hours_worked = db.Column(db.Float, default=0)
    shift_template_id = db.Column(db.Integer, db.ForeignKey('shift_template.id'))
    is_manual = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text)

    employee = db.relationship('Employee', backref='assignments')
    shift_template = db.relationship('ShiftTemplate')

    @property
    def display_code(self):
        if self.status == 'work':
            return f'{self.shift_start}–{self.shift_end}'
        codes = {
            'day_off': 'OFF',
            'weekend_off': 'WE',
            'holiday_off': 'H',
            'constraint_off': '—',
        }
        return codes.get(self.status, '?')

    @property
    def cell_color(self):
        if self.status == 'work' and self.shift_template:
            return self.shift_template.color
        palette = {
            'work': '#22c55e',
            'day_off': '#94a3b8',
            'weekend_off': '#cbd5e1',
            'holiday_off': '#f87171',
            'constraint_off': '#e2e8f0',
        }
        return palette.get(self.status, '#e2e8f0')


class Document(db.Model):
    __tablename__ = 'document'
    id            = db.Column(db.Integer, primary_key=True)
    original_name = db.Column(db.String(200), nullable=False)
    stored_name   = db.Column(db.String(200), nullable=False)   # filename on disk
    file_type     = db.Column(db.String(10))                    # 'xlsx', 'pdf', etc.
    file_size     = db.Column(db.Integer, default=0)            # bytes
    category      = db.Column(db.String(30), default='Other')   # Forecast/Schedule/HR/Other
    description   = db.Column(db.Text)
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)
    extracted_text   = db.Column(db.Text)   # plain-text content, for the AI assistant to read on demand
    extraction_error = db.Column(db.String(200))  # set if extraction wasn't possible (e.g. unsupported type)


class ImportLog(db.Model):
    """Records format/structure mismatches found while parsing an uploaded
    Excel file — e.g. an expected header row or column wasn't where the parser
    expects it. Surfaced to the user as a flash warning at upload time, and
    readable by the AI assistant so 'why didn't my import look right' can be
    answered from history, not just the moment it happened."""
    __tablename__ = 'import_log'
    id         = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    source     = db.Column(db.String(50))    # 'tfc_forecast' | 'abnahme_de' | 'forecast_calc' | 'employee_spec'
    filename   = db.Column(db.String(200))
    level      = db.Column(db.String(10), default='warning')  # 'warning' | 'error'
    message    = db.Column(db.Text, nullable=False)


class ChatSession(db.Model):
    __tablename__ = 'chat_session'
    id         = db.Column(db.Integer, primary_key=True)
    title      = db.Column(db.String(200), default='New conversation')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    messages = db.relationship('ChatMessage', backref='session',
                               cascade='all, delete-orphan', order_by='ChatMessage.id')


class ChatMessage(db.Model):
    __tablename__ = 'chat_message'
    id         = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_session.id', ondelete='CASCADE'), nullable=False)
    role       = db.Column(db.String(10), nullable=False)   # 'user' | 'assistant'
    content    = db.Column(db.Text, nullable=False)
    provider   = db.Column(db.String(20))                   # which LLM answered
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class User(db.Model):
    __tablename__ = 'app_user'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role          = db.Column(db.String(20), nullable=False, default='user')
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    ROLES = ['viewer', 'user', 'admin', 'dev', 'superadmin']

    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)

    def has_role(self, min_role):
        levels = {'viewer': 1, 'user': 2, 'admin': 3, 'dev': 4, 'superadmin': 5}
        return levels.get(self.role, 0) >= levels.get(min_role, 99)


# ── Identity: shared across all projects, on its own fixed database ─────────
# (__bind_key__='identity' — see ProjectScopedSession above). This is the
# source of truth for login going forward; the legacy per-project `User`
# table above is left in place, unused, rather than dropped.

class IdentityUser(db.Model):
    """One role scale, two ways to hold it:
      - global_role: applies to every project automatically, present and future.
        Only 'superadmin' at this level additionally unlocks technical/system
        settings (AI provider config etc.) — see is_technical_admin.
      - ProjectRole rows: applies to one specific granted project, ceiling 'admin'
        (there's no per-project 'superadmin' — that's what the global tier is for).
    Effective role for a project = the higher of the two. Only a true global
    superadmin can grant/change anyone's global_role; a per-project 'admin' can
    manage people within their own project (see app.py's require_role /
    require_global_role and the users_* routes)."""
    __bind_key__ = 'identity'
    __tablename__ = 'identity_user'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    email         = db.Column(db.String(200), unique=True)   # required going forward (invite-only signup); nullable=True only so existing/legacy rows aren't broken
    password_hash = db.Column(db.String(200), nullable=False)
    is_active     = db.Column(db.Boolean, default=True)
    global_role   = db.Column(db.String(20))   # None, or one of ROLES — applies to every project
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    ui_theme      = db.Column(db.String(20), default='light')   # 'light' | 'navy-canvas' | 'dark' — this user's chosen app appearance
    ui_locale     = db.Column(db.String(10), default='en')      # 'en' | 'de' | 'sq' — this user's chosen UI language
    first_name    = db.Column(db.String(80))
    last_name     = db.Column(db.String(80))
    position      = db.Column(db.String(100))   # free-text job title, e.g. "Workforce Planner"
    avatar_filename = db.Column(db.String(200))  # stored under uploads/_identity/avatars/
    # Organizational affiliation — deliberately separate from actual system
    # access (ProjectRole/global_role): someone can be "HR department" while
    # still holding, say, viewer access to a project. One or the other, not both.
    work_context_type  = db.Column(db.String(20))    # None | 'project' | 'department'
    work_context_value = db.Column(db.String(100))   # project key, or one of DEPARTMENTS

    DEPARTMENTS = [
        'Workforce Management', 'Human Resources', 'Executive',
        'Quality Management', 'Learning & Development', 'AI & Automation',
    ]

    ROLES = ['viewer', 'editor', 'admin', 'superadmin']
    _ROLE_LEVELS = {'viewer': 1, 'editor': 2, 'admin': 3, 'superadmin': 4}
    UI_THEMES = ['light', 'navy-canvas', 'dark']
    UI_LOCALES = ['en', 'de', 'sq']

    project_roles = db.relationship('ProjectRole', backref='user', cascade='all, delete-orphan')

    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)

    @property
    def display_name(self):
        full = f'{self.first_name or ""} {self.last_name or ""}'.strip()
        return full or self.username

    @property
    def is_technical_admin(self):
        """True only for a genuine global superadmin — gates AI/system config,
        which is global and can never be reached via a per-project grant."""
        return self.global_role == 'superadmin'

    def role_for(self, project):
        """Effective role for a given project key (the higher of global_role and
        any project-specific grant), or None if this identity has no access at all."""
        global_level = self._ROLE_LEVELS.get(self.global_role, 0)
        project_role = next((pr.role for pr in self.project_roles if pr.project == project), None)
        project_level = self._ROLE_LEVELS.get(project_role, 0)
        best = max(global_level, project_level)
        if best == 0:
            return None
        return next(name for name, level in self._ROLE_LEVELS.items() if level == best)

    def accessible_projects(self, known_projects):
        """Project keys this identity can access, restricted to currently-known/discovered
        projects — a stale ProjectRole row pointing at a since-removed project grants nothing."""
        if self.global_role:
            return list(known_projects)
        return [pr.project for pr in self.project_roles if pr.project in known_projects]

    def has_role(self, project, min_role):
        role = self.role_for(project)
        return self._ROLE_LEVELS.get(role, 0) >= self._ROLE_LEVELS.get(min_role, 99)


class ProjectRole(db.Model):
    __bind_key__ = 'identity'
    __tablename__ = 'project_role'
    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('identity_user.id', ondelete='CASCADE'), nullable=False)
    project = db.Column(db.String(50), nullable=False)
    role    = db.Column(db.String(20), nullable=False, default='editor')

    __table_args__ = (db.UniqueConstraint('user_id', 'project', name='uq_project_role_user_project'),)


class GlobalSetting(db.Model):
    """AI provider/model configuration — genuinely global (one setting shared by
    every project), editable only by a global superadmin (IdentityUser.is_technical_admin).
    Lives on the identity bind alongside IdentityUser/ProjectRole, not per-project."""
    __bind_key__ = 'identity'
    __tablename__ = 'global_setting'
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(200), nullable=False)
    label = db.Column(db.String(100))


class ProjectMeta(db.Model):
    """A client engagement created via the Projects admin page (as opposed to
    the handful hardcoded in app.py's PROJECTS dict from before this existed).
    Lives on the identity bind — the project registry itself is cross-project,
    global data, same reasoning as GlobalSetting. Creating a row here plus
    provisioning its database (done by the /projects route) is everything
    needed for a new project to exist — no restart required."""
    __bind_key__ = 'identity'
    __tablename__ = 'project_meta'
    id             = db.Column(db.Integer, primary_key=True)
    key            = db.Column(db.String(50), unique=True, nullable=False)
    display_name   = db.Column(db.String(100), nullable=False)
    company        = db.Column(db.String(100), default='KiKxxl-evroTarget')
    client         = db.Column(db.String(200))
    site           = db.Column(db.String(100))
    param_category = db.Column(db.String(50))
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)


class PasswordReset(db.Model):
    """A "forgot password" token — only its SHA-256 hash is stored, never the raw
    token (that only ever exists in the emailed link and the brief request that
    consumes it). Single-use (used_at) and short-lived (expires_at, 1 hour)."""
    __bind_key__ = 'identity'
    __tablename__ = 'password_reset'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('identity_user.id', ondelete='CASCADE'), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at    = db.Column(db.DateTime)
    request_ip = db.Column(db.String(45))

    user = db.relationship('IdentityUser')


class Invitation(db.Model):
    """The only way a brand-new account gets created — an admin specifies an
    email + role (project-scoped, or global if invited by a technical admin),
    which emails a token-bearing link; the recipient picks their own username
    and password to activate. Mirrors orgchart's invitations table/flow.
    Only the token hash is stored, same as PasswordReset."""
    __bind_key__ = 'identity'
    __tablename__ = 'invitation'
    id          = db.Column(db.Integer, primary_key=True)
    email       = db.Column(db.String(200), nullable=False)
    project     = db.Column(db.String(50))   # None = global-scope invite
    role        = db.Column(db.String(20), nullable=False)
    token_hash  = db.Column(db.String(64), unique=True, nullable=False)
    invited_by  = db.Column(db.Integer, db.ForeignKey('identity_user.id'))
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at  = db.Column(db.DateTime, nullable=False)
    accepted_at = db.Column(db.DateTime)
    accepted_by = db.Column(db.Integer, db.ForeignKey('identity_user.id'))
    revoked_at  = db.Column(db.DateTime)

    inviter = db.relationship('IdentityUser', foreign_keys=[invited_by])

    @property
    def status(self):
        if self.accepted_at:
            return 'accepted'
        if self.revoked_at:
            return 'revoked'
        if self.expires_at < datetime.utcnow():
            return 'expired'
        return 'pending'
