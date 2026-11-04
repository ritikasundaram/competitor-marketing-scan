# Competitor Scan: an agent that researches your rivals with Exa

Tell it your company. A LangGraph agent finds your competitors and breaks the job into small,
answerable questions. Parallel researchers answer each one with Exa, check their own answers and
retry with a better search when they fall short. The agent then reviews what's missing, profiles
each rival and writes a ranked action plan. Every claim links to a page a researcher actually read.

Public signals only (press, partners, events, social): never revenue, spend or ROI.

## How the agent works

```
understand ─ reads your own site (Exa), notes what you already offer
find_rivals ─ Exa search with an output schema → name, domain, reason
confirm_rivals ⏸ you untick non-rivals or add one (always on)
plan_questions ─ atomic questions: one company, one topic, one time window
approve_questions ⏸ edit / remove / add questions (toggle, off by default)
research ×N (in parallel) ─ each question gets its own researcher sub-graph:
    search (Exa) → read (Exa) → answer (AI) → check (rules) → judge (AI)
       ↑──────────── retry with a better search if not answered ─┘
review_gaps ─ asks for up to 4 follow-up questions → one more research round
profile_rival ×N (in parallel) ─ campaigns rated with the rubric, threat level
plan_actions ─ ranked actions: what, why, first step, effort, impact, evidence
write_report ─ reports/<date>_scan_<run>.json (dashboard) + .md
```

**Who does what**
- **Exa** does all the web research:
  - `search` (type auto + highlights) finds pages by meaning
  - `get_contents` reads full pages so claims can be checked
  - a search with an `output_schema` finds rivals
- **The AI** plans, answers from the numbered sources, judges its own answers, finds gaps and writes
  the plan. The provider is whichever key is in `.env` (or `AI_PROVIDER`):
  - **Claude Haiku 5.5** (`ANTHROPIC_API_KEY`): `claude-haiku-5-5` for the planner and researchers.
  - **Gemini** (`GEMINI_API_KEY`): Flash models for the planner, Flash-Lite for researchers. Each role
    has a list of fallbacks: a model that hits its daily free-tier limit is skipped for the day, and
    calls share a rate limiter (`GEMINI_RPM`, default 10/minute).
  - **Claude via OpenCode** (`OPENCODE_API_KEY`, `AI_PROVIDER=opencode`).
  - Override models in Settings or with `PLANNER_MODEL` / `RESEARCHER_MODEL`.
  - "Busy" errors are retried with growing waits. A bad key, no credit or a used-up quota stops the
    scan with a plain message.
- **Plain rules** (no AI) keep it honest:
  - a finding must cite a page the researcher read (made-up ids are dropped)
  - the page must be about that company: on its site, or naming it (look-alikes are dropped)
  - findings older than `max_age_days` (default 365) are dropped; undated ones are flagged
  - numbers the page doesn't show are flagged, and confidence drops to Low
  - **rubric:**
    - Strong = 2+ independent outside sources, or a concrete result a source states
    - Medium = one outside source or own-channel engagement
    - Weak = own announcement only
  - the AI rates first; the rule lowers any rating the cited sources can't back
  - every action must cite at least one checked rival source, or it's dropped (one retry)
  - every search names the company's website, even the AI's rewritten retry searches

## Phoenix tracing (OpenInference)

- **Auto-traced:** `LangChainInstrumentor` records every graph step as a CHAIN span and every model
  call as an LLM span, with prompt, output, model name and token counts.
- **Hand-made spans:** Exa calls and checks get their proper span kind and nest under the step that
  made them.

| Span | Kind |
|---|---|
| `competitor_scan (start / resume)`: one per run segment | AGENT |
| `researcher_agent Q3`: one per question, holding its steps | AGENT |
| graph steps (`understand`, `plan_questions`, `search`, `judge`…) | CHAIN |
| model calls | LLM |
| `exa.search`, `exa.discover`: every result as a retrieval document | RETRIEVER |
| `exa.read` (get_contents) | TOOL |
| `check.findings`, `check.rubric`, `check.actions` | GUARDRAIL |
| `judge.answer` (did the researcher answer it? with `eval.label`) | EVALUATOR |

**Sessions:** a scan that pauses for you produces one trace per segment. All of them share
`session.id` = the run id, so Phoenix's **Sessions** tab shows the whole scan. The dashboard links
to its trace.

## Setup

1. `pip3 install -r requirements.txt` (Python 3.9+)
2. `cp .env.example .env`, then add:
   - `EXA_API_KEY`: free at https://dashboard.exa.ai/api-keys
   - an AI key: `ANTHROPIC_API_KEY` (Claude Haiku 5.5, https://console.anthropic.com) or
     `GEMINI_API_KEY` (free at https://aistudio.google.com/apikey)
3. `npm install --prefix web`
4. Phoenix (optional, local, free). Start it and leave it running:
   `env -u PHOENIX_COLLECTOR_ENDPOINT PHOENIX_PORT=6006 PHOENIX_HOST=127.0.0.1 python3 -c "import time, phoenix as px; px.launch_app(); [time.sleep(3600) for _ in iter(int, 1)]"`
   → http://localhost:6006

## Run

- **Web app:**
  - `python3 -m uvicorn backend.main:app --port 8000 --reload`
  - in a second terminal: `npm run dev --prefix web` → http://localhost:5173
  - Tick **Mock mode** for a free run on a fake web with planted mistakes. It needs no keys.
- **Command line:**
  - `python3 -m agent --company Figma --domain figma.com --goal "enterprise marketing"`
  - `--mock` free fake run · `--yes` don't pause for rivals · `--approve-questions` review the list
  - `--rivals "Sketch,sketch.com;Penpot,penpot.app"` skip discovery · `--no-trace` no Phoenix
- **Tests** (free, offline; fake Exa + fake AI): `python3 -m unittest discover -s tests`
  - the fact checks and rubric
  - full runs with pauses, edits and retries
  - the API
  - span kinds, nesting and sessions

## Code map

| File | What it does |
|---|---|
| `agent/graph.py` | The LangGraph graph, the researcher sub-graph, prompts |
| `agent/exa.py` | Exa calls (traced), error handling, the mock web |
| `agent/llm.py` | Models via OpenCode; `MockChatModel` for mock runs |
| `agent/checks.py` | Fact checks and the rubric (plain rules) |
| `agent/schemas.py` | The forms each AI step fills in |
| `agent/tracing.py` | Phoenix + OpenInference setup, hand-made spans, sessions |
| `agent/run.py` | Runs the graph in segments (pause → resume), streams events |
| `agent/report.py` | Writes the `.json` and `.md` report |
| `backend/main.py` | FastAPI: start, resume, reports (keys stay server-side) |
| `web/src/` | React dashboard: setup, live view, pauses, report |

## Costs

- **Exa:** a default run (4 rivals × 4 questions + 2 about you, up to 4 follow-ups) makes roughly
  40–90 Exa calls (search + page reads), depending on how many questions need a second search.
  Exa's free tier is $20 at signup + $10/month.
- **AI:** one planner call per step, plus one or two researcher calls per question: about 60 calls
  for a default run (the last real run used 96k tokens). With Claude Haiku 5.5 that's billed per token
  by Anthropic. Gemini's free tier covers it if you stay under its daily limits. Phoenix shows the
  tokens for each call.
- **Mock runs and tests:** free.

## Limitations

- Paused scans live in server memory: restarting the backend forgets a scan that's waiting for you.
- Python 3.9 pins LangGraph to 0.6.11.
- Public signals aren't results: it can say a campaign got press, not whether it made money.
- The app is light mode only.
