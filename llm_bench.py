#!/usr/bin/env python3
"""
LLM Standings updater (llm_bench) — a leaderboard of LLM leaderboards (pure standard library, no pip install needed)
https://github.com/Acrab-Agentic-Labs/llm-standings · code MIT, data CC BY 4.0 · Acrab AI

The page is the static index.html, which loads data.js; this tool only writes the data (data.js + data.json).

  python3 llm_bench.py update                 # fetch sources → Claude extraction → discover new models/sources → reputation → write data.js
  python3 llm_bench.py update --only aa-models,arena-text      # only these sources (ids, comma-separated, trailing * = prefix)
  python3 llm_bench.py update --all           # also re-fetch static sources (official / paper)
  python3 llm_bench.py update --force         # re-extract even if the page hash is unchanged
  python3 llm_bench.py update --no-discover --no-reputation --limit 10
  python3 llm_bench.py render                 # rebuild data.js + data.json from the existing data (no fetching)
  python3 llm_bench.py render --standalone    # ...and build dist/llm-standings.html (page + ECharts + data in one file)
  python3 llm_bench.py sources [--failed]     # source status
  python3 llm_bench.py add <url> --name "xx" --type third_party --cat coding [--static]
  python3 llm_bench.py import results.json    # merge extraction results collected elsewhere
  python3 llm_bench.py doctor                 # check Claude backend / network
(`python3 -m llm_bench update` also works when run from this directory.)

Claude backend (config.json -> claude.backend = auto|api|cli|off):
  auto: Anthropic API (with web_search/web_fetch) if ANTHROPIC_API_KEY is set, otherwise the local `claude -p`
  (Claude Code subscription).
Optional environment variables (or a .env file in this directory): ANTHROPIC_API_KEY, ARTIFICIAL_ANALYSIS_API_KEY,
  REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET, GITHUB_TOKEN, OPENROUTER_API_KEY
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import html
import html.parser
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
CACHE = os.path.join(DATA, 'cache')
HIST = os.path.join(DATA, 'history')
TODAY = dt.date.today().isoformat()
EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
EFFORT_ALL = set(EFFORTS) | {'default', 'unknown'}
CATEGORIES = ['overall', 'coding', 'agentic', 'reasoning', 'knowledge', 'multimodal', 'webdev', '3d',
              'long_context', 'writing', 'safety', 'other']
SOURCE_TYPES = ['official', 'third_party', 'paper', 'community', 'aggregator']
SRC_TIER = {'third_party': 0, 'community': 0, 'paper': 1, 'official': 2, 'aggregator': 3}
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/130.0 Safari/537.36')

DEFAULT_CONFIG = {
    "claude": {
        "backend": "auto",              # auto | api | cli | off
        "api_model": "auto",            # auto = newest sonnet from /v1/models; or pin one, e.g. claude-sonnet-5-5
        "cli_model": "sonnet",          # claude -p --model
        "cli_path": "claude",
        "max_tokens": 16000,
        "workers": 4,
        "web_tools": True,
        "api_web_search_tool": "web_search_20250305",
        "api_web_fetch_tool": "web_fetch_20250910",
        "api_web_fetch_beta": "web-fetch-2025-09-10",
        "timeout": 900
    },
    "fetch": {"timeout": 40, "max_chars": 110000, "min_text_chars": 1200, "workers": 6},
    "scope": {"max_gen": 1},
    "update": {"discover": True, "reputation": True, "static_refresh_days": 30,
               "discover_block_domains": ["benchlm.ai", "llm-stats.com", "benchmarklist.com", "modelgrep.com",
                                          "llmgateway.io", "codelifter.com", "datalearner.com", "lmcouncil.ai",
                                          "promptzone.com", "evertune.ai", "taskade.com", "gumloop.com"]},
    "reputation": {
        "days": 30, "max_posts_per_model": 60, "use_claude_web": True, "max_models": 40,
        "channels": ["hn", "reddit", "github", "v2ex"],
        "github_repos": ["anthropics/claude-code", "openai/codex", "google-gemini/gemini-cli", "sst/opencode",
                         "MoonshotAI/kimi-cli", "QwenLM/qwen-code"],
        "subreddits": ["LocalLLaMA", "ClaudeAI", "OpenAI", "ChatGPTCoding", "singularity", "Bard", "cursor"]
    },
    "signal_sources": [
        {"id": "openrouter-rankings", "url": "https://openrouter.ai/rankings?view=week",
         "hint": "Weekly token usage leaderboard. For each model give rank and weekly tokens (in T or B tokens).",
         "signals": ["openrouter_rank_week", "openrouter_tokens_week"]},
        {"id": "livenerf", "url": "https://raw.githubusercontent.com/ninjahawk/livenerf/master/results/daily.csv",
         "hint": "Daily degradation tracker. Report the latest day's drift estimate vs baseline per model (signal 'degradation_drift', unit 'score delta').",
         "signals": ["degradation_drift"]}
    ],
    "report": {"title": "LLM Standings", "data_js": "data.js", "data_json": "data.json",
               "standalone": "dist/llm-standings.html",
               "focus_models": ["qwen3.8-flash-next", "glm-5.3-flash", "mistral-medium-3.5", "gpt-oss-120b",
                                "nemotron-3.5-super-vl"]}
}

_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


# ----------------------------------------------------------------------------- io helpers
def jload(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def jsave(path, obj, indent=1):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_env():
    p = os.path.join(ROOT, '.env')
    if not os.path.exists(p):
        return
    for line in open(p, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config():
    p = os.path.join(ROOT, 'config.json')
    user = jload(p, None)
    if user is None:
        jsave(p, DEFAULT_CONFIG, indent=2)
        user = {}
    return deep_merge(DEFAULT_CONFIG, user)


class Store:
    """All json data files under data/."""

    def __init__(self):
        os.makedirs(CACHE, exist_ok=True)
        os.makedirs(HIST, exist_ok=True)
        self.models = jload(os.path.join(DATA, 'models.json'), {'models': []})
        self.bench = jload(os.path.join(DATA, 'benchmarks.json'), {'benchmarks': []})
        self.sources = jload(os.path.join(DATA, 'sources.json'), {'sources': []})
        self.scores = jload(os.path.join(DATA, 'scores.json'), {'scores': []})
        self.rep = jload(os.path.join(DATA, 'reputation.json'), {'sentiment': [], 'signals': [], 'history': []})
        self.harness = jload(os.path.join(DATA, 'harnesses.json'), {})
        self.changelog = jload(os.path.join(DATA, 'changelog.json'), {'entries': []})
        self.notes = jload(os.path.join(DATA, 'research_notes.json'), {})
        self.corrections = jload(os.path.join(DATA, 'corrections.json'), {}).get('entries', [])
        self.lock = threading.RLock()

    # convenient views
    @property
    def M(self):
        return {m['id']: m for m in self.models['models']}

    @property
    def B(self):
        return {b['id']: b for b in self.bench['benchmarks']}

    @property
    def S(self):
        return {s['id']: s for s in self.sources['sources']}

    def save(self):
        for name, obj in [('models', self.models), ('benchmarks', self.bench), ('sources', self.sources),
                          ('reputation', self.rep), ('changelog', self.changelog)]:
            obj['updated'] = TODAY
            jsave(os.path.join(DATA, name + '.json'), obj)
        self.scores['updated'] = TODAY
        jsave(os.path.join(DATA, 'scores.json'), self.scores, indent=0)


# ----------------------------------------------------------------------------- fetching
def _ssl_ctx():
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch(url, timeout=40, headers=None, data=None, method=None):
    """Return (status, content_type, text). Tries curl first (system certs, gzip), then urllib."""
    headers = dict(headers or {})
    headers.setdefault('User-Agent', UA)
    headers.setdefault('Accept', 'text/html,application/json,text/csv,text/plain,*/*;q=0.8')
    headers.setdefault('Accept-Language', 'en-US,en;q=0.9,zh-CN;q=0.8')
    curl = shutil.which('curl')
    if curl and data is None and method is None:
        try:
            cmd = [curl, '-sS', '-L', '--compressed', '--max-time', str(timeout), '-w', '\n__STATUS__%{http_code}|%{content_type}']
            for k, v in headers.items():
                cmd += ['-H', f'{k}: {v}']
            cmd.append(url)
            p = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            out = p.stdout.decode('utf-8', 'replace')
            if '__STATUS__' in out:
                body, tail = out.rsplit('\n__STATUS__', 1)
                code, ctype = (tail.split('|', 1) + [''])[:2]
                return int(code or 0), ctype, body
        except Exception:
            pass
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            raw = r.read()
            if r.headers.get('Content-Encoding') == 'gzip':
                import gzip
                raw = gzip.decompress(raw)
            return r.status, r.headers.get('Content-Type', ''), raw.decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8', 'replace')
        except Exception:
            body = ''
        return e.code, '', body
    except Exception as e:
        return 0, '', f'__ERROR__ {type(e).__name__}: {e}'


class _Text(html.parser.HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'head'}
    BLOCK = {'p', 'div', 'section', 'article', 'h1', 'h2', 'h3', 'h4', 'h5', 'li', 'br', 'tr', 'table', 'caption'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.json_blobs, self._script_type = [], 0, [], None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            if tag == 'script':
                a = dict(attrs)
                self._script_type = (a.get('type') or '') + '|' + (a.get('id') or '')
        if tag in ('td', 'th'):
            self.out.append(' | ')
        elif tag in self.BLOCK:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.out.append('\n')

    def handle_data(self, d):
        if self.skip:
            if self._script_type and ('json' in self._script_type or '__NEXT_DATA__' in self._script_type) \
                    and len(d) > 200:
                self.json_blobs.append(d)
            elif 'self.__next_f.push' in d and len(d) > 500:   # Next.js RSC payload
                self.json_blobs.append(d)
            return
        if d.strip():
            self.out.append(d)


def to_text(body, ctype=''):
    b = body.lstrip()
    if 'json' in ctype or b[:1] in '[{':
        try:
            return json.dumps(json.loads(body), ensure_ascii=False, separators=(',', ':'))
        except Exception:
            return body
    if 'csv' in ctype or 'text/plain' in ctype or not re.search(r'<(html|body|div|table)\b', b[:5000], re.I):
        return body
    p = _Text()
    try:
        p.feed(body)
    except Exception:
        return re.sub(r'<[^>]+>', ' ', body)
    text = re.sub(r'[ \t]+', ' ', ''.join(p.out))
    text = re.sub(r'\n\s*\n+', '\n', text).strip()
    blobs = '\n'.join(p.json_blobs)
    if blobs and (len(text) < 6000 or re.search(r'\d{2}\.\d', blobs)):
        text += '\n\n[EMBEDDED PAGE DATA]\n' + blobs[:200000]
    return text


def prefilter(text, keywords, max_chars):
    """Keep lines mentioning known models/benchmarks (with context) when text is too long."""
    if len(text) <= max_chars:
        return text
    lines = text.split('\n')
    if len(lines) < 30:  # one huge line (json): chunk by keyword windows
        low = text.lower()
        keep, i = [], 0
        spans = []
        for kw in keywords:
            for mt in re.finditer(re.escape(kw), low):
                spans.append((max(0, mt.start() - 600), mt.end() + 900))
        spans.sort()
        merged = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out = text[:3000] + '\n...\n' + '\n...\n'.join(text[s:e] for s, e in merged)
        return out[:max_chars]
    pat = re.compile('|'.join(re.escape(k) for k in keywords if len(k) >= 4), re.I) if keywords else None
    keep = set(range(min(40, len(lines))))
    for i, ln in enumerate(lines):
        if pat and pat.search(ln):
            keep.update(range(max(0, i - 3), min(len(lines), i + 4)))
    out = '\n'.join(lines[i] for i in sorted(keep))
    return out[:max_chars]


# ----------------------------------------------------------------------------- Claude backends
class ClaudeError(Exception):
    pass


class Claude:
    def __init__(self, cfg):
        self.cfg = cfg['claude']
        self.backend = None
        self.model = None
        self.web = bool(self.cfg.get('web_tools', True))
        self.calls = 0
        self.cost = 0.0
        want = self.cfg.get('backend', 'auto')
        key = os.environ.get('ANTHROPIC_API_KEY')
        cli = shutil.which(self.cfg.get('cli_path', 'claude')) or (
            os.path.exists(os.path.expanduser('~/.claude/local/claude')) and os.path.expanduser('~/.claude/local/claude'))
        if want == 'off':
            return
        if want in ('auto', 'api') and key:
            self.backend, self.key = 'api', key
        elif want in ('auto', 'cli') and cli:
            self.backend, self.cli = 'cli', cli
        elif want == 'api':
            raise ClaudeError('backend=api but ANTHROPIC_API_KEY is not set')
        elif want == 'cli':
            raise ClaudeError('backend=cli but the claude command was not found (npm i -g @anthropic-ai/claude-code)')

    @property
    def ok(self):
        return self.backend is not None

    def describe(self):
        if self.backend == 'api':
            return f'Anthropic API ({self.model or self.cfg["api_model"]})'
        if self.backend == 'cli':
            return f'Claude Code CLI (claude -p --model {self.cfg["cli_model"]})'
        return 'not configured (render only; update cannot extract)'

    # ---- public
    def ask(self, prompt, web=False):
        if not self.ok:
            raise ClaudeError('no Claude backend')
        web = web and self.web
        self.calls += 1
        if self.backend == 'api':
            return self._api(prompt, web)
        return self._cli(prompt, web)

    def ask_json(self, prompt, web=False):
        txt = self.ask(prompt, web)
        try:
            return parse_json(txt)
        except ValueError:
            txt2 = self.ask('Convert the following answer into the exact JSON object it was supposed to be. '
                            'Return ONLY JSON, no prose.\n\n' + txt[-60000:], web=False)
            return parse_json(txt2)

    # ---- API
    def _post(self, path, body, extra_headers=None):
        headers = {'x-api-key': self.key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
        headers.update(extra_headers or {})
        data = json.dumps(body).encode()
        for attempt in range(5):
            req = urllib.request.Request('https://api.anthropic.com' + path, data=data, headers=headers, method='POST')
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.get('timeout', 900), context=_ssl_ctx()) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                msg = e.read().decode('utf-8', 'replace')
                if e.code in (429, 500, 502, 503, 529) and attempt < 4:
                    time.sleep(8 * (attempt + 1))
                    continue
                raise ClaudeError(f'HTTP {e.code}: {msg[:400]}')
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 4:
                    time.sleep(5)
                    continue
                raise ClaudeError(str(e))

    def _resolve_model(self):
        if self.model:
            return self.model
        m = self.cfg.get('api_model', 'auto')
        if m and m != 'auto':
            self.model = m
            return m
        try:
            req = urllib.request.Request('https://api.anthropic.com/v1/models?limit=100', headers={
                'x-api-key': self.key, 'anthropic-version': '2023-06-01'})
            with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as r:
                ids = [x['id'] for x in json.loads(r.read().decode()).get('data', [])]
            son = [i for i in ids if 'sonnet' in i]
            self.model = (son or ids or ['claude-sonnet-4-5'])[0]
        except Exception:
            self.model = 'claude-sonnet-4-5'
        return self.model

    def _api(self, prompt, web):
        model = self._resolve_model()
        messages = [{'role': 'user', 'content': prompt}]
        body = {'model': model, 'max_tokens': self.cfg.get('max_tokens', 16000), 'messages': messages}
        extra = {}
        if web:
            body['tools'] = [{'type': self.cfg['api_web_search_tool'], 'name': 'web_search', 'max_uses': 5},
                             {'type': self.cfg['api_web_fetch_tool'], 'name': 'web_fetch', 'max_uses': 8}]
            extra['anthropic-beta'] = self.cfg['api_web_fetch_beta']
        final_text = ''
        for _ in range(6):
            try:
                resp = self._post('/v1/messages', body, extra)
            except ClaudeError as e:
                if web and 'tool' in str(e).lower() and len(body.get('tools', [])) > 1:   # fetch tool unsupported
                    body['tools'] = body['tools'][:1]
                    extra = {}
                    continue
                raise
            content = resp.get('content', [])
            u = resp.get('usage', {})
            self.cost += (u.get('input_tokens', 0) * 3 + u.get('output_tokens', 0) * 15) / 1e6
            # keep only text after the last tool result
            texts = []
            for blk in content:
                if blk.get('type') == 'text':
                    texts.append(blk.get('text', ''))
                elif blk.get('type', '').endswith('tool_result') or blk.get('type') == 'server_tool_use':
                    texts = []
            final_text = ''.join(texts) or final_text
            if resp.get('stop_reason') == 'pause_turn':
                messages = messages + [{'role': 'assistant', 'content': content}]
                body['messages'] = messages
                continue
            break
        return final_text

    # ---- CLI (Claude Code)
    def _cli(self, prompt, web):
        tools = 'WebFetch,WebSearch' if web else ''
        base = [self.cli, '-p', '--output-format', 'json', '--model', self.cfg.get('cli_model', 'sonnet')]
        variants = [
            base + ['--tools', tools, '--allowedTools', tools or 'none', '--no-session-persistence',
                    '--strict-mcp-config'],
            base + (['--allowedTools', 'WebFetch', 'WebSearch'] if web else []),
        ]
        last = ''
        with tempfile.TemporaryDirectory() as tmp:
            for cmd in variants:
                for attempt in range(3):
                    try:
                        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmp,
                                           timeout=self.cfg.get('timeout', 900))
                    except subprocess.TimeoutExpired:
                        last = 'timeout'
                        continue
                    out = (p.stdout or '').strip()
                    if p.returncode == 0 and out:
                        try:
                            j = json.loads(out.splitlines()[-1] if not out.startswith('{') else out)
                        except Exception:
                            return out
                        self.cost += float(j.get('total_cost_usd') or 0)
                        if j.get('is_error'):
                            last = str(j.get('result') or j)[:300]
                            if re.search(r'rate|overload|limit', last, re.I):
                                time.sleep(30 * (attempt + 1))
                                continue
                            break
                        return j.get('result') or ''
                    last = (p.stderr or out)[:400]
                    if re.search(r'unknown option|unrecognized|error: option', last, re.I):
                        break          # try next (older-CLI) variant
                    time.sleep(10 * (attempt + 1))
        raise ClaudeError('claude -p failed: ' + last)


def parse_json(txt):
    if not txt:
        raise ValueError('empty')
    m = re.findall(r'```(?:json)?\s*(.*?)```', txt, re.S)
    cands = m[::-1] + [txt]
    for c in cands:
        c = c.strip()
        for opener, closer in (('{', '}'), ('[', ']')):
            i, j = c.find(opener), c.rfind(closer)
            if i != -1 and j > i:
                try:
                    return json.loads(c[i:j + 1])
                except Exception:
                    continue
    raise ValueError('no json in answer: ' + txt[:200])


# ----------------------------------------------------------------------------- normalisation
def slug(s):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9.]+', '-', (s or '').lower())).strip('-')


def norm_key(s):
    return re.sub(r'[^a-z0-9.]+', '', (s or '').lower())


def strip_effort(raw):
    return re.sub(r'\s*[\(\[][^\)\]]*[\)\]]\s*$', '', raw or '').strip()


EFFORT_WORDS = [('non-reasoning', 'none'), ('non reasoning', 'none'), ('no thinking', 'none'), ('instant', 'none'),
                ('xhigh', 'xhigh'), ('x-high', 'xhigh'), ('extra high', 'xhigh'), ('max', 'max'),
                ('minimal', 'minimal'), ('medium', 'medium'), ('low', 'low'), ('high', 'high')]


HARNESS_RULES = [(r'^claude code(?! or)', 'Claude Code'), (r'^(openai )?codex', 'Codex CLI'), (r'^mini-?swe', 'mini-swe-agent'),
                 (r'^terminus', 'Terminus 2'), (r'^openhands', 'OpenHands'), (r'^swe-agent', 'SWE-agent'),
                 (r'^opencode', 'OpenCode'), (r'^gemini cli', 'Gemini CLI'), (r'^cursor', 'Cursor'),
                 (r'^deepseek harness', 'DeepSeek Harness'), (r'^kimi code', 'Kimi Code'), (r'^grok build', 'Grok Build')]


def norm_harness(h):
    if not h:
        return None
    s = str(h).strip()
    for pat, name in HARNESS_RULES:
        if re.search(pat, s, re.I):
            return name
    return s


def model_line(m):
    return m.get('line') or m.get('tier') or 'other'


def fmt_params(m):
    t, a = m.get('params_total_b'), m.get('params_active_b')
    if not t:
        return 'params n/a'
    return f'{t:g}B' + (f'-A{a:g}B' if a and a != t else '')


def bump_line(store, new_model):
    """A new generation only pushes down older models of the SAME vendor and size line."""
    v = (new_model.get('vendor') or '').lower()
    line = model_line(new_model)
    for om in store.models['models']:
        if om['vendor'].lower() == v and model_line(om) == line:
            om['gen'] = om.get('gen', 0) + 1


def norm_effort(e, raw=''):
    e = (str(e or '')).lower().strip()
    if e in EFFORT_ALL or e.startswith('budget:'):
        return e
    for w, v in EFFORT_WORDS:
        if w in e:
            return v
    m = re.search(r'\(([^)]*)\)', raw or '')
    if m:
        for w, v in EFFORT_WORDS:
            if w in m.group(1).lower():
                return v
    return 'unknown'


class Registry:
    def __init__(self, store):
        self.store = store
        self.rebuild()

    def rebuild(self):
        self.alias = {}
        for m in self.store.models['models']:
            for a in [m['id'], m['name']] + list(m.get('aliases') or []):
                self.alias.setdefault(norm_key(a), m['id'])
                self.alias.setdefault(norm_key(strip_effort(a)), m['id'])
        self.ignored = set(norm_key(x) for x in self.store.models.get('ignored_names', []))
        self.balias = {}
        for b in self.store.bench['benchmarks']:
            for a in [b['id'], b['name']] + list(b.get('aliases') or []):
                self.balias.setdefault(norm_key(a), b['id'])

    def model_id(self, mid, raw):
        M = self.store.M
        if mid in M:
            return mid
        for cand in (raw, strip_effort(raw), mid):
            k = norm_key(cand)
            if k in self.alias:
                return self.alias[k]
        return None

    def is_ignored(self, raw):
        return norm_key(strip_effort(raw)) in self.ignored or norm_key(raw) in self.ignored

    def in_scope(self, mid, max_gen):
        m = self.store.M.get(mid)
        return bool(m) and (m.get('gen', 0) <= max_gen)

    def models_prompt(self, max_gen):
        lines = []
        for m in sorted(self.store.models['models'], key=lambda m: (m['vendor'], m.get('gen', 0))):
            if m.get('gen', 0) > max_gen:
                continue
            al = [a for a in (m.get('aliases') or []) if a != m['name']][:6]
            lines.append(f"{m['id']}: {m['name']} [{m['vendor']}, gen{m.get('gen', 0)}]" + (f" aka {'; '.join(al)}" if al else ''))
        return '\n'.join(lines)

    def bench_prompt(self, cats=None):
        return '\n'.join(f"{b['id']}: {b['name']}" for b in self.store.bench['benchmarks'])

    def keywords(self, max_gen):
        kws = set()
        for m in self.store.models['models']:
            if m.get('gen', 0) <= max_gen:
                for a in [m['name']] + list(m.get('aliases') or []):
                    a = strip_effort(a).lower()
                    if len(a) >= 5:
                        kws.add(a)
        return sorted(kws, key=len, reverse=True)[:400]


# ----------------------------------------------------------------------------- prompts
EXTRACT_PROMPT = """You are a precise data-extraction engine for an LLM benchmark leaderboard.
Extract benchmark results from the SOURCE below.

