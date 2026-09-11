# 04 — Build Plan

## Document Control
| Field | Value |
|---|---|
| Project | Project 19 — LLM Gateway: Routing, Caching & Cost Control |
| Document | Implementation / Build Plan |
| Version | 1.0 |
| Status | Approved for build |
| Related documents | 01_PRD.md, 02_ARCHITECTURE.md, 03_API_CONTRACTS.md, 05_TEST_PLAN.md |

## Executive Summary
Eleven sequential phases take the project from an empty repository to a
containerized, dashboarded, fully tested gateway. Each phase lists its
concrete tasks, the exact files it produces, and the acceptance criteria
that must pass before the next phase begins. This document is written to
be handed directly to a coding agent as its task list.

Work through phases in order. Do not start a phase until the previous
phase's acceptance criteria pass. After each phase, run the relevant tests
from `05_TEST_PLAN.md` before moving on.

---

## Phase 0 — Project scaffolding

**Tasks**
1. Create the folder structure exactly as in `02_ARCHITECTURE.md` §3.
2. Write `requirements.txt` pinning the versions in `02_ARCHITECTURE.md`
   §2.
3. Write `.env.example` as specified. Do not commit a real `.env`.
4. Write `config/teams.yaml`, `config/routing_rules.yaml`,
   `config/pricing.yaml` exactly as specified in `02_ARCHITECTURE.md` §5.
5. Initialize a git repo, add `.gitignore` covering `data/`, `.env`,
   `__pycache__/`, `*.db`.

**Acceptance criteria**
- `pip install -r requirements.txt` succeeds in a clean venv.
- Folder tree matches the spec exactly (no extra top-level dirs).

---

## Phase 1 — Data layer

**Tasks**
1. Implement `gateway/models.py` with `Team`, `RequestLog`, `CacheEntry`
   exactly as defined in `02_ARCHITECTURE.md` §4.
2. Implement `gateway/db.py`: creates the SQLite engine at
   `GATEWAY_DB_PATH`, `init_db()` creates tables if missing, and seeds
   `Team` rows from `config/teams.yaml` if the `Team` table is empty.
3. Write `tests/conftest.py` with a pytest fixture that creates a fresh
   in-memory SQLite DB per test (`sqlite://`), overriding
   `GATEWAY_DB_PATH`.

**Acceptance criteria**
- Running the app once creates `data/gateway.db` with 3 seeded teams
  matching `config/teams.yaml`.
- `pytest tests/conftest.py` collects with no errors (fixture-only file,
  no tests yet, just confirm no import errors).

---

## Phase 2 — Provider clients

**Tasks**
1. Implement `gateway/providers/base.py` (`ProviderClient` ABC,
   `ProviderResponse`, `ProviderError`) exactly as in `02_ARCHITECTURE.md`
   §6.
2. Implement `groq_client.py`, `gemini_client.py`, `openrouter_client.py`
   per §6. Each reads its API key from environment variables loaded via
   `gateway/config.py`.
3. Implement `gateway/providers/circuit_breaker.py` per §7 — a
   `CircuitBreaker` class with `record_success()`, `record_failure()`,
   `is_open() -> bool`, keyed per provider name, held in a module-level
   dict (process memory is fine — no persistence needed across restarts).
4. Write a small manual smoke-test script `scripts/smoke_test_providers.py`
   that sends "Say hello in exactly 3 words." to each of the 3 providers
   directly (bypassing the gateway) using real API keys from `.env`, and
   prints the response + latency for each. This is for the human builder
   to confirm their API keys work — not part of the automated test suite.

**Acceptance criteria**
- `python scripts/smoke_test_providers.py` (with real keys in `.env`)
  prints a successful response from all 3 providers.
- `tests/test_fallback.py` (write now, per `05_TEST_PLAN.md` §Fallback)
  passes using `respx`-mocked HTTP calls — no real API keys needed for
  automated tests.

---

## Phase 3 — Routing engine

**Tasks**
1. Implement `gateway/config.py` to load and validate
   `routing_rules.yaml` into a Pydantic settings object.
2. Implement `gateway/router.py`:
   - `classify(messages, rules) -> Literal["simple", "complex"]` per
     `02_ARCHITECTURE.md` §8.
   - `get_chain(tier, rules) -> list[RouteStep]` returns the ordered
     provider/model chain for a tier from `routing_rules.yaml`.
   - `async def route_request(messages, temperature, max_tokens, rules,
     provider_clients) -> tuple[ProviderResponse, str provider_used, str
     model_used]`: classifies, gets the chain, and for each step:
     - Skips the provider if its circuit breaker `is_open()`.
     - Attempts the call up to `MAX_RETRIES + 1` times (FR3: retry once
       before falling over). `MAX_RETRIES = 1`.
     - Calls `record_failure()` on the breaker only after **all** retries
       for that provider are exhausted, then tries the next step.
     - Raises `AllProvidersFailedError` if the whole chain is exhausted.
