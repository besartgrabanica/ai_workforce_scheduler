"""
Uses Claude API (claude-opus-4-8) to parse multilingual (Albanian/German/English)
employee notes into structured scheduling constraints.
"""
import json
import os

import anthropic

_SYSTEM = """\
You are a workforce scheduling assistant for a Kosovo-based call centre that serves the German energy client E.ON.
Your job is to read employee notes written in Albanian, German, or English and extract structured scheduling constraints.

Albanian glossary:
  paradite / vetem paradite = morning only
  masdite / masdite only    = afternoon only
  full time                  = full day
  shkaku i udhetimit         = due to travel (they need a fixed window because of commute)
  vetem                      = only
  prej [month]               = starting from [month]
  mos me planifiku           = do not schedule
  cdo                        = every
  e hene/e marte/e merkure/e enjte/e premte/e shtune/e diele =
        Monday/Tuesday/Wednesday/Thursday/Friday/Saturday/Sunday

For EACH employee return exactly one JSON object with these keys:
  shift_type    : "morning" | "afternoon" | "full" | "custom"
  custom_start  : "HH:MM" or null
  custom_end    : "HH:MM" or null
  day_restrictions : array of {day_of_week: 0-6 (0=Mon), shift_type: "morning"|"afternoon"|"full"|"off"|null}
  excluded_dates   : array of "YYYY-MM-DD" strings
  same_schedule_as : string (employee name) or null
  notes            : one-sentence English summary of the key constraint

Rules:
- If hours like "08:00-16:30" appear → shift_type="custom", parse start/end
- If "paradite" without hours → shift_type="morning", custom_start=null, custom_end=null
- If "full time" or no restriction → shift_type="full"
- Dates like "06.06.2026" → excluded_dates ["2026-06-06"]
- "Prej Qershorit full time" = from June onwards, full time (no excluded dates, just note it)
- "orarin e njejte me X" = same_schedule_as = X
- "mos me planifiku [e hene/e marte/...]" (recurring, no specific date) or any statement that the
  employee never works a given day of the week → day_restrictions entry for that day with
  shift_type="off". Use "off" only for recurring weekly patterns; use excluded_dates for one-off dates.
- Ticket references are informational only; extract any time/day constraint if stated
- Return ONLY a valid JSON array — no markdown, no extra text.
"""


def parse_employee_notes(employees_data: list[dict], api_key: str | None = None) -> list[dict]:
    """
    employees_data: list of {'name': str, 'team': str, 'raw_notes': str}

    Returns a list (same length, same order) of constraint dicts.
    If the API call fails, returns safe defaults for every employee.
    """
    key = api_key or os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return [_default_constraints() for _ in employees_data]

    numbered = '\n'.join(
        f'{i + 1}. [{e["team"]}] {e["name"]}: "{e["raw_notes"]}"'
        for i, e in enumerate(employees_data)
    )

    client = anthropic.Anthropic(api_key=key)
    try:
        msg = client.messages.create(
            model='claude-opus-4-8',
            max_tokens=8000,
            system=_SYSTEM,
            messages=[{'role': 'user', 'content': f'Parse these {len(employees_data)} employees:\n\n{numbered}'}],
        )
        raw = msg.content[0].text.strip()
        # Strip accidental markdown fences
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
        parsed = json.loads(raw)
        if len(parsed) != len(employees_data):
            raise ValueError('Response length mismatch')
        return parsed
    except Exception as exc:
        print(f'[ai_parser] Claude parse failed: {exc} — using defaults')
        return [_default_constraints() for _ in employees_data]


def _default_constraints() -> dict:
    return {
        'shift_type': 'full',
        'custom_start': None,
        'custom_end': None,
        'day_restrictions': [],
        'excluded_dates': [],
        'same_schedule_as': None,
        'notes': '',
    }