Source: {name} | type: {stype} | url: {url}
Extraction hint: {hint}
Today: {today}

KNOWN MODELS (id: name [vendor, generation] aka aliases). Map each result to an id when it is clearly the same
model (dated snapshots like "1017"/"0902" map to the base id). If not listed, set "model": null and keep model_raw exact.
{models}

KNOWN BENCHMARK IDS (id: name). Reuse an id only for the same benchmark AND version AND metric (e.g. "fully
resolved" vs "almost solved", pass@1 vs pass@k, different context length = different ids); otherwise make a new
lowercase-dash id and define it in new_benchmarks.
{benchmarks}

EFFORT: canonical none|minimal|low|medium|high|xhigh|max|default|unknown. "(high)", "xhigh", "max with fallback"
-> the level; non-reasoning / instant / thinking off -> none; a thinking token budget -> "budget:32k";
"default" when the source states the model's default setting; "unknown" when not stated.

RULES
- Only numbers that literally appear in the source. Never compute, estimate or recall from memory.
- Skip models from older generations that are not in KNOWN MODELS (e.g. GPT-4o, Claude 3.x, Llama 3).
- One row per (model, effort, benchmark, harness). If the page names the agent/scaffold (Claude Code, Codex CLI,
  Terminus 2, mini-swe-agent, OpenHands ...) put it in harness.
- unit: % | elo | index | pts | $ | x | other. higher_better=false for cost, latency, error / hallucination rates.
- note (<=60 chars): conditions like pass@k, with tools, context length, subset, "reported by <vendor>".
- LANGUAGE: write every free-text field (note, benchmark_name, harness, and name/scope/feature/size/metric of
  new_benchmarks) in English, even when the source is not; use a benchmark's official English name when it has
  one, otherwise translate it. model_raw stays exactly as written in the source.
- If the source compares a vendor's model against competitors, keep competitor numbers only when this
  source ran them itself (independent leaderboard); for vendor launch posts keep only the vendor's own models.
- If an independent page says a number was reported or run by the model's own vendor (copied from a launch post
  or system card, "vendor-run", "self-reported"), add "source_type":"official" to that row.