3. Rewrite `tests/test_fallback.py` to call the real `route_request()`
   rather than a hand-rolled helper — fallback tests must exercise the
   actual production code path.

**Acceptance criteria**
- `tests/test_routing.py` passes: a short/simple prompt classifies
  `"simple"`; a long prompt (>400 chars) and a prompt containing
  "analyze" both classify `"complex"`.
- `tests/test_fallback.py` passes all 8 tests, including FB-08 (retry
  succeeds on second attempt — no fallback triggered). When groq fails,
  `groq.complete` call count must equal `MAX_RETRIES + 1` (i.e. 2),
  not 1.

---

## Phase 4 — Caching

**Tasks**
1. Implement `gateway/embeddings.py`: exposes `embed(text: str) ->
   np.ndarray`. The `all-MiniLM-L6-v2` model must be **lazy-loaded** —
   initialised on the first call to `embed()`, not at module import time.
   This prevents every test file that imports from `gateway` from paying
   the ~300ms model-load penalty. The single-load guarantee (one instance
   per process) is still preserved via a module-level `_model` variable.
2. Implement `gateway/cache.py`:
   - `make_cache_key(model_tier, messages, temperature, max_tokens) ->
     str` (sha256 per §9).
   - `exact_lookup(session, key) -> CacheEntry | None`.
   - `semantic_lookup(session, model_tier, query_embedding, threshold) ->
     CacheEntry | None` — brute-force cosine similarity over all
     non-expired rows for that tier.
   - `store(session, key, model_tier, prompt_text, embedding,
     response_json, ttl_seconds) -> CacheEntry`.
   - `evict_expired(session) -> int` — deletes all rows with
     `expires_at <= now`; returns count deleted. Without this, expired
     rows accumulate on disk indefinitely.
3. Call `evict_expired()` once at startup inside `init_db()` in
   `gateway/db.py`, immediately after table creation.
4. Wire cache lookup into the request flow in `routes/chat.py` **before**
   routing: exact lookup first, then semantic lookup, then only if both
   miss, call `route_request`.

**Acceptance criteria**
- `tests/test_cache.py` passes: identical request twice → second call is
  `cache_status: "exact"`, provider client is not called (assert via
  mock call count). A paraphrased near-duplicate request → third call is
  `cache_status: "semantic"`.
- Importing `gateway.embeddings` in a test that never calls `embed()`
  does not trigger a model download or load (verify by checking that
  `embeddings._model` is still `None` after a bare import).

---

## Phase 5 — Quota & rate limiting

**Tasks**
1. Implement `gateway/quota.py`:
   - `check_rpm(session, team) -> None`, raises `RateLimitedError` per
     §10 step 2.
   - `check_daily_budget(session, team) -> None`, raises
     `QuotaExceededError` per §10 step 3.
   - Both functions must accept `session: Session` as a parameter —
     never call `get_session()` internally (see session injection rule
     in `02_ARCHITECTURE.md` §10).
   - Both queries must use `WHERE team_id = :team_id` (parameterised) —
     never aggregate without a team filter, as `team_id` is `Optional`
     and NULL rows from auth failures must not be included.
2. Implement `gateway/auth.py`: `resolve_team(session, api_key) -> Team`,
   raises `InvalidApiKeyError` if not found. Accepts `session` as a
   parameter — never calls `get_session()` internally.
3. Wire both into `routes/chat.py` in the exact order specified in §10.

**Acceptance criteria**
- `tests/test_quota.py` passes: a team with a tiny budget (set in test
  fixture) gets a normal 200 on request 1, then 429
  `quota_exceeded` on a subsequent request that would exceed budget — no
  provider call is made for the rejected request (assert mock call
  count unchanged).
- A separate test: 21 rapid requests from a team with `rpm_limit: 20`
  results in the 21st returning 429 `rate_limited`.

---

## Phase 6 — Cost attribution & logging

