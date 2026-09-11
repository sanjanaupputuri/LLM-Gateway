# 02 — Architecture

## Document Control
| Field | Value |
|---|---|
| Project | Project 19 — LLM Gateway: Routing, Caching & Cost Control |
| Document | System Architecture |
| Version | 1.0 |
| Status | Approved for build |
| Related documents | 01_PRD.md, 03_API_CONTRACTS.md, 04_BUILD_PLAN.md, 05_TEST_PLAN.md |

## Executive Summary
The gateway is a FastAPI service backed by SQLite, sitting in front of
three free-tier LLM providers (Groq, Google Gemini, OpenRouter). Every
request passes through auth, quota checks, a two-tier cache (exact +
semantic), a deterministic router with per-provider circuit breakers, and
structured cost-attributed logging. A read-only Streamlit dashboard
visualizes the resulting data. The entire stack runs for free via Docker
Compose, with an optional path to free-tier cloud hosting.

### 1. High-level diagram (describe, since this is a text spec)

```
Caller (test harness / demo client)
        |
        |  POST /v1/chat/completions   (X-Team-Key header)
        v
+-------------------------------------------------------+
|                   FastAPI Gateway                     |
|                                                         |
|  1. AuthMiddleware       -> resolves X-Team-Key->team  |
|  2. QuotaMiddleware       -> checks token+RPM budget    |
|  3. CacheLookup           -> exact hash, then semantic  |
|  4. Router                -> classify simple/complex    |
|  5. ProviderClient chain  -> Groq -> Gemini -> OpenRouter|
|  6. Logger                -> writes request row to DB   |
+-------------------------------------------------------+
        |                                  |
        v                                  v
   SQLite (gateway.db)               Streamlit Dashboard
   - requests table                  reads gateway.db
   - cache table                     read-only, auto-refresh
   - teams table
```

### 2. Tech stack (exact versions — pin these)

| Component | Choice | Version |
|---|---|---|
| Language | Python | 3.11 |
| Web framework | FastAPI | 0.115.x |
| ASGI server | Uvicorn | 0.32.x |
| HTTP client (to providers) | httpx | 0.27.x |
| Data validation | Pydantic | 2.9.x |
| DB | SQLite via `sqlite3` (stdlib) + `sqlmodel` 0.0.22 for ORM convenience | — |
| Embeddings (semantic cache) | `sentence-transformers` 3.1.x, model `all-MiniLM-L6-v2` (runs locally, ~80MB, no API cost) | — |
| Similarity | `numpy` 2.1.x, cosine similarity, brute-force (fine at this scale — hundreds to low thousands of cache rows) | — |
| Dashboard | Streamlit 1.39.x + `pandas` 2.2.x + `plotly` 5.24.x | — |
| Config | YAML via `pyyaml` 6.0.x | — |
| Testing | `pytest` 8.3.x, `pytest-asyncio` 0.24.x, `respx` 0.21.x (mock httpx calls) | — |
| Containerization | Docker + Docker Compose v2 | — |

### 3. Repository / folder structure

```
llm-gateway/
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── gateway/
│   ├── main.py                 # FastAPI app entrypoint
│   ├── config.py                # loads .env + routing_rules.yaml + teams.yaml
│   ├── db.py                    # SQLModel engine/session, init_db()
│   ├── models.py                # SQLModel table classes: Team, RequestLog, CacheEntry
│   ├── schemas.py                # Pydantic request/response models (API contracts)
│   ├── auth.py                   # resolve X-Team-Key -> Team
│   ├── quota.py                   # token budget + RPM checks, raises QuotaExceeded
│   ├── router.py                   # classify_request(), pick_route()
│   ├── cache.py                     # exact_lookup(), semantic_lookup(), store()
│   ├── embeddings.py                 # lazy-loads all-MiniLM-L6-v2 on first embed() call
│   ├── providers/
│   │   ├── base.py                    # ProviderClient ABC: complete(request) -> ProviderResponse
│   │   ├── groq_client.py
│   │   ├── gemini_client.py
│   │   ├── openrouter_client.py
│   │   └── circuit_breaker.py          # per-provider failure tracking
│   ├── cost.py                          # token counting + pricing table -> cost in USD
│   ├── logging_service.py               # write RequestLog rows
│   └── routes/
│       ├── chat.py                       # POST /v1/chat/completions
│       ├── admin.py                       # GET /v1/admin/teams, POST /v1/admin/teams
│       └── health.py                       # GET /healthz
├── config/
│   ├── routing_rules.yaml
│   ├── teams.yaml
│   └── pricing.yaml
├── dashboard/
│   └── app.py                    # Streamlit app
├── tests/
│   ├── conftest.py
│   ├── test_routing.py
│   ├── test_cache.py
│   ├── test_quota.py
│   ├── test_fallback.py
│   └── test_cost_attribution.py
└── data/
    └── gateway.db                 # created at runtime, gitignored
```

