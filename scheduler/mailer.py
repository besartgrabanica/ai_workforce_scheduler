"""
Small wrapper around smtplib for password-reset emails — ported from the
KiKxxl-evroTarget orgchart service's mailer.js, same config/behavior.

Configure via environment variables (.env):
  SMTP_HOST     e.g. smtp.office365.com / smtp.gmail.com
  SMTP_PORT     e.g. 587
  SMTP_USER     e.g. scheduler@kikxxl-evrotarget.com
  SMTP_PASS     the SMTP / app password
  MAIL_FROM     e.g. "KiKxxl-evroTarget Workforce Scheduler <scheduler@kikxxl-evrotarget.com>"
  APP_BASE_URL  e.g. https://scheduler.kikxxl-evrotarget.com  (used in reset links)

If SMTP_HOST is not set, the mailer runs in DRY-RUN mode: emails are logged to
the console instead of being sent. Useful for local development.
"""
import os
import re
import smtplib
from email.message import EmailMessage

SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
MAIL_FROM = os.environ.get('MAIL_FROM') or (
    f'KiKxxl-evroTarget Workforce Scheduler <{SMTP_USER}>' if SMTP_USER else ''
)
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://localhost:5050').rstrip('/')

DRY_RUN = not SMTP_HOST


def _html_escape(s: str) -> str:
    return (str(s or '')
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


def _wrap_html(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>{_html_escape(title)}</title></head>
<body style="margin:0;padding:0;background:#f0f4ff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:520px;background:#ffffff;border-radius:14px;box-shadow:0 4px 20px rgba(29,78,216,.08);overflow:hidden;">
        <tr><td style="padding:24px 28px 0;">
          <div style="display:inline-block;background:#1d4ed8;color:#ffffff;font-size:11px;font-weight:700;letter-spacing:1px;padding:5px 10px;border-radius:5px;text-transform:uppercase;">KiKxxl-evroTarget</div>
        </td></tr>
        <tr><td style="padding:18px 28px 28px;color:#111827;font-size:15px;line-height:1.6;">
          {body_html}
        </td></tr>
        <tr><td style="padding:16px 28px 24px;border-top:1px solid #e5e7eb;color:#9ca3af;font-size:12px;">
          You received this because someone with admin access at KiKxxl-evroTarget initiated this action. If this wasn't expected, you can safely ignore this email.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def send_mail(to: str, subject: str, html: str, text: str | None = None):
    if DRY_RUN:
        print('\n  \U0001F4E7  [DRY-RUN] would send mail:')
        print(f'       To:      {to}')
        print(f'       Subject: {subject}')
        print('       (set SMTP_HOST + creds in .env to send for real)\n')
        return {'dry_run': True}

    msg = EmailMessage()
    msg['From'] = MAIL_FROM
    msg['To'] = to
    msg['Subject'] = subject
    msg.set_content(text or re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', html)).strip())
    msg.add_alternative(html, subtype='html')

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    return {'dry_run': False}


def send_password_reset_email(to: str, token: str, username: str):
    link = f'{APP_BASE_URL}/reset-password?token={token}'
    html = _wrap_html('Reset your KiKxxl-evroTarget password', f"""
    <h2 style="margin:0 0 12px;font-size:18px;color:#111827;">Reset your password</h2>
    <p>Someone (hopefully you) requested a password reset for the account <strong>{_html_escape(username)}</strong> on the KiKxxl-evroTarget Workforce Scheduler.</p>
    <p>Click the button below within the next hour to choose a new password.</p>
    <p style="margin:22px 0 12px;"><a href="{link}" style="display:inline-block;background:#1d4ed8;color:#ffffff;text-decoration:none;font-weight:600;padding:11px 22px;border-radius:8px;">Reset password</a></p>
    <p style="font-size:13px;color:#6b7280;">Or paste this link into your browser:<br/><span style="word-break:break-all;">{link}</span></p>
    <p style="font-size:13px;color:#6b7280;margin-top:18px;">If you didn't request a reset, ignore this email — your password won't change.</p>
    <p style="font-size:13px;color:#6b7280;">This link expires in <strong>1 hour</strong>.</p>
    """)
    return send_mail(to, 'Reset your KiKxxl-evroTarget password', html)


def send_invite_email(to: str, role: str, token: str, invited_by_name: str, project_label: str | None = None):
    link = f'{APP_BASE_URL}/accept-invite?token={token}'
    scope_line = (f'for <strong>{_html_escape(project_label)}</strong> ' if project_label
                  else 'with access to <strong>every project</strong> ')
    html = _wrap_html("You're invited to the KiKxxl-evroTarget Workforce Scheduler", f"""
    <h2 style="margin:0 0 12px;font-size:18px;color:#111827;">You're invited</h2>
    <p>{_html_escape(invited_by_name or 'An admin')} has invited you to join the <strong>KiKxxl-evroTarget Workforce Scheduler</strong> {scope_line}as a <strong>{_html_escape(role)}</strong>.</p>
    <p>Click the button below to choose a username and password and activate your account.</p>
    <p style="margin:22px 0 12px;"><a href="{link}" style="display:inline-block;background:#1d4ed8;color:#ffffff;text-decoration:none;font-weight:600;padding:11px 22px;border-radius:8px;">Accept invite</a></p>
    <p style="font-size:13px;color:#6b7280;">Or paste this link into your browser:<br/><span style="word-break:break-all;">{link}</span></p>
    <p style="font-size:13px;color:#6b7280;margin-top:18px;">This invite expires in <strong>30 days</strong>.</p>
    """)
    return send_mail(to, 'You are invited to the KiKxxl-evroTarget Workforce Scheduler', html)
