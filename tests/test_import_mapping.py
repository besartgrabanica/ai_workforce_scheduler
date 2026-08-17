"""
Regression suite for scheduler/import_mapping.py — Tier 2 of the 3-tier
import resolution (docs/specs/2026-08-import-mapping-detection.md). Builds
minimal synthetic .xlsx workbooks the same way tests/test_parsers_eon.py
does, then exercises fingerprinting and the deterministic/cached resolution
flow against them.
"""
import json
from datetime import date

import openpyxl

from app import db
from scheduler.import_mapping import compute_layout_fingerprint, resolve_cached_mapping, resolve_import
from scheduler.models import ImportMapping
from scheduler.parsers.eon import parse_abnahme_de_file, parse_tfc_file


def _set(ws, row, col0, value):
    ws.cell(row=row, column=col0 + 1, value=value)


def _save(wb, tmp_path, name='test.xlsx'):
    path = str(tmp_path / name)
    wb.save(path)
    return path


def _tfc_workbook_with_gesamt(gesamt_col):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'KiKxxl'
    _set(ws, 7, gesamt_col, 'Gesamt')
    _set(ws, 9, 0, 'Juni')
    _set(ws, 9, 1, 23)
    _set(ws, 9, 2, 'Mo')
    _set(ws, 9, 3, date(2026, 6, 1))
    _set(ws, 9, gesamt_col, 120.5)
    return wb


def _tfc_workbook_without_gesamt():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'KiKxxl'
    _set(ws, 9, 0, 'Juni')
    _set(ws, 9, 1, 23)
    _set(ws, 9, 2, 'Mo')
    _set(ws, 9, 3, date(2026, 6, 1))
    return wb


# ── compute_layout_fingerprint ──────────────────────────────────────────────

def test_fingerprint_is_stable_across_data_row_changes(tmp_path):
    wb1 = _tfc_workbook_with_gesamt(10)
    fp1 = compute_layout_fingerprint(_save(wb1, tmp_path, 'a.xlsx'))

    wb2 = _tfc_workbook_with_gesamt(10)
    _set(wb2.active, 9, 10, 999.0)  # different data value, same header layout
    fp2 = compute_layout_fingerprint(_save(wb2, tmp_path, 'b.xlsx'))

    assert fp1 == fp2


def test_fingerprint_changes_when_header_layout_changes(tmp_path):
    fp_at_10 = compute_layout_fingerprint(_save(_tfc_workbook_with_gesamt(10), tmp_path, 'a.xlsx'))
    fp_no_header = compute_layout_fingerprint(_save(_tfc_workbook_without_gesamt(), tmp_path, 'b.xlsx'))

    assert fp_at_10 != fp_no_header


# ── resolve_cached_mapping ───────────────────────────────────────────────────

def test_resolve_cached_mapping_misses_with_no_rows(ctx, tmp_path):
    path = _save(_tfc_workbook_without_gesamt(), tmp_path)
    assert resolve_cached_mapping('tfc_forecast', path) is None


def test_resolve_cached_mapping_hits_on_matching_file_type_and_fingerprint(ctx, tmp_path):
    path = _save(_tfc_workbook_without_gesamt(), tmp_path)
    fingerprint = compute_layout_fingerprint(path)
    db.session.add(ImportMapping(
        file_type='tfc_forecast', layout_fingerprint=fingerprint,
        mapping_data=json.dumps({'gesamt_col': 10}),
    ))
    db.session.commit()

    found = resolve_cached_mapping('tfc_forecast', path)
    assert found is not None
    assert json.loads(found.mapping_data) == {'gesamt_col': 10}


def test_resolve_cached_mapping_ignores_wrong_file_type(ctx, tmp_path):
    path = _save(_tfc_workbook_without_gesamt(), tmp_path)
    fingerprint = compute_layout_fingerprint(path)
    db.session.add(ImportMapping(
        file_type='abnahme_de', layout_fingerprint=fingerprint,
        mapping_data=json.dumps({'date_col': 1, 'value_col': 4}),
    ))
    db.session.commit()

    assert resolve_cached_mapping('tfc_forecast', path) is None


# ── resolve_import ───────────────────────────────────────────────────────────

def test_resolve_import_uses_tier1_unchanged_when_clean(ctx, tmp_path):
    path = _save(_tfc_workbook_with_gesamt(10), tmp_path)

    result, warnings, tier = resolve_import('tfc_forecast', path, parse_tfc_file)

    assert tier == 'deterministic'
    assert warnings == []
    assert result[0]['total_sync'] == 120.5
    assert ImportMapping.query.count() == 0  # Tier 2 never consulted on a clean Tier 1 hit


def test_resolve_import_falls_back_to_tier1_when_no_cache(ctx, tmp_path):
    path = _save(_tfc_workbook_without_gesamt(), tmp_path)

    result, warnings, tier = resolve_import('tfc_forecast', path, parse_tfc_file)

    assert tier == 'deterministic'
    assert any('Gesamt' in w for w in warnings)


def test_resolve_import_applies_cached_mapping_on_tier1_miss(ctx, tmp_path):
    path = _save(_tfc_workbook_without_gesamt(), tmp_path)

    # Reformatted file: header search fails, but the real Gesamt data lives at col 20.
    wb = openpyxl.load_workbook(path)
    ws = wb['KiKxxl']
    _set(ws, 9, 20, 77.0)
    wb.save(path)

    fingerprint = compute_layout_fingerprint(path)
    db.session.add(ImportMapping(
        file_type='tfc_forecast', layout_fingerprint=fingerprint,
        mapping_data=json.dumps({'gesamt_col': 20}),
    ))
    db.session.commit()

    result, warnings, tier = resolve_import('tfc_forecast', path, parse_tfc_file)

    assert tier == 'cached'
    assert warnings == []
    assert result[0]['total_sync'] == 77.0


def test_resolve_import_distrusts_cached_mapping_that_still_warns(ctx, tmp_path):
    path = _save(_tfc_workbook_without_gesamt(), tmp_path)
    fingerprint = compute_layout_fingerprint(path)
    # A stale/bad cached mapping — 'gesamt_col' key deliberately absent, so
    # parse_tfc_file falls straight back to its own header search, which
    # still misses on this file and re-warns.
    db.session.add(ImportMapping(
        file_type='tfc_forecast', layout_fingerprint=fingerprint,
        mapping_data=json.dumps({}),
    ))
    db.session.commit()

    result, warnings, tier = resolve_import('tfc_forecast', path, parse_tfc_file)

    assert tier == 'deterministic'
    assert any('Gesamt' in w for w in warnings)


# ── parser mapping overrides ─────────────────────────────────────────────────

def test_parse_tfc_file_with_mapping_skips_header_search(tmp_path):
    path = _save(_tfc_workbook_without_gesamt(), tmp_path)
    wb = openpyxl.load_workbook(path)
    _set(wb['KiKxxl'], 9, 15, 42.0)
    wb.save(path)

    results, warnings = parse_tfc_file(path, mapping={'gesamt_col': 15})

    assert warnings == []
    assert results[0]['total_sync'] == 42.0


def test_parse_abnahme_de_file_with_mapping_skips_header_search(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    _set(ws, 1, 0, 'unrelated header')  # deliberately no 'Datum' anywhere
    _set(ws, 2, 0, date(2026, 6, 1))
    _set(ws, 2, 3, 300.0)
    path = _save(wb, tmp_path)

    result, warnings = parse_abnahme_de_file(path, mapping={'date_col': 0, 'value_col': 3})

    assert warnings == []
    assert result == {date(2026, 6, 1): 300.0}
