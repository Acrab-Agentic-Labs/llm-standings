# LLM Standings methodology

This page explains what LLM Standings collects, how a model's score and rank are worked out, how often the data is
refreshed, and how errors are corrected. The same text is on the **About** tab of the report.

## 1. What this is

LLM Standings is a leaderboard of leaderboards, built by Acrab AI and updated every two days. It is a single web page that does four things:

- It collects benchmark results for current large language models from many places.
- It keeps the source of every number.
- It ranks models within categories: coding, agentic work, reasoning & math, knowledge & professional work, multimodal, frontend & design, 3D & spatial, long context, writing & EQ, and hallucination & safety.
- Next to the scores, it shows:
  - **Reputation**: what users actually say about each model.
  - **Usage signals**: OpenRouter traffic and a quality-drift tracker.
  - **Harness comparison**: a feature matrix of coding agents.
  - **Benchmark glossary**: what each benchmark measures and what to watch out for.

LLM Standings was built to answer a practical question for on-device agents: which open model in the ~100B class
should you run, and how much do you give up compared with the closed frontier? The **Size** tab ranks models within
each parameter band for exactly that comparison.

## 2. What is included

### Models

- The leaderboard tracks the latest two generations of each vendor's **size line**. The lines are flagship, mid, small and code.
- A new flagship only pushes out older flagships. A vendor's ~100B model is not dropped because its flagship got a successor.
- **Restricted releases** (partner-only access) are included and labelled "(restricted)". A toggle hides them.
- These are excluded via an ignore list:
  - fine-tunes of another vendor's weights
  - unreleased code names
  - on-device models under 5B parameters
  - product-only previews
- Parameter counts (total / active) are recorded for open-weights models. The **Size** filter and tab use five bands: ≤40B, 40–150B, 150–500B, >500B and undisclosed.

### Sources

Every score carries:

- the model
- the reasoning effort
- the agent harness, if any
- the date
- the source page

There are five source types:

| Type | Examples | Priority |
|---|---|---|
| Third-party | Artificial Analysis, Vals.ai, Epoch AI, Scale Labs, LMArena, Terminal-Bench, Design Arena, EQ-Bench, Context Arena | 1 (highest) |
| Community | Lech Mazur's boards (writing, confabulations, NYT Connections), Kagi LLM Benchmark | 1 |
| Paper | Technical reports, independent audits | 2 |
| Official | Vendor launch posts, model cards | 3 |
| Aggregator | Sites that copy numbers from elsewhere | 4 (lowest; never used for discovery) |

The type normally follows the page a score comes from. When an independent page relays a number that the model's own vendor ran or reported, that score is stored as vendor-reported (Official).

### Reasoning effort

Many models can think for longer or shorter: none / minimal / low / medium / high / xhigh / max. Scores are stored per effort level. The **Effort** tab shows how much effort changes the result.

## 3. Which number is shown for a benchmark

Each model gets **one score per benchmark**:

1. Take the highest-priority source type available for that model and benchmark. The order is independent (third-party/community) > paper > official > aggregator.
2. Within that tier, take the best score across effort levels. The effort used is shown on the chart.
3. Vendor-reported numbers are marked with `*`.

## 4. How models are ranked

Category rankings are used in the heatmap, each category tab and the Size tab. Each ranking combines many leaderboards in four steps:

1. **Percentile per leaderboard.**
   - Only boards with at least 4 models are used.
   - On each board, a model's rank becomes a percentile: 0 for last, 100 for first.
   - With the *By score gap* toggle, the actual score is used instead, scaled from the board's lowest (0) to its highest (100). This keeps the size of the gaps.
2. **Benchmark families.** Versions or variants of the same evaluation are averaged into one family, so one benchmark is not counted twice. Examples:
   - Terminal-Bench 2.x / 4.0
   - the Humanity's Last Exam variants
   - SWE-bench Pro public / v2
   - the Artificial Analysis index versions
3. **One vote per publisher.**
   - Within a category, all families from the same publisher are averaged into a single vote. Publishers include Vals.ai, Artificial Analysis and Scale Labs.
   - The category score is the average of the votes, so a publisher with ten boards does not outvote one with a single board.
   - Weights:
     - Saturated boards count at half weight.
     - These don't count at all:
       - boards that are no longer updated
       - superseded versions
       - vendors' internal benchmarks
4. **Shrinkage.**
   - A model needs results from at least 2 families.
   - Scores are pulled toward 50 when evidence is thin: `score = (raw × k + 50 × 2) / (k + 2)`, where *k* is the number of families.
   - Heatmap cells based on fewer than 3 families are faded. Hovering a cell lists the boards used.

**Overall ranking.**