Return ONLY JSON:
{{"rows":[{{"model":"<id>|null","model_raw":"","effort":"","benchmark":"<id>","benchmark_name":"","score":0,
"unit":"%","higher_better":true,"harness":null,"note":"","date":"YYYY-MM-DD|null","source_type":"official (optional)"}}],
"new_benchmarks":[{{"id":"","name":"","category":"overall|coding|agentic|reasoning|knowledge|multimodal|webdev|3d|long_context|writing|safety|other",
"scope":"<=100 chars, English: what it measures, scale","feature":"<=100 chars, English: what is distinctive, caveats",
"size":"","metric":"","higher_better":true,
"status":"active|saturated|new","url":""}}]}}
{body}"""

WEB_BODY = """
The page may be JavaScript-rendered or blocked for plain HTTP. Use your web tools to read it:
fetch {url}{data_url}. If that fails, search for the same publisher's leaderboard data (same site / its GitHub /
its Hugging Face dataset). Do not use third-party copies unless the hint says so. If nothing can be read,
return {{"rows":[],"new_benchmarks":[],"error":"<why, in English>"}}."""

TEXT_BODY = """
SOURCE TEXT (cleaned page text; tables use | separators):
<<<
{text}
>>>"""

RESOLVE_PROMPT = """You maintain a model registry for an LLM leaderboard. For every vendor it tracks the latest
1-2 generations (gen 0 = current, gen 1 = previous) of EACH size line separately: flagship, mid (~50-250B open
models, "Flash"/"Plus"/"Medium" tiers), small, code. A small/mid model is in scope even when the vendor's flagship
has moved on, as long as it is the newest or previous model of its own size line. Today is {today}.

CURRENT REGISTRY (id: name [vendor, line, gen, released, params]):
{registry}

These model names appeared on leaderboards but are not in the registry:
{names}

For EACH name decide:
- "map": it is an existing model (alias/snapshot/variant naming) -> give "id".
- "new": a real model that is the current or previous generation of its vendor's size line (any size, incl.
  ~100B open-weight models) -> give a full "model": {{"id","name","vendor","family","gen":0|1,"released",
  "open_weights","efforts":[],"default_effort","price_in","price_out","context","aliases":[],
  "tier":"flagship|mid|small|code|other","line":"same as tier unless the vendor has two lines of that tier",
  "params_total_b":null,"params_active_b":null,"arch":"moe|dense|null","url"}}
  and "new_generation": true if it replaces the previous generation OF THE SAME LINE (older models of that line shift down).
  Use web search if you are unsure whether it is real, its size, or its generation.
- "ignore": superseded within its own size line, a third-party fine-tune, unknown/unverifiable, or not an LLM.
Write all free text ("reason", model "name", "family") in English; use the model's official English/Latin name.
Return ONLY JSON: {{"decisions":[{{"raw":"","action":"map|new|ignore","id":null,"model":null,"new_generation":false,"reason":""}}]}}"""

DISCOVER_PROMPT = """You keep an LLM leaderboard current. Today is {today}; the last update was {last}.
Tracked vendors and their newest models:
{registry}

Existing source domains/URLs (do NOT return these again):
{known}

Use web search to find what changed since {since}:
1. New LLM releases from these vendors or important new vendors — ALL size lines (flagship, mid ~50-250B,
   small), open-weight or not. Include total/active params when published.
2. New PRIMARY result pages with numbers for tracked models: official launch posts / model cards / system cards,
   arXiv technical reports, and leaderboards run by the organisation that actually ran the eval (coding,
   agentic, reasoning, multimodal, 3D, long-context, writing, hallucination ...).
Do NOT return aggregators / mirrors that re-publish other people's numbers ({blocked} and similar),
news articles, or generic "best LLM" listicles. If nothing new and primary exists, return empty lists.
Return at most {max_new} sources, most valuable first. Write every free-text field (model and source names,
extract_hint) in English; use official English/Latin names. Return ONLY JSON:
{{"new_models":[{{"id","name","vendor","family","gen":0,"released","open_weights","efforts":[],"price_in","price_out",
"context","aliases":[],"tier","line","params_total_b","params_active_b","arch","url","new_generation":false}}],
"new_sources":[{{"id":"short-dash-id","name":"","url":"","type":"official|third_party|paper|community",
"categories":[],"data_url":null,"refresh":"live|static","extract_hint":"what to extract, how effort is labeled"}}]}}"""

SIGNAL_PROMPT = """Extract usage / reputation signals from this source for an LLM leaderboard.
Source: {name} url: {url}
Hint: {hint}
Allowed signal names: {signals}
KNOWN MODELS (id: name aka aliases): map each entry to an id; skip models not in the list.
{models}
Return ONLY JSON ("note" is short English text or ""):
{{"signals":[{{"model":"<id>","signal":"","value":0,"unit":"","note":""}}]}}
{body}"""

SENTIMENT_PROMPT = """You are measuring the real-world user reputation (word of mouth) of the LLM "{name}" ({vendor})
for a leaderboard. Today is {today}. Window: last {days} days.

Below are posts/comments collected automatically (may contain noise: other models with similar names, bots, news reposts).
Posts can be in any language (e.g. Chinese from V2EX / linux.do / Zhihu): read them all, but write your answer in English.
{web_note}
Judge only genuine first-hand usage opinions about THIS model. Count distinct opinions you actually used (n_samples).
Aspects in [-1,1] (null if no evidence): coding, agent, writing, reasoning, speed, cost, stability (silent quality
drops / "nerfing", inconsistency, rate limits), instruction (instruction following), hallucination.

LANGUAGE: every free-text field is English. praise / complaints: short English tags of 2-5 words each.
summary: one English sentence (<=160 chars). quotes: an English quote goes verbatim into "text" (<=140 chars);
for a quote in any other language, "text" = faithful English translation, "text_orig" = the original wording
(verbatim, <=140 chars) and "translated": true.