### 4. Data models (SQLModel table definitions — implement exactly)

```python
# gateway/models.py

class Team(SQLModel, table=True):
    id: str = Field(primary_key=True)          # e.g. "team-sales"
    name: str
    api_key: str = Field(unique=True, index=True)
    daily_token_budget: int = 200_000
    rpm_limit: int = 20
    created_at: datetime = Field(default_factory=datetime.utcnow)

class RequestLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)
    team_id: Optional[str] = Field(default=None, index=True)  # None for auth failures
    route_decision: str          # "simple" | "complex" | "" (auth failure)
    provider: str                 # "groq" | "gemini" | "openrouter" | "cache"
    model: str
    cache_status: str              # "miss" | "exact" | "semantic"
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    success: bool
    error_message: Optional[str] = None

class CacheEntry(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    cache_key: str = Field(index=True)      # sha256 hash for exact match
    model_tier: str                          # "simple" | "complex"
    prompt_text: str                          # normalized latest user message
    embedding: bytes                           # np.ndarray.tobytes()
    response_json: str                          # serialized full response
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    hit_count: int = 0
```

### 5. Config files (exact formats — the coding agent must produce files matching these schemas)

**`config/teams.yaml`** — seeds the `Team` table on startup if empty:
```yaml
teams:
  - id: team-sales
    name: Sales Team
    api_key: sk-team-sales-demo-key
    daily_token_budget: 100000
    rpm_limit: 15
  - id: team-support
    name: Support Team
    api_key: sk-team-support-demo-key
    daily_token_budget: 150000
    rpm_limit: 20
  - id: team-eng
    name: Engineering Team
    api_key: sk-team-eng-demo-key
    daily_token_budget: 50000
    rpm_limit: 10
```

**`config/routing_rules.yaml`** — defines the classifier thresholds and the
provider chain per tier:
```yaml
classifier:
  # A request is "complex" if EITHER condition is true; else "simple".
  max_simple_chars: 400          # prompt longer than this -> complex
  complex_keywords:               # presence of any (case-insensitive) -> complex
    - "step by step"
    - "analyze"
    - "architecture"
    - "code review"
    - "compare"
    - "explain in detail"
    - "write a"
    - "debug"

routes:
  simple:
    chain:
      - provider: groq
        model: llama-3.1-8b-instant
      - provider: gemini
        model: gemini-2.0-flash
      - provider: openrouter
        model: meta-llama/llama-3.1-8b-instruct:free
  complex:
    chain:
      - provider: groq
        model: llama-3.3-70b-versatile
      - provider: gemini
        model: gemini-2.0-flash
      - provider: openrouter
        model: deepseek/deepseek-chat:free

cache:
  semantic_similarity_threshold: 0.92
  ttl_seconds: 86400              # 24h

circuit_breaker:
  failure_threshold: 3             # consecutive failures before opening
  cooldown_seconds: 60              # time before retrying an open circuit
```

**`config/pricing.yaml`** — used purely for cost *attribution/display* since
providers are free; this lets the dashboard show "notional cost saved" as
if these were paid API calls (useful for the KPI in the PRD, and realistic
for when a team later swaps in paid providers):
```yaml
# USD per 1M tokens, input/output — figures are illustrative published
# list-price figures for the underlying open model classes, used only to
# compute a notional cost for the dashboard.
pricing:
  groq/llama-3.1-8b-instant: { input: 0.05, output: 0.08 }
  groq/llama-3.3-70b-versatile: { input: 0.59, output: 0.79 }
  gemini/gemini-2.0-flash: { input: 0.10, output: 0.40 }
  openrouter/meta-llama/llama-3.1-8b-instruct:free: { input: 0.0, output: 0.0 }
  openrouter/deepseek/deepseek-chat:free: { input: 0.0, output: 0.0 }
```

