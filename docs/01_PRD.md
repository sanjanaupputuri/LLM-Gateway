# 01 — Product Requirements Document
## Project 19: LLM Gateway — Routing, Caching & Cost Control

## Document Control
| Field | Value |
|---|---|
| Project | Project 19 — LLM Gateway: Routing, Caching & Cost Control |
| Document | Product Requirements Document (PRD) |
| Version | 1.0 |
| Status | Approved for build |
| Related documents | 02_ARCHITECTURE.md, 03_API_CONTRACTS.md, 04_BUILD_PLAN.md, 05_TEST_PLAN.md |

## Executive Summary
This project delivers a single internal gateway that every application
calls instead of hitting LLM providers directly. It routes each request to
the most appropriate free-tier model, falls back automatically if a
provider fails, caches repeated or near-duplicate queries, enforces
per-team usage budgets, and gives a live view of spend and usage patterns.
The result: predictable cost, resilience to any one provider's outages or
free-tier limits, and full visibility into who is spending what.

### 1. Problem statement
Multiple applications need to call LLMs. Without a shared gateway, each app
hard-codes a provider/model, duplicates retry logic, has no idea what it's
spending, and one runaway app can blow the whole budget. This project builds
the single internal endpoint every app should call instead of hitting
providers directly.

### 2. Goals
- G1. One stable internal HTTP endpoint (`POST /v1/chat/completions`) that
  fronts multiple backend LLM providers, OpenAI-compatible in shape.
- G2. Requests are routed to the cheapest model capable of handling them,
  with automatic fallback to another provider/model if the chosen one
  errors, times out, or is rate-limited.
- G3. Repeated or semantically similar requests are served from cache
  instead of hitting a provider again.
- G4. Each "team" (a caller identified by an API key) has a token budget;
  exceeding it throttles further requests and raises an alert.
- G5. A dashboard shows spend, request volume, cache-hit rate, and per-team
  cost breakdown, with anomaly alerts for unusual spend spikes.

### 3. Non-goals (explicitly out of scope)
- NG1. No enterprise SSO/OAuth — a static per-team API key is sufficient.
- NG2. No support for streaming responses in v1 (non-streaming
  `chat/completions` only). Streaming is a stretch goal, not required.
- NG3. No image/audio/multimodal input — text chat completions only.
- NG4. No multi-region or high-availability deployment — single instance is
  fine for this project's scale.
- NG5. No paid infrastructure of any kind. Every component must run for
  free (local Docker Compose, or free hosting tiers).

### 4. Target user
The platform/infra "team" in the scenario, and — from the caller's
perspective — any application that would otherwise call an LLM provider
directly. In this project, the "applications" calling the gateway are
simulated by a test harness (see `05_TEST_PLAN.md`) plus a simple demo chat
client.

### 5. Success metrics (KPIs)
| KPI | Target |
|---|---|
| Single endpoint adoption | 100% of test traffic goes through the gateway, none direct to a provider |
| Cost reduction from caching | ≥30% of a repeated-query test batch served from cache (cache hit, $0 provider cost) |
| Routing correctness | Simple prompts route to the cheap/fast model ≥90% of the time in the test suite; complex prompts route to the stronger model ≥90% of the time |
| Fallback resilience | 0 user-facing failures when the primary provider is forced to fail in tests |
| Quota enforcement | A team that exceeds its budget receives HTTP 429 on the very next request, not later |
| Spend visibility | Every logged request is attributable to a team_id, model, and cost in the dashboard within one refresh cycle |

### 6. Functional requirements
1. **FR1 — Gateway endpoint.** `POST /v1/chat/completions` accepts an
   OpenAI-style request body (see `03_API_CONTRACTS.md`) and an
   `X-Team-Key` header identifying the caller.
2. **FR2 — Routing engine.** Classify each incoming request as `simple` or
   `complex` using a deterministic heuristic (no extra LLM call — must not
   itself cost money or add material latency). Route `simple` → cheap/fast
   model, `complex` → stronger model. Routing rules are defined in a config
   file, not hard-coded (see `routing_rules.yaml` in
   `02_ARCHITECTURE.md`).
3. **FR3 — Fallback.** If the routed provider/model call fails (HTTP error,
   timeout, or rate-limit response), retry once against the same provider
   with backoff, then fail over to the next provider in the configured
   chain. Only return an error to the caller if every provider in the chain
   fails.
4. **FR4 — Exact cache.** Before calling any provider, compute a
   normalized hash of `(model_tier, messages, temperature, max_tokens)`. If
   a non-expired cache entry exists, return it directly — no provider call.
5. **FR5 — Semantic cache.** If no exact match, embed the latest user
   message locally (no external API call) and compare against cached
   embeddings for the same model_tier. If cosine similarity ≥ configured
   threshold (default 0.92), return the cached response, tagged
   `cache: "semantic"` in the response metadata.
6. **FR6 — Quota enforcement.** Each team has a daily token budget
   (configurable). Before routing, check the team's tokens used today. If
   the request would exceed budget, return HTTP 429 with a clear error body
   and do not call any provider.
7. **FR7 — Rate limiting.** Each team also has a requests-per-minute limit,
   enforced independently of the token budget.
8. **FR8 — Logging & cost attribution.** Every request (cache hit or
   provider call) is logged with: timestamp, team_id, route decision,
   provider, model, input/output tokens, computed cost, cache status,
   latency, and success/failure.
9. **FR9 — Dashboard.** A Streamlit app reads the log table and shows:
   total spend, spend by team (table + bar chart), spend by model, request
   volume over time, cache-hit rate, and an anomaly alert banner when a
   team's spend rate in the current hour exceeds N× its 7-day average
   (default N=3).
10. **FR10 — Admin config.** Teams, their API keys, and their budgets are
    defined in a config file (or a `teams` table seeded from it) — no code
    change needed to add a team.

### 7. Constraints
- Every provider used must be genuinely free (no credit card required for
  the free tier used).
- The whole system must run locally via `docker compose up` with zero
  paid services.
- Must degrade gracefully: if a provider's free-tier daily quota is
  exhausted mid-demo, the fallback chain must route around it, not crash
  the gateway.

### 8. Assumptions
- The builder (S) has, or will create, free accounts and API keys for:
  Groq (https://console.groq.com), Google AI Studio / Gemini
  (https://aistudio.google.com), and OpenRouter (https://openrouter.ai).
- Local development happens on a machine that can run Docker Desktop (or
  Docker Engine on Linux) and Python 3.11.
- "Team" in this project is a synthetic concept for the demo — a handful of
  hardcoded team IDs (e.g. `team-sales`, `team-support`, `team-eng`) is
  sufficient; it does not need a real user-management system.