TONE (the output is published on a public page): write praise, complaints and summary in neutral, factual language.
Describe what users report, not your verdict: "perceived quality drop (unconfirmed)" rather than "nerfed",
"benchmarks seen as unrepresentative" rather than "benchmark gaming", "weaker reasoning" rather than "dumb".
Attribute third-party claims to their source ("an audit by X reports ..."). Do not quote posts that contain insults,
profanity, or allegations of misconduct (cheating, benchmark gaming, deliberate degradation, faking): count such
opinions in pos/neg/n_samples, but do not include them in "quotes". Never name individual users.

Return ONLY JSON:
{{"pos":0.0,"neg":0.0,"neu":0.0,"n_samples":0,"channels":[],"aspects":{{"coding":null,"agent":null,"writing":null,
"reasoning":null,"speed":null,"cost":null,"stability":null,"instruction":null,"hallucination":null}},
"praise":["<2-5 word English tag>",...],"complaints":["<2-5 word English tag>",...],
"quotes":[{{"text":"<=140 chars verbatim","url":"","channel":"","date":"YYYY-MM-DD"}},
{{"text":"<English translation>","text_orig":"<original wording>","translated":true,"url":"","channel":"","date":"YYYY-MM-DD"}}],
"summary":"<one English sentence>"}}