**`.env.example`**:
```
GROQ_API_KEY=
GEMINI_API_KEY=
OPENROUTER_API_KEY=
GATEWAY_DB_PATH=./data/gateway.db
LOG_LEVEL=INFO
```

### 6. Provider client interface (implement this ABC, one subclass per provider)

```python
# gateway/providers/base.py
class ProviderResponse(BaseModel):
    text: str
    input_tokens: int
    output_tokens: int
    raw: dict

class ProviderClient(ABC):
    name: str
    @abstractmethod
    async def complete(self, model: str, messages: list[dict],
                        temperature: float, max_tokens: int) -> ProviderResponse:
        """Raises ProviderError on any failure (HTTP error, timeout, rate limit)."""
```

- `groq_client.py` calls `https://api.groq.com/openai/v1/chat/completions`
  (OpenAI-compatible; use the `openai` SDK pointed at Groq's base_url, or
  raw `httpx`).
- `gemini_client.py` calls the Gemini API
  (`https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`)
  with `key=` query param from `GEMINI_API_KEY`; map the OpenAI-style
  `messages` list to Gemini's `contents` format inside this client only —
  the rest of the system never sees Gemini's native schema.
- `openrouter_client.py` calls `https://openrouter.ai/api/v1/chat/completions`
  (OpenAI-compatible) with `OPENROUTER_API_KEY`.
- Every client must set an 8-second httpx timeout and raise `ProviderError`
  (defined in `base.py`) on timeout, non-2xx response, or malformed body —
  never let a raw exception escape the client.

### 7. Circuit breaker (per provider, in-memory, process-local)

Simple state machine per provider name, no external dependency:
- States: `closed` (normal), `open` (skip this provider), `half_open`
  (single trial request allowed).
- On `ProviderError`: increment failure count. If count ≥
  `failure_threshold` (config), transition to `open`, record
  `opened_at`.
- While `open`: if `now - opened_at > cooldown_seconds`, transition to
  `half_open` and allow exactly one request through.
- `half_open` success → `closed`, reset failure count. `half_open`
  failure → back to `open`, reset `opened_at`.
- The router must skip any provider currently `open` and go to the next
  in the chain, without waiting for its timeout.

### 8. Routing algorithm (deterministic, implement exactly)

```
def classify(messages, rules) -> "simple" | "complex":
    last_user_msg = last message with role == "user"
    if len(last_user_msg.content) > rules.classifier.max_simple_chars:
        return "complex"
    lowered = last_user_msg.content.lower()
    if any(kw in lowered for kw in rules.classifier.complex_keywords):
        return "complex"
    return "simple"
```

**Retry behaviour (FR3) — implemented in `router.py`:**

`route_request()` iterates the provider chain and, for each provider,
attempts the call up to `MAX_RETRIES + 1` times before advancing to the
next provider. `MAX_RETRIES = 1` (one initial attempt + one retry).
`record_failure()` on the circuit breaker is only called after all retries
for that provider are exhausted — a single transient error does not
immediately trip the breaker or trigger a fallback.

```
for each step in chain:
    if breaker.is_open(): skip
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.complete(...)
            breaker.record_success()
            return response
        except ProviderError:
            if attempt < MAX_RETRIES: continue   # retry same provider
    breaker.record_failure()                      # all retries exhausted
    # advance to next provider
raise AllProvidersFailedError
```

### 9. Cache key & semantic lookup (implement exactly)

- Exact key: `sha256(json.dumps({model_tier, messages, temperature,
  max_tokens}, sort_keys=True))`.
- Semantic lookup: only runs on exact-cache miss. Embed the last user
  message with `all-MiniLM-L6-v2`. Compare against all non-expired
  `CacheEntry` rows with the same `model_tier` using cosine similarity.
  If max similarity ≥ `cache.semantic_similarity_threshold`, return that
  entry's `response_json` and increment its `hit_count`.
- On any provider response (cache miss path), write a new `CacheEntry`
  with `expires_at = now + cache.ttl_seconds`.

**Embedding model loading — lazy, not at import time:**

`gateway/embeddings.py` holds the `SentenceTransformer` instance in a
module-level variable initialised to `None`. The model is loaded on the
first call to `embed()` via an internal `_get_model()` helper. This
ensures test files that import from `gateway` but do not exercise the
cache do not pay the ~300ms model-load penalty on every test run.
The single-load guarantee is preserved: once set, the instance is reused
for the process lifetime.

