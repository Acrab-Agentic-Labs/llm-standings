# Updating LLM Standings

LLM Standings is two things:

- `llm_bench.py`, an updater that uses only the Python standard library (3.7+).
- A static page, `index.html`, that reads the data the updater writes.

## Quick start

```bash
git clone https://github.com/Acrab-Agentic-Labs/llm-standings.git
cd llm-standings
python3 llm_bench.py doctor     # check the Claude backend and network access
python3 llm_bench.py update     # discover → fetch & extract → reputation → write data.js / data.json
open index.html                 # or double-click it
```

`config.json` is created with defaults on the first run.

## What an update does

1. **Discover.** Claude searches for new models and new primary sources: launch posts, model cards and independent leaderboards. Aggregator sites are blocklisted in `config.json` → `update.discover_block_domains`.
2. **Fetch and extract.** Every source in `data/sources.json` is re-read.
   - If a source has a `data_url` (JSON / CSV / raw file), that is used first.
   - Pages whose content hash is unchanged are skipped.
   - Changed pages are given to Claude, which extracts rows (model, effort, benchmark, harness, score). The rows are merged into `data/scores.json`, keyed on (model, benchmark, effort, harness).
   - Live leaderboards (`refresh: live`) are read on every run. Vendor posts and papers (`refresh: static`) are read every 30 days; `--all` forces them.
   - Pages that are rendered by JavaScript, blocked, or PDFs are read through Claude's own web tools.
3. **Resolve new names.** A model name not in the registry is classified by Claude as an alias, a new model or a name to ignore.
   - Generations are tracked per vendor and size line (flagship / mid / small / code), and only the latest two generations of each line are kept.
4. **Reputation and signals.**
   - Recent posts are collected from Hacker News, Reddit, GitHub issues and V2EX, plus a web search. Claude classifies them; the rules are in [METHODOLOGY.md § 5](../METHODOLOGY.md#5-reputation-and-usage-signals).
   - OpenRouter usage and LiveNerf drift are refreshed.
5. **Write.** `data.js` and `data.json` are written, a changelog entry is added, and the badge counts in `README.md` are refreshed.

A first full update calls Claude about 80–120 times and takes 15–25 minutes. Later runs only re-extract pages that changed.

## Claude backend (`config.json` → `claude.backend`)

| Value | Behaviour |
|---|---|
| `auto` (default) | Uses the Anthropic API (server-side web search / fetch) if `ANTHROPIC_API_KEY` is set, otherwise the local `claude -p` CLI (Claude Code subscription, no key needed) |
| `api` / `cli` | Force one of them |

## Commands

```bash
python3 llm_bench.py update --only vals-*,tbench-tb4                   # refresh some sources only
python3 llm_bench.py update --no-discover --no-reputation              # leaderboards only
python3 llm_bench.py update --skip-sources --only-rep claude-opus-5.5  # reputation only
python3 llm_bench.py update --all                                      # also re-read vendor posts / papers
python3 llm_bench.py update --force                                    # re-extract even unchanged pages
python3 llm_bench.py render                                            # rewrite data.js / data.json offline
python3 llm_bench.py render --standalone                               # + dist/llm-standings.html (single file)
python3 llm_bench.py sources --failed                                  # sources that failed last time
python3 llm_bench.py add https://example.com/leaderboard --name "X Bench" --type third_party --cat coding
python3 llm_bench.py import results.json                               # merge results collected elsewhere
python3 llm_bench.py doctor                                            # check backend and network
```

`import` accepts a JSON file with `sources`, `new_models`, `model_updates`, `new_sources`, `sentiment` and `signals`. See the docstring of `cmd_import` for the format.

## Optional environment variables

These can go in a `.env` file next to `llm_bench.py`; `.env` is git-ignored.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Use the API backend |
| `ARTIFICIAL_ANALYSIS_API_KEY` | Free key; read Artificial Analysis' JSON API instead of scraping the page |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Reddit returns 403 to anonymous `.json` requests; create a "script" app |
| `GITHUB_TOKEN` | Higher rate limit for GitHub issue search |

## Hosting and sharing

The page is static.

- **Hosting:** put `index.html`, `data.js`, `data.json` and `assets/` on any static host. After each update, only `data.js` and `data.json` change.
  - The public site is served by GitHub Pages from this repository (Settings → Pages → Deploy from a branch → `main`, `/(root)`): https://acrab-agentic-labs.github.io/llm-standings/. Pushing an update to `main` republishes it.
  - The empty `.nojekyll` file tells GitHub Pages to serve the files as they are.
- **Single file:** `render --standalone` builds `dist/llm-standings.html` with ECharts and the data inlined (~2.5 MB), for emailing or uploading one file.
- **Why `data.js` and not `data.json`:** a page opened from disk (`file://`) is not allowed to read a separate `.json` file, so `index.html` loads `data.js`, which sets `window.LLM_DATA`.

## Maintaining the data

- **Corrections:**
  - When published data changes for a reason other than a normal source update, add an entry to `data/corrections.json` (date, type `error` or `upstream`, what, fix) and to the table in `METHODOLOGY.md`.
  - The About tab shows `data/corrections.json`.
- **Ignored names:** `data/models.json` → `ignored_names` lists names that must never be added (fine-tunes, code names, product-only previews).
- **Restricted releases:** set `"restricted": true` on the model; the page labels it and can hide it.
- **Highlights:** short, hand-written lines can be added at the top of `data/changelog.json` with `"kind": "highlight"`.
- **Wording:** reputation text must stay neutral and attributed. The reputation prompt enforces this, and [METHODOLOGY.md § 5](../METHODOLOGY.md#5-reputation-and-usage-signals) states the policy.

## Files

```
index.html               the report page (edit this to change the layout)
data.js / data.json      generated report data
assets/echarts.min.js    Apache ECharts 5.6.0 (license: assets/ECHARTS-LICENSE, assets/LICENSE-d3)
llm_bench.py             the updater (CLI)
config.json              settings
METHODOLOGY.md           what is collected, how ranking works, update frequency, corrections
data/models.json         model registry: generation, size line, effort levels, prices, parameters, aliases, ignore list
data/sources.json        every source URL, extraction hint and last fetch status
data/scores.json         all scores, each with source, effort, harness and date
data/benchmarks.json     benchmark glossary (scope, distinctive features, status)
data/reputation.json     reputation, usage and quality-drift signals
data/harnesses.json      coding-agent feature matrix
data/changelog.json      what changed in each update
data/corrections.json    corrections log (shown on the About tab)
data/LICENSE             CC BY 4.0 for the data
data/history/            dated snapshots of best scores (git-ignored)
dist/                    single-file build (git-ignored)
```