**Tasks**
1. Implement `gateway/cost.py`: `compute_cost(provider, model,
   input_tokens, output_tokens, pricing_config) -> float`, reading
   `config/pricing.yaml`. Return `0.0` for any model/provider not found
   in the pricing table (log a warning, don't crash). The lookup key is
   `f"{provider}/{model}"` — e.g. `"groq/llama-3.1-8b-instant"`.
2. Implement `gateway/logging_service.py`: `log_request(session, ...) ->
   RequestLog`. Accepts `session: Session` as a parameter — never calls
   `get_session()` internally (see session injection rule in
   `02_ARCHITECTURE.md` §10).
3. Wire `POST /v1/chat/completions` end-to-end per the flow in
   `03_API_CONTRACTS.md`. Use a `try/finally` pattern in `chat.py` so
   that `log_request()` is called on **every exit path**: 200 (success),
   200 (cache hit), 429 (rate limited), 429 (quota exceeded), 502
   (all providers failed), and 504 (timeout). Requests that are throttled
   or rejected must appear in `RequestLog` — invisible failures make quota
   enforcement unauditable.

**Acceptance criteria**
- `tests/test_cost_attribution.py` passes: a mocked Groq response with
  known input/output token counts produces the exact expected `cost_usd`
  per the pricing table's per-token math. The test **must** use a
  non-zero-price model (e.g. `groq/llama-3.1-8b-instant` at $0.05/1M
  input) — testing only with free OpenRouter models would pass vacuously
  since `cost_usd` would be `0.0` regardless of whether the key lookup
  works.
- Manually hit the endpoint 5 times with `curl` (using a real or mocked
  key) and confirm 5 rows appear in `RequestLog` via a quick `sqlite3
  data/gateway.db "select * from requestlog;"`.
- Hit the endpoint with a bad API key (401) and with a team that has
  budget exhausted (429) — confirm both produce `RequestLog` rows with
  `success=False`.
---

## Phase 7 — Admin & health endpoints

**Tasks**
1. Implement `routes/admin.py`: `GET /v1/admin/teams`,
   `POST /v1/admin/teams` per `03_API_CONTRACTS.md`.
2. Implement `routes/health.py`: `GET /healthz`, including each circuit
   breaker's current state.

**Acceptance criteria**
- `curl localhost:8000/healthz` returns 200 with all 3 providers
  `"closed"` on a fresh start.
- `curl localhost:8000/v1/admin/teams` lists the 3 seeded teams with
  `tokens_used_today` matching the sum in `RequestLog`.

---

## Phase 8 — Dashboard

**Tasks**
1. Implement `dashboard/app.py` per `02_ARCHITECTURE.md` §11 — all 6
   panels. Use `st.cache_data(ttl=10)` on the DB-read function so the
   dashboard auto-refreshes every 10 seconds without hammering SQLite.
2. Add a sidebar filter: date range + team multiselect, applied to all
   panels.

**Acceptance criteria**
- `streamlit run dashboard/app.py` (or via Docker Compose) shows real
  data after running the smoke tests / demo traffic script (Phase 9).
- Manually force a "spend spike" (see `05_TEST_PLAN.md` §Anomaly) and
  confirm the red banner appears.

---

## Phase 9 — Containerization & demo script

**Tasks**
1. Write `Dockerfile` for the gateway (multi-stage: install deps, copy
   `gateway/`, `config/`; `CMD ["uvicorn", "gateway.main:app", "--host",
   "0.0.0.0", "--port", "8000"]`).
2. Write a second `Dockerfile` (or reuse with a different `CMD`) for the
   dashboard: `CMD ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0"]`.
3. Write `docker-compose.yml` per `02_ARCHITECTURE.md` §12 — two
   services, shared `./data` volume, gateway reads `.env`.
4. Write `scripts/demo_traffic.py`: sends a mixed batch of ~40 requests
   across the 3 seeded teams — a mix of simple/complex prompts, some
   exact duplicates, some paraphrased near-duplicates, and enough volume
   from one team to trip its quota — so a fresh reviewer can run one
   script and immediately see all 6 required capabilities lit up in the
   dashboard.

**Acceptance criteria**
- `docker compose up --build` starts both services; gateway healthy at
  `:8000/healthz`, dashboard reachable at `:8501`.
- `python scripts/demo_traffic.py` run against the running gateway
  populates the dashboard with visible: cache hits, at least one 429,
  spend by team/model, and (if you set a low anomaly threshold for the
  demo) the anomaly banner.

---

## Phase 10 — Documentation & handoff

**Tasks**
1. Write `README.md` at the repo root (separate from this doc set): setup
   instructions, how to get free API keys for Groq/Gemini/OpenRouter, how
   to run locally, how to run the demo script, screenshot of the
   dashboard.
2. Note the free-tier hosting option from `02_ARCHITECTURE.md` §12 as an
   optional "Deploy it" section.

**Acceptance criteria**
- A reviewer who has never seen the project can clone the repo, follow
  `README.md`, and get the demo running in under 15 minutes.
