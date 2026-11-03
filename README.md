# Competitor Marketing Agent (Exa + OpenCode + Phoenix/Langfuse)

Manual-run only. Free-tier friendly.

## Paths
- Web app (recommended): FastAPI backend + React frontend. Enter a company → auto-discover rivals → untick non-rivals → run a live scan (SSE progress, proof, metric cards, filterable table, trace viewer, saved reports, markdown download).
- Chat agent: `@marketing-researcher scan inputs.yaml` (uses the Exa MCP server).
- Observable CLI: `python runner.py --input inputs.yaml [--dry-run] [--tracer phoenix|langfuse|both|none]`.

## Setup
1. `pip install -r requirements.txt` (pyyaml enough for dry-run)
2. `cp .env.example .env`
3. Free Exa key when ready (no card): https://dashboard.exa.ai/api-keys → `EXA_API_KEY` in `.env`
   - Anonymous MCP (`https://mcp.exa.ai/mcp`) works for 1-2 tiny tests, then rate-limits.
4. Phoenix (optional, free local): `pip install arize-phoenix`, then start it in a thread (works on Python 3.9; avoids the `phoenix.server/types.py` shadowing bug):
   `env -u PHOENIX_COLLECTOR_ENDPOINT PHOENIX_PORT=6006 PHOENIX_HOST=127.0.0.1 python3 -c "import time, phoenix as px; px.launch_app(); [time.sleep(3600) for _ in iter(int, 1)]"` → http://localhost:6006
5. Langfuse (optional): set `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` (and `LANGFUSE_OTEL_ENDPOINT` for self-host). Traces export over OTLP; no Langfuse SDK needed.

## Run
- Web app: backend `python3 -m uvicorn backend.main:app --port 8000`, then frontend `npm run dev --prefix web` → `http://localhost:5173`.
- Terminal: `python runner.py --company Figma` (rivals auto-discovered; `--competitors ...` skips discovery, `--no-grade` skips grading, `--no-proof` skips page fetches, `--synthesis template|openai|auto`, `--dry-run` for free mock).
- Phoenix UI: start it with the command in Setup step 4 → `http://localhost:6006`, then scan with `--tracer phoenix`.
- Langfuse: scan with `--tracer langfuse` (or `both`).
- Via yaml fallback: `python runner.py --input inputs.yaml --dry-run --tracer none`.

## Integrations
- **Exa SDK** (`exa_py`): competitor discovery, campaign search with highlights, `outputSchema` grading, and `get_contents` proof fetching for the top campaign URLs.
- **Exa MCP** (`opencode.json`): remote server exposing `web_search_advanced_exa`, `competitor_finder`, `company_research`, `web_fetch_exa`. Restart opencode after changing config.
- **Grading**: Exa structured output, done during the search (no extra installs); `--no-grade` leaves raw rows with Strength TBD.
- **Synthesis**: template by default ($0). `--synthesis openai` (or `auto` with `OPENAI_API_KEY`) rewrites the report with OpenAI while preserving citations and proxy disclaimers.
- **Tracing**: Phoenix and/or Langfuse via OTLP, plus local `traces/*.jsonl` always.

## Costs
- Exa free: $20 signup + $10/mo. Real scan (2 comps × 3 queries + proof) ≈ $0.05.
- Template synthesis = $0. OpenAI synthesis only if you set `OPENAI_API_KEY`.
- Phoenix/Langfuse self-host = $0.

## Effectiveness
Public proxies only (press/social/SEO/hiring signals), never ROI. Every claim needs a URL.
