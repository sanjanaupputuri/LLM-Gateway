# Project 19 — LLM Gateway: Routing, Caching & Cost Control
## Build Document Set — Index

This folder is the complete, unambiguous build spec for Project 19 from the
BVRIT Enterprise GenAI Project Catalog. It is written to be handed directly
to a coding agent (Claude Code, Cursor, Copilot Workspace, etc.) or followed
manually. Read the documents in this order:

| # | Document | Purpose |
|---|----------|---------|
| 1 | `01_PRD.md` | What we're building and why. Scope, non-goals, KPIs. |
| 2 | `02_ARCHITECTURE.md` | System design, tech stack (exact versions), folder structure, data models, config files. |
| 3 | `03_API_CONTRACTS.md` | Every HTTP endpoint: method, path, request/response schema, status codes. |
| 4 | `04_BUILD_PLAN.md` | Phase-by-phase implementation plan with file-level tasks and acceptance criteria. **Give this file to the coding agent as the primary task list.** |
| 5 | `05_TEST_PLAN.md` | Concrete test cases (unit, integration, manual QA) mapped to the catalog's required test scenarios. |

## Key decisions already made (do not re-litigate these — they are locked)

- **Language/runtime:** Python 3.11, FastAPI + Uvicorn.
- **Model providers (all free, no credit card):**
  - **Groq** (primary — fast, generous free tier: Llama 3.1/3.3 models).
  - **Google Gemini** (secondary — free tier via Google AI Studio: Gemini 2.0/2.5 Flash).
  - **OpenRouter free-tier models** (tertiary fallback pool — `:free` suffixed models, used only when both above fail).
  - GitHub Models is **not** used — it was fully retired by GitHub on July 30, 2026.
- **Cache:** SQLite-backed (exact-match + semantic via local embeddings). No Redis dependency required to run for free; Redis is an optional swap documented but not required.
- **Quotas/rate limits:** Per-team token budgets enforced via SQLite + FastAPI middleware.
- **Observability:** Lightweight custom Streamlit dashboard reading the same SQLite DB. No Langfuse/Prometheus/Grafana — those are noted as a "Stretch goal" only.
- **Deployment:** Docker Compose for local/dev. Free-tier hosting path documented (Render for the API, Streamlit Community Cloud for the dashboard) — no paid infrastructure required anywhere.
- **Auth:** Simple `X-Team-Key` header mapped to a team_id via a config table — sufficient for a student/hackathon-grade project, explicitly not enterprise SSO.

## What "done" looks like

All 6 required architecture pieces from the catalog are implemented and demoable:
1. One internal endpoint fronting multiple backend models.
2. Task-based routing with automatic fallback.
3. Semantic + exact caching (visible cache hits in the dashboard).
4. Per-team quotas and rate limits (a team can be throttled).
5. A spend dashboard with anomaly alerts.
6. Cost attribution per team, visible in the dashboard.

If any of these six is missing, the build is not complete — do not mark the
project done until all six are demonstrable per `05_TEST_PLAN.md`.
