"""
Tier 2 of the 3-tier import resolution
(docs/specs/2026-08-import-mapping-detection.md): applies a previously
confirmed ImportMapping when Tier 1 (the existing deterministic parser in
scheduler/parsers/<project>.py) reports a layout warning, before falling
back to Tier 1's own existing behavior. Tier 3 (AI-assisted proposal) and
the confirm/edit UI that creates ImportMapping rows are later slices — this
module only wires the lookup, so until that UI exists the cache is always
empty and every import resolves via Tier 1 exactly as it did before.
"""
import hashlib
import json

import openpyxl

from scheduler.models import ImportMapping


def compute_layout_fingerprint(filepath):
    """A stable hash of a workbook's structural shape: sheet names plus every
    *string*-valued cell in each sheet's first 8 rows. Both file types this
    feature covers keep their structural header labels within that range and
    their real per-period data below it or in numeric/date cells within it —
    e.g. eon.py's TFC parser only ever scans row 7 for the 'Gesamt' label,
    with actual daily figures starting row 9; its Abnahmemenge parser scans
    for a 'Datum' text header, with dates/numbers in the data cells beside
    it. Restricting to strings in the top rows means a new period's numbers
    (and even a first data row's own text, like a month/weekday label) never
    perturb the fingerprint — only the labels/positions that define the
    layout do. A client moving/renaming/reordering those labels produces a
    different fingerprint, which is exactly the invalidation this feature
    needs — see ImportMapping's (file_type, layout_fingerprint) uniqueness."""
    wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
    parts = []
    for name in wb.sheetnames:
        parts.append(f'SHEET:{name}')
        ws = wb[name]
        for row in ws.iter_rows(min_row=1, max_row=8, values_only=True):
            for v in row:
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
    return hashlib.sha256('\x1f'.join(parts).encode('utf-8')).hexdigest()


def resolve_cached_mapping(file_type, filepath):
    """Tier 2 lookup. Project scoping is implicit — ImportMapping lives in
    the active project-scoped DB, so this can only ever match a mapping
    confirmed under the currently active project. Returns the ImportMapping
    row, or None on a cache miss (no mapping yet, or the fingerprint no
    longer matches a reformatted file)."""
    fingerprint = compute_layout_fingerprint(filepath)
    return ImportMapping.query.filter_by(
        file_type=file_type, layout_fingerprint=fingerprint,
    ).first()


def resolve_import(file_type, filepath, parse_fn):
    """Runs Tier 1 (parse_fn, unchanged — acceptance criterion 1) and, only
    if it reports a warning (a Tier 1 miss), looks up Tier 2. On a Tier 2
    hit, re-parses with the confirmed mapping applied and trusts that result
    instead. A Tier 2 hit that still produces warnings (the cached mapping
    no longer resolves this file cleanly either) is not trusted — this
    falls back to the plain Tier 1 result rather than risking a silent
    misparse from a stale cache entry.

    parse_fn is a project parser's parse_tfc_file / parse_abnahme_de_file —
    both accept an optional `mapping=` keyword carrying a previously
    confirmed ImportMapping.mapping_data (JSON-decoded) that overrides the
    deterministic header/position search. Tier 3 (AI-assisted) doesn't
    exist yet, so a Tier 1 miss with no Tier 2 match just returns Tier 1's
    own result, unchanged.

    Returns (result, warnings, tier) where tier is 'deterministic' or
    'cached', for ImportLog to record (acceptance criterion 6)."""
    result, warnings = parse_fn(filepath)
    if not warnings:
        return result, warnings, 'deterministic'

    cached = resolve_cached_mapping(file_type, filepath)
    if cached is None:
        return result, warnings, 'deterministic'

    cached_result, cached_warnings = parse_fn(filepath, mapping=json.loads(cached.mapping_data))
    if cached_warnings:
        return result, warnings, 'deterministic'
    return cached_result, [], 'cached'
