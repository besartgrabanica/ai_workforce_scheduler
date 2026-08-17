"""
Multi-provider LLM abstraction.

Supported providers
───────────────────
  mistral    – Mistral AI (EU-hosted; default — see README/RUNBOOK)
  ollama     – local Llama via Ollama REST API
  anthropic  – Claude via Anthropic SDK
  openai     – GPT models via OpenAI API
  gemini     – Gemini via Google's OpenAI-compatible endpoint

All providers expose:
  stream_chat(messages, system, **kw) → Generator[str, None, None]

Tool-calling (documents, import log, employee drill-down) is only available
for the OpenAI-compatible providers (mistral/openai/gemini) via chat_with_tools —
Anthropic and Ollama fall back to plain streaming without tool access. Mistral's
chat completions API is OpenAI-compatible, so it reuses the same code path as
openai/gemini rather than needing a separate SDK.
"""
import json
import os

import requests

# ── helpers ──────────────────────────────────────────────────────────────────

def _get_param(key: str, default: str = '') -> str:
    """Read an AI setting from the global (cross-project) GlobalSetting table,
    fall back to env var. AI config is deliberately global, not per-project —
    see scheduler/models.py's GlobalSetting."""
    try:
        from scheduler.models import GlobalSetting
        p = GlobalSetting.query.filter_by(key=key).first()
        if p and p.value:
            return p.value
    except Exception:
        pass
    return os.environ.get(key.upper(), default)


# Providers whose chat-completions API is OpenAI-shaped (request/response
# format, including tool-calling) — covers Mistral, OpenAI itself, and Gemini's
# compatibility endpoint. One code path serves all three.
_OPENAI_COMPAT_PROVIDERS = {
    'mistral': {
        'model_key': 'ai_mistral_model', 'model_default': 'mistral-large-latest',
        'api_key_env': 'MISTRAL_API_KEY', 'api_key_param': 'ai_mistral_key',
        'base_url': 'https://api.mistral.ai/v1',
    },
    'openai': {
        'model_key': 'ai_openai_model', 'model_default': 'gpt-4o',
        'api_key_env': 'OPENAI_API_KEY', 'api_key_param': 'ai_openai_key',
        'base_url': None,
    },
    'gemini': {
        'model_key': 'ai_gemini_model', 'model_default': 'gemini-1.5-pro',
        'api_key_env': 'GEMINI_API_KEY', 'api_key_param': 'ai_gemini_key',
        'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai/',
    },
}


def _openai_compat_config(prov: str):
    cfg = _OPENAI_COMPAT_PROVIDERS[prov]
    model = _get_param(cfg['model_key'], cfg['model_default'])
    api_key = os.environ.get(cfg['api_key_env'], _get_param(cfg['api_key_param'], ''))
    return model, api_key, cfg['base_url']


# ── Ollama ────────────────────────────────────────────────────────────────────