**Cache eviction:**

Expired rows are filtered at read time by `WHERE expires_at > now`, but
they are never automatically deleted — without explicit cleanup the table
grows without bound. `cache.evict_expired(session)` deletes all rows with
`expires_at <= now` and is called once per startup inside `init_db()` in
`gateway/db.py`.

### 10. Quota & rate limit checks (order of operations in `chat.py`)

1. Resolve team from `X-Team-Key` (401 if unknown key).
2. Check RPM: count `RequestLog` rows for this team in the last 60
   seconds; if ≥ `rpm_limit`, return 429 `{"error": "rate_limited"}`.
3. Check daily token budget: sum `input_tokens + output_tokens` for this
   team since local midnight UTC; if ≥ `daily_token_budget`, return 429
   `{"error": "quota_exceeded"}`.
4. Only after both checks pass: run cache lookup, then routing.
5. Log the request (success or failure) regardless of outcome, including
   429s (with `success=false`, `error_message` set), so the dashboard can
   show throttling events.

**Session injection rule (enforced from Phase 5 onwards):**

Every function in `auth.py`, `quota.py`, and `logging_service.py` must
accept `session: Session` as an explicit parameter — never call
`get_session()` internally. If a helper opens its own session it creates a
second database connection, and writes made in one session will not be
visible in the other within the same request. Only `chat.py` (the route
handler) depends on `get_session` via FastAPI's `Depends()`.

```python
# Correct pattern
def resolve_team(session: Session, api_key: str) -> Team: ...
def check_rpm(session: Session, team: Team) -> None: ...
def log_request(session: Session, ...) -> RequestLog: ...

# Wrong — never do this inside auth/quota/logging helpers
def resolve_team(api_key: str) -> Team:
    session = next(get_session())   # opens a second session
    ...
```

**Logging must cover all exit paths:**

`log_request()` must be called on every code path in `chat.py`, not just
the success path. Use a `try/finally` pattern or explicit catch blocks so
that 401, 429 (rate limit), 429 (quota exceeded), 502, and 504 responses
all produce a `RequestLog` row (with `success=False` and `error_message`
set). Requests that are throttled or rejected must be visible in the
dashboard — invisible failures make quota enforcement unauditable.

**NULL `team_id` in quota queries:**

`RequestLog.team_id` is `Optional[str]` — it is `None` for auth failures
logged before a team is resolved. All RPM and budget queries must include
`WHERE team_id = :team_id` (parameterised) so anonymous rows are never
accidentally aggregated into a team's counts.

### 11. Dashboard data contract

The Streamlit app (`dashboard/app.py`) opens `data/gateway.db` **read-only**
and queries `RequestLog` directly (no separate API needed — same-container
or same-volume access via Docker Compose). Panels required:
1. Total spend (sum `cost_usd`) — big number, today vs. all-time.
2. Spend by team — bar chart.
3. Spend by model — bar chart.
4. Request volume over time — line chart, hourly buckets.
5. Cache hit rate — `(exact+semantic) / total requests`, as a percentage.
6. Anomaly banner: for each team, compute current-hour spend vs. average
   hourly spend over the last 7 days; if current > 3× average AND
   current-hour request count ≥ 5 (avoid false alarms on tiny samples),
   show a red banner: `"⚠ {team} spending {X}x above normal this hour."`

### 12. Deployment

**Local (required, must work with zero config beyond `.env`):**
```
docker compose up --build
```
`docker-compose.yml` defines two services: `gateway` (port 8000) and
`dashboard` (port 8501), both mounting `./data` as a shared volume so the
dashboard reads what the gateway writes.

**Free-tier hosting (documented, optional for the demo):**
- Gateway → Render.com free Web Service (Dockerfile deploy). Note: free
  tier sleeps after inactivity — document this as a known limitation, not
  a bug.
- Dashboard → Streamlit Community Cloud (free), pointed at a small
  Postgres instead of SQLite if the dashboard needs to run in a separate
  process from the gateway with no shared filesystem — use Supabase's free
  Postgres tier for this case. **This swap is optional**; local Docker
  Compose with SQLite is the primary, required deployment target for
  grading/demo purposes.