- This is the average of a model's category scores, for models with at least 2 categories.
- New models covered by only one or two publishers can rank higher or lower than they will once more boards test them. Check the hover details.

**Defaults and toggles.**

- By default, rankings use **independent results only**.
- The *Ranking* toggles can:
  - add vendor self-reports
  - give saturated boards full weight
  - switch to score gaps
  - hide restricted releases
- The toggles only change rankings. Benchmark charts always show every score and can be filtered by source type.

**Size view.** The same method applies, but the ranking is computed only among models in the same parameter band. Small models are not compared against trillion-parameter ones.

## 5. Reputation and usage signals

**Inputs.**

- Posts from the last 30 days on Hacker News (Algolia API), Reddit, GitHub issues and V2EX.
- A web search covering Reddit, X, linux.do and Zhihu.

**Classification.**

- Claude reads the posts and keeps only first-hand opinions.
- It counts positive, negative and neutral opinions and scores aspects: coding, agents, writing, reasoning, speed, cost, stability, instruction following and hallucination.
- It records short praise and complaint tags.
- It keeps quotes with links. Quotes not written in English are translated and marked *translated*; the original wording is kept in the data.
- Quotes are shown only when civil and on topic. Opinions that contain insults or allege misconduct (for example deliberate degradation or benchmark gaming) still count in the shares but are not quoted. Our own summaries and tags describe such claims neutrally, as reports by their source ("users report a perceived quality drop", "an audit by X reports ..."), and never name individual users.

**Ordering.**

- Models are ordered by net positive share.
- Each share is pulled toward the overall average, as if the model had 10 extra average opinions.
- Models with fewer than 8 opinions are listed separately as a small sample.
- A much thinner new sample never overwrites a richer earlier one.

**Usage signals.** OpenRouter's weekly token ranking and the LiveNerf quality-drift tracker.

## 6. How often it is updated

- The data is updated every two days with `python3 llm_bench.py update`. The header of the report shows the date of the last update.
- Each update does the following:
  1. **Discovers** new models and new primary sources, such as launch posts, model cards and new independent leaderboards. Aggregator sites are blocklisted.
  2. **Re-reads every live leaderboard.**
     - A page whose content hash has not changed is skipped. Changed pages are re-extracted.
     - Vendor posts and papers rarely change, so they are re-read every 30 days.
     - Some sites serve cached copies, so a cache-busting parameter is added for them.
  3. **Extracts** structured rows with Claude: model, effort, benchmark, harness, score. Numbers must appear literally on the page. Model names are matched against a registry of aliases. Unknown names are classified as an alias, a new model or a name to ignore.
  4. **Refreshes** reputation and usage signals.
  5. **Records** a changelog: new models, new benchmarks, the biggest changes and hand-written highlights. It also stores a dated snapshot of best scores.
- Only `data.js` / `data.json` change in a normal update. The page itself (`index.html`) stays the same.

## 7. Corrections

**Policy.**

- When an error is found, the data is fixed in the next update.
- The fix is logged in `data/corrections.json`, which is shown on the About tab. Each entry has the date, what was wrong and what was changed.
- Old values are not silently overwritten: earlier best scores remain in the dated snapshots and the changelog.
- Changes made by a benchmark's own publisher, such as a new version or a revised dataset, are logged too, because they replace data.

**Log** (as of 2026-11-13; the live list is in `data/corrections.json`):

