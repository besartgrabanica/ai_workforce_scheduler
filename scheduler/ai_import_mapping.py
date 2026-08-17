"""
Tier 3 of the 3-tier import resolution
(docs/specs/2026-08-import-mapping-detection.md): proposes a column/cell
mapping via the Claude API when Tier 1 (deterministic) and Tier 2 (cached
ImportMapping) both miss on a file's layout. Follows scheduler/ai_parser.py's
calling convention exactly — hardcoded model, JSON-only output, safe default
(None) on any failure — per CLAUDE.md's working agreement: "any new
AI-assisted feature should look like a sibling function to this file, not a
new calling style."

A proposal from this module is NEVER auto-applied. Nothing downstream (a
saved ImportMapping, a scheduling run) consumes it until a human admin
confirms or edits it through a later slice's UI — see resolve_import() in
scheduler/import_mapping.py, which only ever surfaces a proposal for
review, never derives parsed data from it.
"""
import json
import os

import anthropic

_TFC_SYSTEM = """\
You are helping onboard a reformatted E.ON TFC forecast Excel file for a workforce \
scheduling app. The file has a sheet named 'KiKxxl' with a "Gesamt" (grand total) \
section that used to start at a fixed column, identified by a "Gesamt" label in row 7 \
(0-based row index 6). The client has reformatted the sheet and that label can no \
longer be found by exact text match.

You are given the non-empty cells of the sheet's first several rows, as \
{"row": r, "col": c, "value": v} entries (0-based row/col indices, matching this \
app's own parser indexing).

Find the 0-based column index where the "Gesamt" (grand total) section starts. That \
column and the following ~75 columns hold, per data row: [+0]=Synchron daily total, \
[+1..+24]=30-min sync slots, [+25]=Asynchron daily total, [+26..+49]=async slots, \
[+50]=Chat daily total, [+51..+74]=chat slots, [+75]=grand total — so data rows should \
have numeric values starting at and after this column. A "Gesamt"-like label \
(possibly renamed or translated) anywhere in the header rows is the strongest signal \
if still present.

Return ONLY a JSON object: {"gesamt_col": <int or null>, "confidence": <0.0-1.0>, \
"rationale": "<one sentence>"}. If you cannot identify the column with reasonable \
confidence, return gesamt_col: null and explain why in rationale — never guess.
"""

_ABNAHME_SYSTEM = """\
You are helping onboard a reformatted "Abnahmemenge DE" (German offices daily \
contribution) Excel file for a workforce scheduling app. The file used to have a \
'Datum' header somewhere marking the date column, with the daily contribution value \
in the last non-empty column of that same header row. The client has reformatted the \
sheet and 'Datum' can no longer be found by exact text match.

You are given the non-empty cells of the sheet's first several rows, as \
{"row": r, "col": c, "value": v} entries (0-based row/col indices, matching this \
app's own parser indexing).

Find the 0-based column index holding dates, and the 0-based column index holding the \
daily contribution number (a plain float/int — most likely the rightmost populated \
column in the same rows as the dates).

Return ONLY a JSON object: {"date_col": <int or null>, "value_col": <int or null>, \
"confidence": <0.0-1.0>, "rationale": "<one sentence>"}. If you cannot identify both \
columns with reasonable confidence, return null for whichever is uncertain and explain \
why in rationale — never guess.
"""

_SYSTEMS = {
    'tfc_forecast': _TFC_SYSTEM,
    'abnahme_de': _ABNAHME_SYSTEM,
}


def propose_mapping(file_type, snapshot, api_key=None):
    """
    file_type: 'tfc_forecast' | 'abnahme_de'
    snapshot: list of {'row': int, 'col': int, 'value': ...} dicts — the
    non-empty cells from the file's early rows (see
    scheduler.import_mapping.build_layout_snapshot), JSON-serializable.

    Returns {'mapping': {...}, 'confidence': float, 'rationale': str} where
    'mapping' is shaped exactly like the mapping= override
    parse_tfc_file/parse_abnahme_de_file already accept, or None if the API
    call failed, isn't configured, or the model couldn't propose one with
    confidence — callers must treat None as "no proposal available" and
    never guess a mapping themselves.
    """
    system = _SYSTEMS.get(file_type)
    if system is None:
        return None

    key = api_key or os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return None

    client = anthropic.Anthropic(api_key=key)
    try:
        msg = client.messages.create(
            model='claude-opus-4-8',
            max_tokens=1000,
            system=system,
            messages=[{'role': 'user', 'content': json.dumps(snapshot)}],
        )
        raw = msg.content[0].text.strip()
        # Strip accidental markdown fences
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
        parsed = json.loads(raw)
    except Exception as exc:
        print(f'[ai_import_mapping] Claude proposal failed: {exc} — no proposal')
        return None

    confidence = parsed.get('confidence')
    rationale = parsed.get('rationale', '')

    if file_type == 'tfc_forecast':
        col = parsed.get('gesamt_col')
        if col is None:
            return None
        mapping = {'gesamt_col': int(col)}
    else:  # abnahme_de
        date_col, value_col = parsed.get('date_col'), parsed.get('value_col')
        if date_col is None or value_col is None:
            return None
        mapping = {'date_col': int(date_col), 'value_col': int(value_col)}

    return {'mapping': mapping, 'confidence': confidence, 'rationale': rationale}
