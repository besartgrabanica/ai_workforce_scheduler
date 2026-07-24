"""
Registry of per-project Excel parser modules.

Each client sends forecast/employee data in their own file format — there's
no generic way to parse an arbitrary external spreadsheet layout, so each
project gets its own module here implementing whichever of the four
functions defined in eon.py apply to that client's real files:
  parse_tfc_file(filepath)                 -> (rows, warnings)   — required for forecast import
  parse_abnahme_de_file(filepath)          -> (dict, warnings)   — optional
  parse_forecast_calculation_file(filepath)-> (dict, warnings)   — optional
  parse_employee_file(filepath)            -> (rows, warnings)   — required for employee import

To onboard a new client's parser: get a real sample file from them, use
eon.py as the reference shape for the functions/return types, write
scheduler/parsers/<project_key>.py implementing what applies, then add one
line to _REGISTRY below. Nothing else needs to change — app.py's import
routes look the module up by the active project's key and handle a missing
module (or a missing individual function on it) with a clear "not set up
yet" message instead of guessing or crashing.
"""
from . import eon

_REGISTRY = {
    'eon': eon,
}


def get_parser_module(project_key):
    """The parser module registered for this project, or None if none exists yet."""
    return _REGISTRY.get(project_key)
