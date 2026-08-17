"""
Regression suite for scheduler/ai_import_mapping.py — Tier 3 of the 3-tier
import resolution. No real network calls anywhere here: this repo's real
.env carries a live ANTHROPIC_API_KEY (loaded by app.py's load_dotenv() at
import time), so every "has an API key" path below monkeypatches
anthropic.Anthropic with a fake client rather than risk a real, billed call.
"""
import scheduler.ai_import_mapping as aim


class _FakeContentBlock:
    def __init__(self, text):
        self.text = text


class _FakeMessage:
    def __init__(self, text):
        self.content = [_FakeContentBlock(text)]


class _FakeMessages:
    def __init__(self, response_text):
        self._response_text = response_text

    def create(self, **kwargs):
        return _FakeMessage(self._response_text)


class _FakeAnthropicClient:
    def __init__(self, response_text):
        self.messages = _FakeMessages(response_text)


def _stub_anthropic(monkeypatch, response_text):
    monkeypatch.setattr(aim.anthropic, 'Anthropic', lambda api_key=None: _FakeAnthropicClient(response_text))


# ── safe defaults (no API key / unknown file type) ──────────────────────────

def test_propose_mapping_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    assert aim.propose_mapping('tfc_forecast', [{'row': 6, 'col': 10, 'value': 'Gesamt'}]) is None


def test_propose_mapping_returns_none_for_unknown_file_type():
    assert aim.propose_mapping('employee_spec', []) is None


# ── tfc_forecast ─────────────────────────────────────────────────────────────

def test_propose_mapping_parses_tfc_json_response(monkeypatch):
    _stub_anthropic(monkeypatch, '{"gesamt_col": 42, "confidence": 0.9, "rationale": "moved right"}')

    result = aim.propose_mapping('tfc_forecast', [{'row': 6, 'col': 42, 'value': 'Total'}], api_key='fake-key')

    assert result == {'mapping': {'gesamt_col': 42}, 'confidence': 0.9, 'rationale': 'moved right'}


def test_propose_mapping_strips_markdown_fences(monkeypatch):
    _stub_anthropic(monkeypatch, '```json\n{"gesamt_col": 5, "confidence": 0.5, "rationale": "x"}\n```')

    result = aim.propose_mapping('tfc_forecast', [], api_key='fake-key')

    assert result['mapping'] == {'gesamt_col': 5}


def test_propose_mapping_returns_none_when_model_cannot_place_gesamt_col(monkeypatch):
    _stub_anthropic(monkeypatch, '{"gesamt_col": null, "confidence": 0.1, "rationale": "no signal"}')

    assert aim.propose_mapping('tfc_forecast', [], api_key='fake-key') is None


# ── abnahme_de ───────────────────────────────────────────────────────────────

def test_propose_mapping_parses_abnahme_json_response(monkeypatch):
    _stub_anthropic(monkeypatch, '{"date_col": 1, "value_col": 5, "confidence": 0.8, "rationale": "rightmost numeric"}')

    result = aim.propose_mapping('abnahme_de', [], api_key='fake-key')

    assert result == {'mapping': {'date_col': 1, 'value_col': 5}, 'confidence': 0.8, 'rationale': 'rightmost numeric'}


def test_propose_mapping_returns_none_when_abnahme_missing_either_column(monkeypatch):
    _stub_anthropic(monkeypatch, '{"date_col": 1, "value_col": null, "confidence": 0.3, "rationale": "unsure"}')

    assert aim.propose_mapping('abnahme_de', [], api_key='fake-key') is None


# ── failure modes ────────────────────────────────────────────────────────────

def test_propose_mapping_returns_none_on_malformed_json(monkeypatch):
    _stub_anthropic(monkeypatch, 'not json at all')

    assert aim.propose_mapping('tfc_forecast', [], api_key='fake-key') is None


def test_propose_mapping_returns_none_when_the_api_call_raises(monkeypatch):
    class _RaisingMessages:
        def create(self, **kwargs):
            raise RuntimeError('network error')

    class _RaisingClient:
        def __init__(self, api_key=None):
            self.messages = _RaisingMessages()

    monkeypatch.setattr(aim.anthropic, 'Anthropic', _RaisingClient)

    assert aim.propose_mapping('tfc_forecast', [], api_key='fake-key') is None