POSTS:
<<<
{posts}
>>>"""


# ----------------------------------------------------------------------------- updater
class Updater:
    def __init__(self, cfg, store, claude, args):
        self.cfg, self.store, self.claude, self.args = cfg, store, claude, args
        self.reg = Registry(store)
        self.max_gen = cfg['scope']['max_gen']
        self.unmatched = {}           # raw name -> list of (source_id, row)
        self.report = {'ok': [], 'unchanged': [], 'failed': [], 'empty': [], 'skipped': []}
        self.old_best = self._best_map()

    # -- helpers
    def _best_map(self):
        """Best value per (model, benchmark), preferring independent results like the report does:
        third_party/community > paper > official > aggregator, then the best score within that tier."""
        best, tier = {}, {}
        B = self.store.B
        for r in self.store.scores['scores']:
            hb = B.get(r['benchmark'], {}).get('higher_better', True)
            k = (r['model'], r['benchmark'])
            t = SRC_TIER.get(r.get('source_type'), 2)
            if k not in best or t < tier[k] or (t == tier[k] and (r['score'] > best[k]) == hb and r['score'] != best[k]):
                best[k], tier[k] = r['score'], t
        return best

    def select_sources(self):
        srcs = [s for s in self.store.sources['sources'] if s.get('enabled', True)]
        if self.args.only:
            pats = [p.strip() for p in self.args.only.split(',') if p.strip()]

            def match(s):
                return any((s['id'] == p) or (p.endswith('*') and s['id'].startswith(p[:-1])) or p in s['url'] for p in pats)
            return [s for s in srcs if match(s)]
        out = []
        for s in srcs:
            refresh = s.get('refresh') or ('static' if s.get('type') in ('official', 'paper') else 'live')
            if refresh == 'ref':
                continue
            if refresh == 'static' and not self.args.all:
                last = s.get('last_fetch') or '2000-01-01'
                age = (dt.date.today() - dt.date.fromisoformat(last[:10])).days
                if s.get('last_status') not in (None, 'new') and age < self.cfg['update']['static_refresh_days']:
                    self.report['skipped'].append(s['id'])
                    continue
            out.append(s)
        if self.args.limit:
            out = out[: self.args.limit]
        return out

    def _fetch_source(self, s):
        tmo = self.cfg['fetch']['timeout']
        cands = [s.get('url')]
        if s.get('data_url') and (not s.get('data_url_env') or os.environ.get(s['data_url_env'])):
            cands.insert(0, s['data_url'])
        for u in cands:
            if not u or not str(u).startswith('http'):
                continue
            if re.search(r'\.(pdf|xlsx|xls|zip|parquet|png|jpg)(\?|$)', u, re.I):
                continue                      # binary → let Claude's web tools read it
            if 'artificialanalysis.ai/api' in u and os.environ.get('ARTIFICIAL_ANALYSIS_API_KEY'):
                code, ctype, body = fetch(u, tmo, headers={'x-api-key': os.environ['ARTIFICIAL_ANALYSIS_API_KEY']})
            else:
                code, ctype, body = fetch(u, tmo)
            if 'pdf' in (ctype or '') or body[:5] == '%PDF-':
                continue
            if code == 200 and body and not body.startswith('__ERROR__'):
                text = to_text(body, ctype)
                if len(text) >= self.cfg['fetch']['min_text_chars'] and not re.search(
                        r'(just a moment|enable javascript|cf-chl|access denied|captcha)', text[:3000], re.I):
                    return u, text
        return None, None

    def process_source(self, s):
        sid = s['id']
        try:
            used_url, text = self._fetch_source(s)
            h = hashlib.sha1(text.encode()).hexdigest() if text else None
            if text and h == s.get('last_hash') and not self.args.force:
                s['last_fetch'] = TODAY
                s['last_status'] = 'unchanged'
                self.report['unchanged'].append(sid)
                return
            if text:
                with open(os.path.join(CACHE, re.sub(r'[^a-zA-Z0-9_.-]', '_', sid) + '.txt'), 'w', encoding='utf-8') as f:
                    f.write(text)
            if not self.claude.ok:
                s['last_status'] = 'no_claude'
                self.report['failed'].append((sid, 'no Claude backend available'))
                return
            common = dict(name=s.get('name', sid), stype=s.get('type', ''), url=s.get('url'), today=TODAY,
                          hint=s.get('extract_hint') or '-', models=self.reg.models_prompt(self.max_gen),
                          benchmarks=self.reg.bench_prompt(s.get('categories')))
            if text:
                body = TEXT_BODY.format(text=prefilter(text, self.reg.keywords(self.max_gen), self.cfg['fetch']['max_chars']))
                res = self.claude.ask_json(EXTRACT_PROMPT.format(body=body, **common), web=False)
                via = 'fetch'
            else:
                if not self.claude.web:
                    s['last_status'] = 'fetch_failed'
                    self.report['failed'].append((sid, 'fetch failed and web tools are disabled'))
                    return
                body = WEB_BODY.format(url=s.get('url'), data_url=(' or ' + s['data_url']) if s.get('data_url') else '')
                res = self.claude.ask_json(EXTRACT_PROMPT.format(body=body, **common), web=True)
                via = 'claude-web'
            self.apply_result(s, res, via, h)
        except Exception as e:  # keep going
            s['last_status'] = 'error: ' + str(e)[:160]
            self.report['failed'].append((sid, str(e)[:160]))
            log(f'  ✗ {sid}: {str(e)[:160]}')

    def apply_result(self, s, res, via, h=None):
        """Normalise one source's extraction result and merge it into scores.json."""
        sid = s['id']
        rows = self._normalise(s, res if isinstance(res, dict) else {'rows': res})
        s['last_fetch'] = TODAY
        s['last_via'] = via
        if not rows:
            s['last_status'] = 'empty' + (f": {res.get('error')}" if isinstance(res, dict) and res.get('error') else '')
            self.report['empty'].append(sid)
            return 0
        # upsert by (model, benchmark, effort, harness): new values win, rows the page no longer
        # shows are kept (still true as of their date) so a partial extraction never loses data
        # (if the new extraction is at least half as big as before, the page is the new truth: replace)
        key = lambda r: (r['model'], r['benchmark'], r.get('effort'), r.get('harness') or '')
        new_keys = {key(r) for r in rows}
        with self.store.lock:
            old_n = sum(1 for r in self.store.scores['scores'] if r.get('source') == sid)
            mode = res.get('mode') if isinstance(res, dict) else None   # import may force 'upsert' / 'replace'
            replace = mode == 'replace' or (mode != 'upsert' and len(rows) >= 0.5 * old_n)
            keep = [r for r in self.store.scores['scores']
                    if r.get('source') != sid or (not replace and key(r) not in new_keys)]
            self.store.scores['scores'] = keep + rows
        if h:
            s['last_hash'] = h
        s['last_status'] = 'ok'
        s['rows'] = len(rows)
        self.report['ok'].append((sid, len(rows), via))
        log(f'  ✓ {sid}: {len(rows)} rows ({via})')
        return len(rows)

    def _normalise(self, s, res):
        out = []
        B = self.store.B
        for nb in res.get('new_benchmarks') or []:
            if nb.get('id') and nb['id'] not in B:
                txt = ' '.join(str(nb.get(k) or '') for k in ('name', 'scope', 'feature'))
                nb = {**nb, 'added_by': 'auto', 'added': TODAY,
                      'internal': bool(nb.get('internal')) or bool(re.search(r'\b(?:internal|in-house|self-built)\b', txt, re.I)),
                      'leaderboard': nb.get('leaderboard') or (s.get('url') if s.get('type') != 'official' else None)}
                if nb.get('category') not in CATEGORIES + ['meta']:
                    nb['category'] = 'other'
                with self.store.lock:
                    self.store.bench['benchmarks'].append(nb)
                B[nb['id']] = nb
        for r in res.get('rows') or []:
            try:
                score = float(str(r.get('score')).replace('%', '').replace(',', ''))
            except Exception:
                continue
            raw = r.get('model_raw') or r.get('model') or ''
            bid = r.get('benchmark') or ''
            if bid not in B:
                bid = self.reg.balias.get(norm_key(r.get('benchmark_name') or bid), bid)
            if bid not in B:
                nb = {'id': bid or norm_key(r.get('benchmark_name')), 'name': r.get('benchmark_name') or bid,
                      'category': (s.get('categories') or ['other'])[0], 'scope': '', 'feature': '', 'size': '',
                      'metric': r.get('unit', ''), 'higher_better': r.get('higher_better', True), 'status': 'active',
                      'url': s.get('url'), 'leaderboard': s.get('url'), 'added_by': 'auto', 'added': TODAY, 'internal': False}
                if not nb['id']:
                    continue
                with self.store.lock:
                    self.store.bench['benchmarks'].append(nb)
                B[nb['id']] = nb
                bid = nb['id']
            row = {'model': None, 'model_raw': raw, 'effort': norm_effort(r.get('effort'), raw), 'benchmark': bid,
                   'score': score, 'unit': r.get('unit') or B[bid].get('metric') or '%',
                   'higher_better': r.get('higher_better', B[bid].get('higher_better', True)),
                   'source_type': s.get('type', 'third_party'), 'source': s['id'], 'url': s.get('url'),
                   'date': r.get('date') or TODAY, 'harness': norm_harness(r.get('harness')), 'note': (r.get('note') or '')[:120]}
            if row['source_type'] not in SOURCE_TYPES:
                row['source_type'] = 'third_party'
            # a row may be marked LESS independent than its page (an independent board relaying a vendor's number),
            # never more
            rt = str(r.get('source_type') or '').split(' ')[0]
            if rt in SOURCE_TYPES and SRC_TIER[rt] > SRC_TIER[row['source_type']]:
                row['source_type'] = rt
            mid = self.reg.model_id(r.get('model'), raw)
            if mid:
                row['model'] = mid
                if self.reg.in_scope(mid, self.max_gen):
                    out.append(row)
            elif raw and not self.reg.is_ignored(raw):
                with self.store.lock:
                    self.unmatched.setdefault(strip_effort(raw), []).append((s['id'], row))
        return out

    def resolve_unmatched(self):
        if not self.unmatched or not self.claude.ok:
            return
        names = sorted(self.unmatched)[:120]
        log(f'\n▶ {len(self.unmatched)} new model names; asking Claude to resolve them…')
        reg = '\n'.join(f"{m['id']}: {m['name']} [{m['vendor']}, {model_line(m)}, gen{m.get('gen', 0)}, "
                        f"{m.get('released', '?')}, {fmt_params(m)}]"
                        for m in self.store.models['models'] if m.get('gen', 0) <= 2)
        listing = '\n'.join(f"- {n}  (seen in: {', '.join(sorted(set(x[0] for x in self.unmatched[n])))[:120]})" for n in names)
        try:
            res = self.claude.ask_json(RESOLVE_PROMPT.format(today=TODAY, registry=reg, names=listing), web=True)
        except Exception as e:
            log('  resolve failed:', e)
            return
        added, mapped, ignored = [], 0, 0
        for d in res.get('decisions', []):
            raw = d.get('raw')
            if raw not in self.unmatched:
                continue
            act = d.get('action')
            if act == 'new' and d.get('model', {}).get('id'):
                m = d['model']
                if m['id'] not in self.store.M:
                    if d.get('new_generation'):
                        bump_line(self.store, m)
                    m = {**m, 'aliases': sorted(set((m.get('aliases') or []) + [raw, m.get('name', raw)])),
                         'added_by': 'auto', 'added': TODAY, 'hidden': False}
                    m.setdefault('gen', 0)
                    self.store.models['models'].append(m)
                    added.append(m['name'])
                d['id'] = m['id']
                act = 'map'
            if act == 'map' and d.get('id') in self.store.M:
                mid = d['id']
                m = self.store.M[mid]
                m['aliases'] = sorted(set((m.get('aliases') or []) + [raw]))
                for sid, row in self.unmatched[raw]:
                    row['model'] = mid
                    if self.reg.in_scope(mid, self.max_gen):
                        self.store.scores['scores'].append(row)
                mapped += 1
            elif act == 'ignore':
                self.store.models.setdefault('ignored_names', []).append(raw)
                ignored += 1
        self.reg.rebuild()
        log(f'  mapped {mapped}, new models {len(added)} {added[:8]}, ignored {ignored}')
        if added:
            self.store.changelog['entries'].insert(0, {'date': TODAY, 'kind': 'new_model', 'text': 'New model: ' + ', '.join(added)})

    def discover(self):
        if not (self.claude.ok and self.claude.web):
            return []
        log('\n▶ Asking Claude to search for new models / sources…')
        last = self.store.models.get('updated') or TODAY
        since = (dt.date.fromisoformat(last[:10]) - dt.timedelta(days=5)).isoformat()
        reg_lines = []
        by_v = {}
        for m in self.store.models['models']:
            if m.get('gen', 0) == 0:
                by_v.setdefault(m['vendor'], []).append(f"{m['name']} ({model_line(m)}, {m.get('released', '?')}, {fmt_params(m)})")
        for v, ms in sorted(by_v.items()):
            reg_lines.append(f"{v}: {'; '.join(ms[:10])}")
        known = '\n'.join(sorted({s['url'] for s in self.store.sources['sources']})[:400])
        blocked = self.cfg['update'].get('discover_block_domains', [])
        try:
            res = self.claude.ask_json(DISCOVER_PROMPT.format(today=TODAY, last=last, since=since, registry='\n'.join(reg_lines),
                                                              known=known, max_new=10, blocked=', '.join(blocked)), web=True)
        except Exception as e:
            log('  discover failed:', e)
            return []
        new_src = []
        urls = {s['url'] for s in self.store.sources['sources']}
        for m in res.get('new_models') or []:
            if m.get('id') and m['id'] not in self.store.M and m.get('name'):
                if m.pop('new_generation', False):
                    bump_line(self.store, m)
                m.update(added_by='discover', added=TODAY, hidden=False)
                m.setdefault('gen', 0)
                m['aliases'] = sorted(set((m.get('aliases') or []) + [m['name']]))
                self.store.models['models'].append(m)
                self.store.changelog['entries'].insert(0, {'date': TODAY, 'kind': 'new_model', 'text': f"New model: {m['name']} ({m.get('vendor', '')})"})
                log(f"  + model {m['name']}")
        for s in res.get('new_sources') or []:
            if not s.get('url') or s['url'] in urls:
                continue
            host = urllib.parse.urlparse(s['url']).netloc.lower()
            if any(host.endswith(b) for b in blocked) or s.get('type') == 'aggregator':
                log(f"  · skipping aggregator {s['url']}")
                continue
            sid = slug(s.get('id') or s.get('name') or host)[:40] or hashlib.sha1(s['url'].encode()).hexdigest()[:10]
            while sid in self.store.S:
                sid += '-2'
            s.update(id=sid, enabled=True, added=TODAY, added_by='discover', last_hash=None, last_status='new')
            s.setdefault('type', 'third_party')
            s.setdefault('categories', ['other'])
            self.store.sources['sources'].append(s)
            new_src.append(s)
            self.store.changelog['entries'].insert(0, {'date': TODAY, 'kind': 'new_source', 'text': f"New source: {s.get('name') or s['url']}", 'url': s['url']})
            log(f"  + source {s.get('name')} {s['url']}")
        self.reg.rebuild()
        return new_src

    def update_signals(self):
        if not self.claude.ok:
            return
        log('\n▶ Usage / degradation signals…')
        for sig in self.cfg.get('signal_sources', []):
            code, ctype, body = fetch(sig['url'], self.cfg['fetch']['timeout'])
            text = to_text(body, ctype) if code == 200 else ''
            prompt_body = (TEXT_BODY.format(text=prefilter(text, self.reg.keywords(self.max_gen), 60000)) if len(text) > 500
                           else WEB_BODY.format(url=sig['url'], data_url=''))
            try:
                res = self.claude.ask_json(SIGNAL_PROMPT.format(name=sig['id'], url=sig['url'], hint=sig.get('hint', ''),
                                                                signals=', '.join(sig.get('signals', [])),
                                                                models=self.reg.models_prompt(self.max_gen), body=prompt_body),
                                           web=len(text) <= 500)
            except Exception as e:
                log(f'  ✗ {sig["id"]}: {e}')
                continue
            new = []
            for x in res.get('signals') or []:
                mid = self.reg.model_id(x.get('model'), x.get('model') or '')
                if mid and isinstance(x.get('value'), (int, float)):
                    new.append({'model': mid, 'signal': x.get('signal'), 'value': x['value'], 'unit': x.get('unit', ''),
                                'url': sig['url'], 'date': TODAY, 'note': x.get('note', ''), 'source': sig['id']})
            if new:
                names = {x['signal'] for x in new}
                self.store.rep['signals'] = [x for x in self.store.rep.get('signals', [])
                                             if not (x.get('source', '') == sig['id'] or (x['signal'] in names and x.get('url') == sig['url']))] + new
                log(f'  ✓ {sig["id"]}: {len(new)} signals')

    # -- reputation
    def _posts_hn(self, terms, since_ts):
        posts = []
        for t in terms[:2]:
            for tag in ('comment', 'story'):
                q = urllib.parse.quote(f'"{t}"')
                url = (f'https://hn.algolia.com/api/v1/search_by_date?query={q}&tags={tag}'
                       f'&numericFilters=created_at_i>{since_ts}&hitsPerPage=60')
                code, _, body = fetch(url, 30)
                if code != 200:
                    continue
                try:
                    hits = json.loads(body).get('hits', [])
                except Exception:
                    continue
                for h in hits:
                    txt = h.get('comment_text') or h.get('story_text') or h.get('title') or ''
                    txt = html.unescape(re.sub(r'<[^>]+>', ' ', txt))
                    if t.lower() not in txt.lower():
                        continue
                    posts.append({'channel': 'hn', 'date': (h.get('created_at') or '')[:10],
                                  'url': f"https://news.ycombinator.com/item?id={h.get('objectID')}", 'text': txt[:700]})
        return posts

    def _reddit_token(self):
        cid, sec = os.environ.get('REDDIT_CLIENT_ID'), os.environ.get('REDDIT_CLIENT_SECRET')
        if not (cid and sec):
            return None
        if getattr(self, '_rtok', None):
            return self._rtok
        import base64
        auth = base64.b64encode(f'{cid}:{sec}'.encode()).decode()
        code, _, body = fetch('https://www.reddit.com/api/v1/access_token', 30,
                              headers={'Authorization': 'Basic ' + auth, 'User-Agent': 'llm-bench/1.0'},
                              data=b'grant_type=client_credentials', method='POST')
        try:
            self._rtok = json.loads(body)['access_token']
        except Exception:
            self._rtok = None
        return self._rtok

    def _posts_reddit(self, terms, days):
        tok = self._reddit_token()
        subs = '+'.join(self.cfg['reputation']['subreddits'])
        posts = []
        for t in terms[:2]:
            q = urllib.parse.quote(f'"{t}"')
            t_window = 'month' if days > 7 else 'week'
            if tok:
                url = f'https://oauth.reddit.com/r/{subs}/search?q={q}&restrict_sr=1&sort=new&t={t_window}&limit=60'
                code, _, body = fetch(url, 30, headers={'Authorization': 'Bearer ' + tok, 'User-Agent': 'llm-bench/1.0'})
            else:
                url = f'https://www.reddit.com/r/{subs}/search.json?q={q}&restrict_sr=1&sort=new&t={t_window}&limit=60'
                code, _, body = fetch(url, 30, headers={'User-Agent': 'llm-bench/1.0'})
            if code != 200:
                continue
            try:
                for c in json.loads(body)['data']['children']:
                    d = c['data']
                    posts.append({'channel': 'reddit', 'date': dt.datetime.fromtimestamp(d.get('created_utc', 0), dt.timezone.utc).date().isoformat(),
                                  'url': 'https://www.reddit.com' + d.get('permalink', ''),
                                  'text': (d.get('title', '') + ' — ' + (d.get('selftext') or ''))[:700]})
            except Exception:
                pass
        return posts

    def _posts_github(self, terms, since_date):
        posts = []
        hdr = {'Accept': 'application/vnd.github+json'}
        if os.environ.get('GITHUB_TOKEN'):
            hdr['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
        repos = ' '.join('repo:' + r for r in self.cfg['reputation']['github_repos'])
        q = urllib.parse.quote(f'"{terms[0]}" {repos} created:>{since_date}')
        code, _, body = fetch(f'https://api.github.com/search/issues?q={q}&sort=created&order=desc&per_page=40', 30, headers=hdr)
        if code == 200:
            try:
                for it in json.loads(body).get('items', []):
                    posts.append({'channel': 'github', 'date': it.get('created_at', '')[:10], 'url': it.get('html_url'),
                                  'text': (it.get('title', '') + ' — ' + (it.get('body') or ''))[:600]})
            except Exception:
                pass
        return posts

    def _posts_v2ex(self, terms):
        posts = []
        q = urllib.parse.quote(terms[0])
        code, _, body = fetch(f'https://www.sov2ex.com/api/search?q={q}&sort=created&size=30', 30)
        if code == 200:
            try:
                for h in json.loads(body).get('hits', []):
                    s = h.get('_source', {})
                    posts.append({'channel': 'v2ex', 'date': (s.get('created') or '')[:10],
                                  'url': f"https://www.v2ex.com/t/{s.get('id')}",
                                  'text': (s.get('title', '') + ' — ' + (s.get('content') or ''))[:600]})
            except Exception:
                pass
        return posts

    def rep_models(self):
        """Current-gen models of every size line, most-covered first (cap: reputation.max_models)."""
        cur = {s['model'] for s in self.store.rep.get('sentiment', [])}
        cnt = {}
        for r in self.store.scores['scores']:
            cnt[r['model']] = cnt.get(r['model'], 0) + 1
        out = [m for m in self.store.models['models'] if not m.get('hidden') and m.get('gen', 0) == 0
               and (m['id'] in cur or cnt.get(m['id'], 0) >= 15)]
        out.sort(key=lambda m: (m['id'] not in cur, -cnt.get(m['id'], 0)))
        return out[: self.cfg['reputation'].get('max_models', 40)]

    def search_terms(self, m):
        if m.get('search_terms'):
            return m['search_terms']
        name = m['name']
        terms = [name]
        short = re.sub(r'^(Claude|Google|OpenAI|Meta|xAI|Mistral|Alibaba)\s+', '', name)
        if short != name and len(short) >= 6:
            terms.insert(0, short)
        return terms

    def update_reputation(self):
        if not self.claude.ok:
            return
        rc = self.cfg['reputation']
        days = rc['days']
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        since_ts, since_date = int(since.timestamp()), since.date().isoformat()
        models = self.rep_models()
        if self.args.only_rep:
            want = set(self.args.only_rep.split(','))
            models = [m for m in models if m['id'] in want]
        log(f'\n▶ Reputation: {len(models)} models (HN / Reddit / GitHub / V2EX' + (' + Claude web search' if rc.get('use_claude_web') and self.claude.web else '') + ')')

        def one(m):
            terms = self.search_terms(m)
            posts = []
            ch = set(rc['channels'])
            try:
                if 'hn' in ch:
                    posts += self._posts_hn(terms, since_ts)
                if 'reddit' in ch:
                    posts += self._posts_reddit(terms, days)
                if 'github' in ch:
                    posts += self._posts_github(terms, since_date)
                if 'v2ex' in ch:
                    posts += self._posts_v2ex(terms)
            except Exception:
                pass
            seen, uniq = set(), []
            for p in sorted(posts, key=lambda p: p['date'], reverse=True):
                k = p['url']
                if k not in seen:
                    seen.add(k)
                    uniq.append(p)
            uniq = uniq[: rc['max_posts_per_model']]
            web = bool(rc.get('use_claude_web')) and self.claude.web
            if not uniq and not web:
                return m['id'], None, 0
            web_note = ('You may ALSO use web search (reddit, x.com, linux.do, zhihu, v2ex, blogs) to find more '
                        f'first-hand opinions from the last {days} days. Cite real URLs only.') if web else ''
            ptxt = '\n'.join(f"[{p['channel']} {p['date']}] {p['url']}\n{p['text']}" for p in uniq) or '(none collected)'
            try:
                res = self.claude.ask_json(SENTIMENT_PROMPT.format(name=m['name'], vendor=m['vendor'], today=TODAY, days=days,
                                                                   web_note=web_note, posts=ptxt[:90000]), web=web)
            except Exception as e:
                log(f'  ✗ {m["name"]}: {e}')
                return m['id'], None, len(uniq)
            return m['id'], res, len(uniq)

        results = []
        with cf.ThreadPoolExecutor(max_workers=self.cfg['claude']['workers']) as ex:
            for mid, res, n in ex.map(one, models):
                if res and isinstance(res, dict) and res.get('n_samples', 0) > 0:
                    res.update(model=mid, date=TODAY, method=f'auto: {n} fetched posts + Claude review')
                    results.append(res)
                    log(f"  ✓ {self.store.M[mid]['name']}: n={res.get('n_samples')} +{res.get('pos', 0):.0%} -{res.get('neg', 0):.0%}")
                else:
                    log(f"  · {self.store.M[mid]['name']}: not enough evidence ({n} posts fetched)")
        # a much thinner fresh sample should not overwrite a richer recent one
        old = {x['model']: x for x in self.store.rep.get('sentiment', [])}
        kept = []
        for r in results:
            o = old.get(r['model'])
            on = (o or {}).get('n_samples') or 0
            if o and r.get('n_samples', 0) < on and r.get('n_samples', 0) < max(8, 0.3 * on):
                kept.append(self.store.M[r['model']]['name'])
                continue
            r['prev'] = {'date': o.get('date'), 'pos': o.get('pos'), 'neg': o.get('neg'), 'n': o.get('n_samples')} if o else None
            r.pop('prev') if not o else None
        results = [r for r in results if self.store.M[r['model']]['name'] not in kept]
        if kept:
            log(f'  (sample much smaller than last time; keeping previous reputation: {", ".join(kept)})')
        if results:
            got = {r['model'] for r in results}
            self.store.rep['sentiment'] = [x for x in self.store.rep.get('sentiment', []) if x['model'] not in got] + results
            hist = self.store.rep.setdefault('history', [])
            hist[:] = [h for h in hist if not (h['date'] == TODAY and h['model'] in got)]
            hist += [{'date': TODAY, 'model': r['model'], 'pos': r.get('pos'), 'neg': r.get('neg'), 'n': r.get('n_samples')} for r in results]

    # -- changelog / history
    def finish(self):
        """Changelog: one summary line + the newsworthy items only (no speed/cost churn, no flood of backfilled rows)."""
        new_best = self._best_map()
        M, B = self.store.M, self.store.B
        in_scope = lambda mid: M.get(mid, {}).get('gen', 0) <= self.max_gen and not M.get(mid, {}).get('hidden')
        old_models = {k[0] for k in self.old_best}
        old_bench = {k[1] for k in self.old_best}
        changes = []
        for k, v in new_best.items():
            if B.get(k[1], {}).get('category') == 'meta' or not in_scope(k[0]):
                continue
            if k not in self.old_best:
                changes.append(('new', k, None, v))
            elif abs(self.old_best[k] - v) > 1e-9:
                changes.append(('chg', k, self.old_best[k], v))
        ent = self.store.changelog['entries']
        new_items = [c for c in changes if c[0] == 'new']
        chg_items = [c for c in changes if c[0] == 'chg']
        items = []
        # 1) a model's first scores  2) benchmarks seen for the first time (vendor-internal ones skipped, long lists
        # collapsed)  3) biggest value changes
        for mid in sorted({c[1][0] for c in new_items if c[1][0] not in old_models}):
            n = sum(1 for c in new_items if c[1][0] == mid)
            items.append({'date': TODAY, 'kind': 'new_scores', 'text': f"{M.get(mid, {}).get('name', mid)} gets its first scores ({n})"})
        nb = [bid for bid in sorted({c[1][1] for c in new_items if c[1][1] not in old_bench}) if not B.get(bid, {}).get('internal')]
        nb.sort(key=lambda bid: -sum(1 for c in new_items if c[1][1] == bid))
        for bid in nb[:6]:
            n = sum(1 for c in new_items if c[1][1] == bid)
            items.append({'date': TODAY, 'kind': 'new_bench', 'text': f"New benchmark: {B.get(bid, {}).get('name', bid)} ({n} model{'s' if n != 1 else ''})"})
        if len(nb) > 6:
            items.append({'date': TODAY, 'kind': 'new_bench', 'text': f"+{len(nb) - 6} more new benchmarks: " + ', '.join(B.get(b, {}).get('name', b) for b in nb[6:])})
        rel = lambda c: abs(c[3] - c[2]) / (abs(c[2]) or 1)
        for kind, (mid, bid), old, v in sorted(chg_items, key=rel, reverse=True)[:12]:
            items.append({'date': TODAY, 'kind': 'chg', 'text': f"{M.get(mid, {}).get('name', mid)} · {B.get(bid, {}).get('name', bid)}: {old:g} → {v:g}"})
        if changes:
            ent[0:0] = [{'date': TODAY, 'kind': 'summary',
                         'text': f'This update: {len(new_items)} new scores, {len(chg_items)} best-score changes'}] + items[:25]
        self.store.changelog['entries'] = ent[:400]
        snap = [{'m': k[0], 'b': k[1], 'v': v} for k, v in new_best.items()]
        jsave(os.path.join(HIST, f'{TODAY}.json'), {'date': TODAY, 'best': snap}, indent=0)


# ----------------------------------------------------------------------------- ranking helpers
# Versions / variants of one evaluation count as one vote in the category ranking.
FAMILY_RULES = [
    (r'^hle', 'hle'), (r'^terminal-bench-(?!science)', 'terminal-bench'), (r'^swe-bench-pro', 'swe-bench-pro'),
    (r'^aa-intelligence-index', 'aa-intelligence-index'), (r'^aa-omniscience(?!-hallucination)', 'aa-omniscience'),
    (r'^critpt', 'critpt'), (r'^frontiercode', 'frontiercode'), (r'^frontiermath', 'frontiermath'), (r'^gdpval', 'gdpval'),
    (r'^osworld', 'osworld'), (r'^tau\d?-?bench|^tau\d', 'tau-bench'), (r'^(contextarena-)?mrcr', 'mrcr'),
    (r'^programbench', 'programbench'), (r'^cursorbench', 'cursorbench'), (r'^livecodebench', 'livecodebench'),
    (r'^mmmu', 'mmmu'), (r'^cwe-bench', 'cwe-bench'), (r'^3jsbench', '3jsbench'), (r'^benchcad', 'benchcad'),
    (r'^swe-atlas', 'swe-atlas'), (r'^healthbench', 'healthbench'), (r'^biomysterybench', 'biomysterybench'),
    (r'^graphwalks', 'graphwalks'), (r'^lechmazur-generalization', 'lechmazur-generalization'),
    (r'^matharena-apex', 'matharena-apex'), (r'^aime', 'aime'), (r'^hmmt', 'hmmt'), (r'^arc-agi', 'arc-agi'),
    (r'^automationbench', 'automationbench'), (r'^gdp-pdf', 'gdp-pdf'), (r'^toolathlon', 'toolathlon'),
    (r'^(vals-)?finance-agent', 'finance-agent'), (r'^chartography', 'chartography'), (r'^omnidocbench', 'omnidocbench'),
    (r'^simpleqa', 'simpleqa'), (r'^eqbench-(creative|longform)', 'eqbench-creative'),
]


def bench_family(bid):
    for pat, fam in FAMILY_RULES:
        if re.search(pat, bid):
            return fam
    return bid


def publisher_of(url):
    """Who ran the evaluation: registered domain, or the org for GitHub / Hugging Face hosted data."""
    u = urllib.parse.urlparse(url or '')
    host = re.sub(r'^www\.', '', u.netloc.lower())
    parts = [p for p in u.path.split('/') if p]
    if host in ('github.com', 'raw.githubusercontent.com', 'gist.github.com'):
        return 'github/' + (parts[0].lower() if parts else '')
    if host.endswith('huggingface.co'):
        q = urllib.parse.parse_qs(u.query).get('dataset')
        if q:
            return 'hf/' + q[0].split('/')[0].lower()
        if parts and parts[0] in ('datasets', 'spaces', 'api') and len(parts) > 1:
            parts = parts[1:] if parts[0] != 'api' else parts[2:]
        return 'hf/' + (parts[0].lower() if parts else '')
    if host.endswith('github.io'):
        return host
    return '.'.join(host.split('.')[-2:])


# ----------------------------------------------------------------------------- render
ECHARTS_PATH = os.path.join(ROOT, 'assets', 'echarts.min.js')
INDEX_PATH = os.path.join(ROOT, 'index.html')
DATA_JS_PREFIX = 'window.LLM_DATA = '


def ensure_echarts():
    if not os.path.exists(ECHARTS_PATH):
        os.makedirs(os.path.dirname(ECHARTS_PATH), exist_ok=True)
        code, _, body = fetch('https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js', 60)
        if code == 200:
            open(ECHARTS_PATH, 'w', encoding='utf-8').write(body)
    return os.path.exists(ECHARTS_PATH)


def render(store, cfg, claude_desc=''):
    """Write the report data: data.js (loaded by index.html, works when opened by double-click) and data.json
    (the same payload as plain JSON for other tools). index.html itself is static and is not rewritten."""
    ensure_echarts()
    max_gen = cfg['scope']['max_gen']
    M = store.M
    models = [m for m in store.models['models'] if m.get('gen', 0) <= max_gen]
    mids = {m['id'] for m in models}
    srcs = store.sources['sources']
    sidx = {s['id']: i for i, s in enumerate(srcs)}
    midx = {m['id']: i for i, m in enumerate(models)}
    bench = [b for b in store.bench['benchmarks']]
    bidx = {b['id']: i for i, b in enumerate(bench)}
    rows = []
    for r in store.scores['scores']:
        if r['model'] not in mids or r['benchmark'] not in bidx:
            continue
        src = r.get('source')
        surl = srcs[sidx[src]]['url'] if src in sidx else None
        rows.append([midx[r['model']], bidx[r['benchmark']], r['score'], r.get('effort') or 'unknown',
                     sidx.get(src, -1), r.get('source_type', ''), r.get('harness') or '', (r.get('note') or '')[:100],
                     (r.get('date') or '')[:10], r.get('url') if r.get('url') != surl else '', r.get('model_raw') or ''])
    cover = {}
    for r in rows:
        cover.setdefault(r[1], set()).add(r[0])
    used_b = set(cover)
    src_rows = {}
    for r in rows:
        src_rows[r[4]] = src_rows.get(r[4], 0) + 1
    payload = {
        'meta': {'updated': store.scores.get('updated') or TODAY, 'generated': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
                 'backend': claude_desc, 'max_gen': max_gen, 'title': cfg['report']['title'],
                 'focus': cfg['report'].get('focus_models', [])},
        'models': [{k: m.get(k) for k in ('id', 'name', 'vendor', 'family', 'gen', 'released', 'open_weights', 'efforts',
                                            'default_effort', 'price_in', 'price_out', 'context', 'tier', 'line', 'url', 'hidden',
                                            'note', 'params_total_b', 'params_active_b', 'arch', 'params_note')}
                   | {'restricted': bool(m.get('restricted'))}
                   for m in models],
        'benchmarks': [{**{k: b.get(k) for k in ('id', 'name', 'category', 'scope', 'feature', 'size', 'metric',
                                                   'higher_better', 'status', 'url', 'leaderboard', 'internal', 'superseded_by')},
                        'family': b.get('family') or bench_family(b['id']),
                        'n': len(cover.get(i, ()))} for i, b in enumerate(bench)],
        'sources': [{**{k: s.get(k) for k in ('id', 'name', 'url', 'type', 'categories', 'data_url', 'format',
                                                'last_fetch', 'last_status', 'added_by', 'refresh')},
                     'publisher': s.get('publisher') or publisher_of(s.get('url')),
                     'rows': src_rows.get(i, 0)} for i, s in enumerate(srcs)],
        'cols': ['m', 'b', 'v', 'e', 's', 't', 'h', 'note', 'date', 'url', 'raw'],
        'scores': rows,
        'reputation': store.rep,
        'harness': store.harness,
        'changelog': store.changelog.get('entries', [])[:60],
        'notes': store.notes,
        'corrections': store.corrections,
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    rc = cfg['report']
    js_path, json_path = os.path.join(ROOT, rc['data_js']), os.path.join(ROOT, rc['data_json'])
    for path, text in ((json_path, blob + '\n'), (js_path, DATA_JS_PREFIX + blob.replace('</', '<\\/') + ';\n')):
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(path + '.tmp', path)
    update_readme_badges(sum(1 for m in models if not m.get('hidden')), len(used_b), len([1 for v in src_rows if v >= 0]))
    return js_path, len(rows), len(models), len(used_b)


README_BADGES = ('<!-- badges:start -->', '<!-- badges:end -->')


def update_readme_badges(n_models, n_bench, n_sources):
    """Keep the count badges in README.md in step with the report header (rewrites only the marked block)."""
    path = os.path.join(ROOT, 'README.md')
    if not os.path.exists(path):
        return
    text = open(path, encoding='utf-8').read()
    a, b = README_BADGES
    if a not in text or b not in text:
        return
    badge = lambda label, msg, color: f'![{label} {msg}](https://img.shields.io/badge/{label}-{msg.replace(" ", "%20")}-{color})'
    block = ' '.join([badge('updated', 'every 2 days', '4c78a8'), badge('code', 'MIT', '555'), badge('data', 'CC BY 4.0', '555'),
                      badge('models', str(n_models), 'd9734e'), badge('benchmarks', str(n_bench), 'd9734e'),
                      badge('sources', str(n_sources), 'd9734e')])
    new = text[:text.index(a) + len(a)] + '\n' + block + '\n' + text[text.index(b):]
    if new != text:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new)


def build_standalone(cfg, out=None):
    """One self-contained HTML file (page + ECharts + data inlined) for emailing or uploading as a single file."""
    out = os.path.join(ROOT, out or cfg['report']['standalone'])
    page = open(INDEX_PATH, encoding='utf-8').read()
    data_js = open(os.path.join(ROOT, cfg['report']['data_js']), encoding='utf-8').read()
    echarts_js = open(ECHARTS_PATH, encoding='utf-8').read() if ensure_echarts() else ''
    tags = ('<script src="assets/echarts.min.js"></script>', '<script src="data.js"></script>')
    for t in tags:
        if t not in page:
            raise SystemExit(f'index.html is missing the tag {t}')
    page = page.replace(tags[0], '<script>' + echarts_js.replace('</script', '<\\/script') + '</script>')
    page = page.replace(tags[1], '<script>' + data_js + '</script>')
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(page)
    return out


# ----------------------------------------------------------------------------- commands
def cmd_update(args, cfg):
    store = Store()
    try:
        claude = Claude(cfg)
    except ClaudeError as e:
        sys.exit(str(e))
    log(f'Claude backend: {claude.describe()}')
    if not claude.ok:
        log('⚠ No Claude backend (set ANTHROPIC_API_KEY, or install Claude Code and log in). Only rewriting the report data.')
    up = Updater(cfg, store, claude, args)
    t0 = time.time()
    if claude.ok and cfg['update']['discover'] and not args.no_discover and not args.only and not args.skip_sources:
        up.discover()
    if not args.skip_sources:
        srcs = up.select_sources()
        log(f'\n▶ Fetching {len(srcs)} sources (skipped {len(up.report["skipped"])} recently fetched static sources; --all re-fetches them)')
        with cf.ThreadPoolExecutor(max_workers=max(1, cfg['claude']['workers'])) as ex:
            list(ex.map(up.process_source, srcs))
        up.resolve_unmatched()
    if claude.ok and cfg['update']['reputation'] and not args.no_reputation and (not args.only or args.only_rep):
        up.update_signals()
        up.update_reputation()
    up.finish()
    store.save()
    path, n, nm, nb = render(store, cfg, claude.describe())
    r = up.report
    log(f"\nDone in {time.time() - t0:.0f}s · updated {len(r['ok'])} · unchanged {len(r['unchanged'])} · empty {len(r['empty'])} · "
        f"failed {len(r['failed'])} · {claude.calls} Claude calls" + (f" ≈ ${claude.cost:.2f}" if claude.cost else ''))
    for sid, why in r['failed'][:15]:
        log(f'  ✗ {sid}: {why}')
    log(f'Report data: {path}  ({nm} models · {nb} benchmarks · {n} scores)')


def cmd_import(args, cfg):
    """Merge results collected outside the fetcher (another machine, a browser, a research agent).
    A row may carry "source_type": "official" when an independent page relays a vendor-reported number.
    File format: {"sources": {"<source id>": {"status": "ok|unchanged|failed", "rows": [...], "new_benchmarks": [...],
    "note": ""}}, "new_models": [Model...], "new_sources": [Source...], "sentiment": [...], "signals": [...]}"""
    store = Store()
    try:
        claude = Claude(cfg)
    except ClaudeError:
        claude = Claude({**cfg, 'claude': {**cfg['claude'], 'backend': 'off'}})
    args.only = args.force = args.all = None
    up = Updater(cfg, store, claude, args)
    total = {'ok': 0, 'unchanged': 0, 'failed': 0}
    for path in args.files:
        doc = jload(path, {})
        for m in doc.get('new_models') or []:
            if m.get('id') and m['id'] not in store.M and m.get('name'):
                if m.pop('new_generation', False):
                    bump_line(store, m)
                m.update(added_by='import', added=TODAY, hidden=m.get('hidden', False))
                m.setdefault('gen', 0)
                m.setdefault('line', m.get('tier') or 'other')
                m['aliases'] = sorted(set((m.get('aliases') or []) + [m['name']]))
                store.models['models'].append(m)
                store.changelog['entries'].insert(0, {'date': TODAY, 'kind': 'new_model',
                                                      'text': f"New model: {m['name']} ({m.get('vendor', '')})", 'url': m.get('url')})
                log(f"  + model {m['name']}")
        for x in doc.get('model_updates') or []:          # field fixes / status changes for tracked models
            m = store.M.get(x.get('id'))
            if not m:
                continue
            became_open = x.get('open_weights') and not m.get('open_weights')
            for k, v in x.items():
                if k != 'id' and k in ('open_weights', 'released', 'price_in', 'price_out', 'context', 'params_total_b',
                                       'params_active_b', 'arch', 'url', 'note', 'efforts', 'aliases', 'hidden', 'params_note'):
                    m[k] = sorted(set((m.get('aliases') or []) + v)) if k == 'aliases' else v
            if became_open:
                store.changelog['entries'].insert(0, {'date': TODAY, 'kind': 'status',
                                                      'text': f"{m['name']} now open weights", 'url': x.get('url')})
            log(f"  ~ model {m['name']} updated: {', '.join(k for k in x if k != 'id')}")
        known_urls = {x['url'] for x in store.sources['sources']}
        for x in doc.get('new_sources') or []:
            if x.get('id') and x['id'] not in store.S and x.get('url') not in known_urls:
                x.update(enabled=True, added=TODAY, added_by='import', last_hash=None, last_status='new')
                x.setdefault('type', 'third_party')
                x.setdefault('refresh', 'live')
                store.sources['sources'].append(x)
                store.changelog['entries'].insert(0, {'date': TODAY, 'kind': 'new_source',
                                                      'text': f"New source: {x.get('name') or x['url']}", 'url': x['url']})
        up.reg.rebuild()
        S = store.S
        for sid, res in (doc.get('sources') or {}).items():
            s = S.get(sid)
            if not s:
                log(f'  ? unknown source {sid}, skipped')
                continue
            st = res.get('status', 'ok')
            if st == 'unchanged':
                s.update(last_fetch=TODAY, last_status='unchanged', last_via='import')
                total['unchanged'] += 1
            elif st == 'failed' or not res.get('rows'):
                s.update(last_fetch=TODAY, last_status='failed: ' + (res.get('note') or '')[:100], last_via='import')
                total['failed'] += 1
            else:
                up.apply_result(s, res, 'import')
                total['ok'] += 1
        if doc.get('sentiment'):
            old = {x['model']: x for x in store.rep.get('sentiment', [])}
            got = []
            for r in doc['sentiment']:
                if not isinstance(r, dict):
                    continue
                mid = up.reg.model_id(r.get('model'), r.get('model') or '')
                if not mid:
                    continue
                o = old.get(mid)
                on = (o or {}).get('n_samples') or 0
                if o and r.get('n_samples', 0) < on and r.get('n_samples', 0) < max(8, 0.3 * on):
                    continue          # a much thinner fresh sample doesn't overwrite a richer one
                r.update(model=mid, date=r.get('date') or TODAY)
                got.append(r)
            ids = {r['model'] for r in got}
            store.rep['sentiment'] = [x for x in store.rep.get('sentiment', []) if x['model'] not in ids] + got
            hist = store.rep.setdefault('history', [])
            hist[:] = [x for x in hist if not (x['date'] == TODAY and x['model'] in ids)]
            hist += [{'date': TODAY, 'model': r['model'], 'pos': r.get('pos'), 'neg': r.get('neg'), 'n': r.get('n_samples')} for r in got]
            log(f'  reputation: updated {len(got)} models')
        if doc.get('signals'):
            sigs = []
            for x in doc['signals']:
                if not isinstance(x, dict):
                    continue
                mid = up.reg.model_id(x.get('model'), x.get('model') or '')
                if mid and isinstance(x.get('value'), (int, float)):
                    sigs.append({**x, 'model': mid, 'date': x.get('date') or TODAY})
            keyf = lambda x: (x['model'], x['signal'])
            newk = {keyf(x) for x in sigs}
            store.rep['signals'] = [x for x in store.rep.get('signals', []) if keyf(x) not in newk] + sigs
            log(f'  signals: {len(sigs)}')
    if up.unmatched:
        if claude.ok:
            up.resolve_unmatched()
        else:
            log(f'  {len(up.unmatched)} unmatched model names (no Claude backend, not resolved): {list(up.unmatched)[:10]}')
    up.finish()
    store.save()
    path, n, nm, nb = render(store, cfg, claude.describe())
    log(f"Import done: updated {total['ok']} · unchanged {total['unchanged']} · failed {total['failed']}")
    log(f'Report data: {path}  ({nm} models · {nb} benchmarks · {n} scores)')


def cmd_render(args, cfg):
    store = Store()
    path, n, nm, nb = render(store, cfg)
    log(f'Report data: {path}  ({nm} models · {nb} benchmarks · {n} scores) — open index.html to view')
    if args.standalone:
        log(f'Single-file copy: {build_standalone(cfg)}')


def cmd_sources(args, cfg):
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except Exception:
        pass
    store = Store()
    cnt = {}
    for r in store.scores['scores']:
        cnt[r.get('source')] = cnt.get(r.get('source'), 0) + 1
    for s in sorted(store.sources['sources'], key=lambda s: (s.get('type', ''), s['id'])):
        st = s.get('last_status') or ''
        if args.failed and (st in ('ok', 'unchanged', 'seed') or not st):
            continue
        if s.get('refresh') == 'ref' and not args.all:
            continue
        print(f"{s.get('type', ''):11s} {s['id'][:34]:34s} {cnt.get(s['id'], 0):4d} rows  {s.get('last_fetch', '') or '':10s} {st[:28]:28s} {s['url'][:80]}")


def cmd_add(args, cfg):
    store = Store()
    sid = args.id or slug(args.name or urllib.parse.urlparse(args.url).netloc + urllib.parse.urlparse(args.url).path)[:40]
    if any(s['url'] == args.url for s in store.sources['sources']):
        sys.exit('That URL already exists')
    store.sources['sources'].append({'id': sid, 'name': args.name or sid, 'url': args.url, 'type': args.type,
                                     'categories': [c.strip() for c in (args.cat or 'other').split(',')], 'data_url': args.data_url,
                                     'format': 'html', 'extract_hint': args.hint or '', 'enabled': True, 'added': TODAY,
                                     'added_by': 'manual', 'refresh': 'static' if args.static else 'live',
                                     'last_hash': None, 'last_status': 'new'})
    store.save()
    print(f'Added {sid}. Run: python3 llm_bench.py update --only {sid}')


def cmd_doctor(args, cfg):
    print('Python', sys.version.split()[0], '| curl', bool(shutil.which('curl')))
    try:
        c = Claude(cfg)
        print('Claude backend:', c.describe())
        if c.ok:
            t = c.ask('Reply with exactly: pong')
            print('  test call →', t.strip()[:40])
    except Exception as e:
        print('Claude backend error:', e)
    for u in ['https://hn.algolia.com/api/v1/search?query=test', 'https://artificialanalysis.ai/leaderboards/models',
              'https://openrouter.ai/rankings', 'https://raw.githubusercontent.com/Aider-AI/aider/main/README.md']:
        code, _, body = fetch(u, 20)
        print(f'  {code} {len(body):>8} {u}')


def main(argv=None):
    load_env()
    cfg = load_config()
    ap = argparse.ArgumentParser(prog='llm_bench', description='LLM Standings updater: refreshes data.js / data.json for index.html')
    sub = ap.add_subparsers(dest='cmd')
    u = sub.add_parser('update', help='fetch + extract + reputation, then write data.js / data.json')
    u.add_argument('--only', help='only these source ids (comma-separated; trailing * matches a prefix)')
    u.add_argument('--all', action='store_true', help='also re-fetch static sources (official / paper)')
    u.add_argument('--force', action='store_true', help='re-extract even if the page is unchanged')
    u.add_argument('--limit', type=int, help='process at most N sources')
    u.add_argument('--no-discover', action='store_true', help='skip searching for new models / sources')
    u.add_argument('--no-reputation', action='store_true', help='skip reputation and usage signals')
    u.add_argument('--only-rep', help='run reputation only for these model ids (comma-separated)')
    u.add_argument('--skip-sources', action='store_true', help='do not fetch sources; only run reputation / signals')
    rd = sub.add_parser('render', help='rewrite data.js + data.json from the current data (index.html is static)')
    rd.add_argument('--standalone', action='store_true',
                    help='also build one self-contained HTML file (report.standalone, default dist/llm-standings.html)')
    s = sub.add_parser('sources', help='show source status')
    s.add_argument('--failed', action='store_true', help='only sources whose last status is not ok / unchanged')
    s.add_argument('--all', action='store_true', help='also show reference-only sources (not extracted)')
    a = sub.add_parser('add', help='add a source')
    a.add_argument('url')
    a.add_argument('--id')
    a.add_argument('--name')
    a.add_argument('--type', default='third_party', choices=SOURCE_TYPES)
    a.add_argument('--cat', help='categories, comma-separated: ' + ','.join(CATEGORIES))
    a.add_argument('--data-url')
    a.add_argument('--hint')
    a.add_argument('--static', action='store_true', help='static page (launch blog / paper), re-fetched every 30 days by default')
    sub.add_parser('doctor', help='check the environment (Claude backend / network)')
    im = sub.add_parser('import', help='merge extraction results collected elsewhere (JSON files)')
    im.add_argument('files', nargs='+')
    args = ap.parse_args(argv)
    if args.cmd == 'update':
        cmd_update(args, cfg)
    elif args.cmd == 'render':
        cmd_render(args, cfg)
    elif args.cmd == 'sources':
        cmd_sources(args, cfg)
    elif args.cmd == 'add':
        cmd_add(args, cfg)
    elif args.cmd == 'doctor':
        cmd_doctor(args, cfg)
    elif args.cmd == 'import':
        cmd_import(args, cfg)
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