def _stream_ollama(messages: list, system: str, model: str, base_url: str):
    url = base_url.rstrip('/') + '/api/chat'
    all_messages = [{'role': 'system', 'content': system}] + messages
    payload = {'model': model, 'messages': all_messages, 'stream': True}
    try:
        with requests.post(url, json=payload, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                chunk = data.get('message', {}).get('content', '')
                if chunk:
                    yield chunk
                if data.get('done'):
                    break
    except requests.exceptions.ConnectionError:
        yield '\n\n⚠️ Cannot connect to Ollama. Make sure it is running (`ollama serve`).'
    except Exception as e:
        yield f'\n\n⚠️ Ollama error: {e}'


# ── Anthropic / Claude ────────────────────────────────────────────────────────

def _stream_anthropic(messages: list, system: str, model: str, api_key: str):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        with client.messages.stream(
            model=model,
            max_tokens=2048,
            system=system,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield text
    except ImportError:
        yield '⚠️ anthropic package not installed.'
    except Exception as e:
        yield f'\n\n⚠️ Claude error: {e}'


# ── OpenAI-compatible (Mistral + OpenAI + Gemini + Ollama /v1) ───────────────

def _stream_openai_compat(messages: list, system: str, model: str,
                           api_key: str, base_url: str | None = None):
    try:
        from openai import OpenAI
        kwargs = {'api_key': api_key or 'none'}
        if base_url:
            kwargs['base_url'] = base_url
        client = OpenAI(**kwargs)
        all_messages = [{'role': 'system', 'content': system}] + messages
        with client.chat.completions.create(
            model=model,
            messages=all_messages,
            stream=True,
            max_tokens=2048,
        ) as stream:
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ''
                if delta:
                    yield delta
    except Exception as e:
        yield f'\n\n⚠️ Error: {e}'


def _chunk_text(text: str, words_per_chunk: int = 12):
    """Split already-complete text into word-grouped pieces so a UI built for
    token streaming still gets a similar feel, without an extra network round
    trip once the model has already produced its final answer (see
    _stream_openai_compat_with_tools)."""
    words = text.split(' ')
    for i in range(0, len(words), words_per_chunk):
        piece = ' '.join(words[i:i + words_per_chunk])
        yield piece + (' ' if i + words_per_chunk < len(words) else '')


def _stream_openai_compat_with_tools(messages: list, system: str, model: str, api_key: str,
                                      tools: list, tool_executor, base_url: str | None = None,
                                      max_tool_iterations: int = 5):
    """Resolve any tool calls first (non-streaming — these round trips are
    normally short), then yield the final answer in word-grouped chunks.

    tool_executor(name: str, arguments: dict) -> str must be provided by the
    caller (app.py), since tools need DB/Flask context this module doesn't have.
    """
    try:
        from openai import OpenAI
        kwargs = {'api_key': api_key or 'none'}
        if base_url:
            kwargs['base_url'] = base_url
        client = OpenAI(**kwargs)
    except Exception as e:
        yield f'\n\n⚠️ Error: {e}'
        return

    convo = [{'role': 'system', 'content': system}] + list(messages)

    for _ in range(max_tool_iterations):
        try:
            resp = client.chat.completions.create(
                model=model, messages=convo, tools=tools, max_tokens=2048,
            )
        except Exception as e:
            yield f'\n\n⚠️ Error: {e}'
            return

        msg = resp.choices[0].message

        if not msg.tool_calls:
            for piece in _chunk_text(msg.content or ''):
                yield piece
            return

        convo.append({
            'role': 'assistant',
            'content': msg.content,
            'tool_calls': [
                {'id': tc.id, 'type': 'function',
                 'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or '{}')
            except json.JSONDecodeError:
                args = {}
            try:
                result = tool_executor(tc.function.name, args)
            except Exception as e:
                result = f'Error running tool: {e}'
            convo.append({'role': 'tool', 'tool_call_id': tc.id, 'content': str(result)})

    yield '\n\n⚠️ Reached the tool-call limit without a final answer.'


# ── Public API ────────────────────────────────────────────────────────────────

def stream_chat(messages: list, system: str = '', provider: str | None = None):
    """
    Yield response text chunks from the configured (or specified) LLM provider.
    Plain streaming, no tool access — see chat_with_tools for that.

    messages : list of {'role': 'user'|'assistant', 'content': str}
    system   : system prompt string
    provider : override; if None, reads 'ai_provider' from DB/env
    """
    prov = provider or _get_param('ai_provider', 'mistral')

    if prov == 'ollama':
        model    = _get_param('ai_ollama_model', 'llama3.2')
        base_url = _get_param('ai_ollama_url',   'http://localhost:11434')
        yield from _stream_ollama(messages, system, model, base_url)

    elif prov == 'anthropic':
        model   = _get_param('ai_anthropic_model', 'claude-opus-4-8')
        api_key = os.environ.get('ANTHROPIC_API_KEY', _get_param('ai_anthropic_key', ''))
        yield from _stream_anthropic(messages, system, model, api_key)

    elif prov in _OPENAI_COMPAT_PROVIDERS:
        model, api_key, base_url = _openai_compat_config(prov)
        yield from _stream_openai_compat(messages, system, model, api_key, base_url)

    else:
        yield f'⚠️ Unknown provider: {prov}'


def chat_with_tools(messages: list, system: str = '', provider: str | None = None,
                     tools: list | None = None, tool_executor=None):
    """
    Like stream_chat, but lets the model call tools (documents, import log,
    employee drill-down) when it decides it needs them, instead of everything
    being stuffed into the system prompt up front.

    Only mistral/openai/gemini support this (their APIs are OpenAI-shaped).
    Anthropic/Ollama silently fall back to stream_chat without tool access.
    """
    prov = provider or _get_param('ai_provider', 'mistral')

    if prov in _OPENAI_COMPAT_PROVIDERS and tools and tool_executor:
        model, api_key, base_url = _openai_compat_config(prov)
        yield from _stream_openai_compat_with_tools(
            messages, system, model, api_key, tools, tool_executor, base_url
        )
    else:
        yield from stream_chat(messages, system=system, provider=prov)


# ── Tool schemas (OpenAI/Mistral function-calling format) ───────────────────
# Execution lives in app.py, which has the DB/Flask context these need.

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'list_documents',
            'description': (
                "List uploaded documents (contracts, commitments, forecasts, misc files) "
                "for this project, with id/name/category/description. Call this before "
                "read_document to find the right document id."
            ),
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_document',
            'description': (
                "Read the extracted text content of one uploaded document by id. "
                "Only works for text-extractable types (xlsx, csv, txt, docx) — "
                "check list_documents first."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'document_id': {'type': 'integer', 'description': 'The document id from list_documents.'},
                },
                'required': ['document_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_import_log',
            'description': (
                "Read recent import warnings/errors — e.g. when an uploaded forecast or "
                "employee-spec Excel file didn't match the expected column/header layout. "
                "Use this to answer questions like 'did the last import have any issues?'."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {'type': 'integer', 'description': 'Max entries to return (default 20).'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_team_roster',
            'description': (
                "Full per-employee detail (FTE%, mode, shift, weekend/holiday availability, "
                "notes) for one team. The always-available context only has team-level "
                "summaries, so call this whenever a question needs individual employee detail."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'team_name': {'type': 'string', 'description': 'Exact team name, e.g. "Team Agon".'},
                },
                'required': ['team_name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'find_employee',
            'description': "Look up one employee by (partial) name and return their full detail.",
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': 'Full or partial employee name.'},
                },
                'required': ['name'],
            },
        },
    },
]


# ── Context builder ───────────────────────────────────────────────────────────

def build_workforce_context(project: dict | None = None) -> str:
    """
    Build a compact text summary of workforce data to inject as system context —
    company info and team-level aggregates only. Per-employee detail, documents,
    and the import log are deliberately NOT included here; they're available via
    tool calls (see TOOLS above) so the ~230 employees' worth of detail is only
    ever paid for (in tokens) when a question actually needs it.
    """
    import calendar

    from scheduler.models import Employee, ForecastPeriod, Schedule, Team

    project = project or {}
    company = project.get('company', 'the company')
    client = project.get('client', '')
    site = project.get('site', '')

    lines = [
        f'You are an AI assistant for {company}\'s {project.get("display_name", "")} workforce scheduling system.',
        'You have tools available for document lookup, the import log, and per-employee/team detail — '
        'call them whenever a question needs specifics beyond the summary below. '
        'Answer accurately and concisely; use bullet points for lists and show your working briefly for stats.',
        '',
        '═══ COMPANY INFO ═══',
        f'Company: {company}',
        f'Client:  {client}',
        f'Site:    {site}',
        '',
    ]

    active = Employee.query.filter_by(status='active').all()
    trainees = Employee.query.filter_by(status='trainee').count()
    inactive = Employee.query.filter_by(status='inactive').count()

    lines.append(f'═══ TEAMS ({len(active)} active employees, {trainees} trainees, {inactive} inactive) ═══')
    teams = Team.query.order_by(Team.name).all()
    for team in teams:
        members = [e for e in active if e.team == team.name]
        if not members:
            continue
        avg_fte = sum(e.fte_percent for e in members) / len(members)
        we_yes = sum(1 for e in members if e.works_saturday == 'yes' or e.works_sunday == 'yes')
        lines.append(f'• {team.name}: {len(members)} agents, avg FTE {avg_fte:.0f}%, {we_yes} work weekends')

    schedules = Schedule.query.order_by(Schedule.generated_at.desc()).limit(3).all()
    if schedules:
        lines += ['', '═══ RECENT SCHEDULES ═══']
        for s in schedules:
            lines.append(f'• {s.name} ({calendar.month_name[s.month]} {s.year})')

    periods = ForecastPeriod.query.order_by(ForecastPeriod.start_date.desc()).limit(2).all()
    if periods:
        lines += ['', '═══ FORECAST PERIODS ═══']
        for p in periods:
            lines.append(f'• {p.name}: {p.start_date} → {p.end_date}')

    return '\n'.join(lines)