| Date | Type | What was wrong / changed | Fix |
|---|---|---|---|
| 2026-11-13 | Error | ARC-AGI-3 scores under ARC Prize's Provider Adapter harness were stored only for the three newest entries (Claude Fable 5.2, Gemini 4 Argon, Gemini 4 Carbon). For older entries only the Standard-harness score was stored, so the best scores were not comparable across models. | Provider Adapter rows added for every model that has them on ARC Prize's result pages. The best ARC-AGI-3 score changed for six models (for example GPT-6 Astra 62.7 to 99.9, GPT-6.1 Sol 52.7 to 96.4, Claude Sonnet 5.5 24.3 to 72.4). |
| 2026-11-13 | Upstream | Epoch AI re-fitted its Capabilities Index after adding Gemini 4 Carbon (every value moved slightly, for example Claude Fable 5.2 174.24 to 173.44). Its model pages also show higher GPQA Diamond values for Claude Fable 5.2 (94 to 96) and Claude Opus 5.5 (91 to 94). | ECI rows replaced with the current values; the two GPQA Diamond rows updated. |
| 2026-11-11 | Error | RoboDojo Sim, a public robot-manipulation benchmark, was labelled an in-house benchmark, because its description said that Anthropic's results were run internally. | The label is removed and the official RoboDojo leaderboard is now tracked as a source. Anthropic's own runs stay marked vendor-reported. No default ranking changed. |
| 2026-11-11 | Error | 9 BenchCAD Vision2Code scores that the board had copied from vendor system cards and launch charts were stored as independent results. | They are now marked vendor-reported and no longer count in the default ranking. A single score on an independent page can now be marked vendor-reported. |
| 2026-11-11 | Upstream | Terminal-Bench released version 4.1: the same 66 tasks with stricter verifiers; existing 4.0 submissions were regraded. | 4.1 added as a new benchmark. The 4.0 results are kept under the archived 4.0 board; both versions count as one benchmark family in rankings. |
| 2026-11-11 | Upstream | Epoch AI revised its MirrorCode scores (for example Claude Opus 5.5 79 to 96) and re-fitted its Capabilities Index again. | Rows replaced with the current values. |
| 2026-11-10 | Error | Arena vision scores mixed two views of the same leaderboard (raw and style-controlled), so 15 models had two different values. | Only the style-controlled values (the leaderboard's default view) are kept; the vision board was re-read from the 2026-11-08 snapshot. |
| 2026-11-10 | Upstream | Scale released SWE Atlas v1.1 with a new judge; its scores are not comparable with v1.0. | v1.1 added as new benchmarks; the v1.0 boards are kept for reference, marked superseded and excluded from rankings. |
| 2026-11-10 | Upstream | Kaggle re-scored the FACTS suite on Nov 6; each model's average now covers only the dimensions it was tested on. | FACTS rows replaced with the 7 tracked models that have all four dimensions; 9 older rows for models with partial or no coverage removed. |
| 2026-11-10 | Upstream | Epoch AI recomputed its Capabilities Index (ECI); every value moved and one entry (GPT-5.5 Instant) was dropped. | All ECI rows replaced with the new values. |
| 2026-11-05 | Error | Reputation summaries, tags and changelog lines used loaded wording ("suspected benchmark gaming", "nerf", "dumb"), and 16 quotes contained insults or misconduct allegations. | Our own text was rewritten neutrally with attribution; the 16 quotes are no longer shown (the opinions still count). |
| 2026-11-05 | Error | The changelog's "best-score changes" took the highest value from any source, so vendor self-reports showed as gains. | The changelog now uses the same source priority as the report. |
| 2026-11-05 | Error | On Nov 4, the Scale Labs HLE-Diamond rows for GPT-6.1 Terra and DeepSeek V4.1 Flash were deleted based on a stale cached page. | Both rows were restored. AA, Vals.ai and Scale Labs pages are now always read with a cache-buster. |
| 2026-11-04 | Upstream | Artificial Analysis revised the CritPt v2 dataset (Index v5.0.1). | 73 old rows were removed and all models re-read (Nov 4–5). |
| 2026-11-04 | Upstream | Design Arena changed the evaluation pool of its Blender board. | 16 old rows were removed. |
| 2026-11-04 | Upstream | CWE-bench v2 is not comparable with v1. | v1 is kept but marked superseded and excluded from rankings. |
| 2026-11-02 | Error | Category rankings double-counted versions of the same evaluation, let one publisher dominate the agentic category, and included self-reports by default. | Benchmark families, one vote per publisher, and independent-only by default. |
| 2026-11-02 | Error | The reputation ranking put models with 1–2 opinions at the top. | Shrinkage toward the average and a minimum of 8 opinions. |
| 2026-11-02 | Error | Inkling and Inkling Small were shown as closed models. | Both are now marked open weights. |
| 2026-11-02 | Upstream | Vals.ai SAGE v1.1 changed its methodology. | v1.1 scores are stored separately from v1. |
| 2026-10-28 | Error | The original "Muse Spark" and the non-Pro "MiMo V2.5" were mapped to Muse Spark 1.3 and MiMo V2.5 Pro. | 7 scores were removed and both names ignored. |
| 2026-10-28 | Upstream | The AA Intelligence Index moved from v4.3.2 to v5.0. | The versions are stored separately; v4.3.2 is excluded from rankings. |
| 2026-10-27 | Error | Self-reports could replace independent results, and ProgramBench mixed two metrics. | Source priority was added and ProgramBench was split in two. |

To report an error, [open an issue](https://github.com/Acrab-Agentic-Labs/llm-standings/issues/new/choose) using the **Wrong score** or **Add a source or model** template, with the model, the benchmark and a link to the source. Other questions: yiran.zhang@acrab.ai.

## 8. Limits

- Benchmarks measure narrow skills. A higher rank does not mean a model is better for your task.
- Coverage is uneven. New and smaller models appear on fewer boards, so their rankings are less certain.
- Leaderboards differ in harness, prompts and effort settings. The Harness and Effort tabs show where that matters.
- Reputation reflects the people who post online, not all users. Some forums can only be searched, not read in full.
