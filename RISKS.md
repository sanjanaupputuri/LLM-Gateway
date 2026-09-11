# LLM Gateway — Risk & Decision Log

This file is updated at the end of every phase. It is the single source of
truth for open risks, resolved issues, and important decisions made during
the build. Read this at the start of any new session before continuing.

---

## How to read this file

- **OPEN** — risk is real, not yet mitigated, must be addressed in the listed phase
- **RESOLVED** — was a risk, has been fixed (phase noted)
- **ACCEPTED** — known limitation, consciously not fixing (reason given)

---

## Open Risks

| ID | Severity | Description | Must fix in | Notes |
|----|----------|-------------|-------------|-------|
| R-01 | Medium | **Python 3.13.3 on host vs spec's 3.11** — all code runs fine on 3.13 locally, but the Docker image must pin `python:3.11-slim` per spec or there will be a dev/container mismatch. | Phase 9 (Dockerfile) | Do not use `python:latest` in the Dockerfile |
| R-02 | Low | **`init_db()` concurrent startup race** — two gateway instances starting simultaneously on an empty DB both see 0 rows and both try to seed teams. The second will raise an `IntegrityError` on the `api_key` unique constraint instead of silently double-inserting. Not a problem for single-instance demo but needs a `try/except IntegrityError` guard. | Phase 9 (docker-compose) | Single instance only for demo — acceptable to defer |
| R-03 | High | **`pricing.yaml` key format coupling** — `cost.py` (Phase 6) must build the lookup key as exactly `f"{provider}/{model}"` to match the keys in `pricing.yaml`. Any deviation silently returns `cost_usd=0.0` with only a log warning. Must be verified with test CO-01 using a **non-zero-price model** (Groq or Gemini) — testing only with free OpenRouter models would pass vacuously. | Phase 6 (cost.py) | Key format is `groq/llama-3.1-8b-instant` etc. |
| R-04 | Medium | **`sentence-transformers` installs ~2GB of transitive deps** (torch, transformers, tokenizers). First `pip install` will be slow and surprising. Must be called out in README and in the Dockerfile layer ordering (install heavy deps first so Docker cache is effective). | Phase 8 (README) / Phase 9 (Dockerfile) | Not a bug — just a UX/DX issue |
| R-05 | Low | **`lru_cache` on config loaders** — tests that modify `os.environ` directly (not via pytest `monkeypatch`) and forget to call `clear_config_cache()` will read stale config silently. The `autouse` `clear_caches` fixture in `conftest.py` runs before/after every test, which mitigates this. | Ongoing — watch in Phases 3–6 | Always use `monkeypatch.setenv` in tests, never `os.environ[...] =` directly |
| R-11 | Medium | **Circuit breaker state is process-local and resets on restart** — if the gateway restarts mid-incident, all breakers reset to CLOSED and the failing provider gets hammered again. Acceptable for demo (per spec), but document in README. | Phase 10 (README) | By design per spec — no persistence needed |
| R-13 | Medium | **Quota check raises before logging — 429s will be invisible to dashboard** — the quota/rate-limit check in `chat.py` raises an exception before the provider is called. If the logging call is placed only on the success path, throttled requests won't appear in `RequestLog` or the dashboard. The logging call **must** be in a `finally` block (or equivalent catch-and-log on every exit path). | Phase 6 (chat.py) | Every exit path — 401, 429 (rate limit), 429 (quota), 502, 504, 200 — must write a RequestLog row |
| R-14 | Low | **NULL `team_id` in quota queries** — `RequestLog.team_id` is `Optional[str]` (None for auth failures). RPM and daily-budget queries that `WHERE team_id = :id` are safe, but any query that aggregates without filtering `team_id IS NOT NULL` could accidentally mix anonymous rows into a team's count. | Phase 5 (quota.py) | Verify all quota queries include explicit `team_id IS NOT NULL` or are parameterized by team |
| R-15 | Low | **`lru_cache` config — no hot-reload** — config changes require a full restart. The `/healthz` endpoint (Phase 7) should surface which config was loaded and when, so an operator can confirm the running config matches intent. | Phase 7 (health.py) | Acceptable limitation — document in README |
| R-16 | Low | **Dashboard DB path hard-coded to SQLite** — `dashboard/app.py` will need to read `GATEWAY_DB_PATH` (or a `DATABASE_URL`) from environment, not hard-code a path, to keep the optional Render + Streamlit Cloud deployment path viable without a rewrite. | Phase 8 (dashboard/app.py) | Use `os.getenv("GATEWAY_DB_PATH", "./data/gateway.db")` |
| R-17 | Medium | **Session must be injected, never called directly** — every function in `auth.py`, `quota.py`, `logging_service.py` must accept `session: Session` as a parameter. If any of them calls `get_session()` internally they will open a second session, and writes in one won't be visible in the other within the same request. Only `chat.py` (the route handler) should depend on `get_session` via FastAPI DI. | Phase 5 (auth.py, quota.py) / Phase 6 (logging_service.py) | Pattern: `def resolve_team(session: Session, api_key: str) -> Team` |

---

## Resolved

| ID | Description | Resolved in | Fix applied |
|----|-------------|-------------|-------------|
| R-06 | **`respx` version compatibility with `httpx 0.27.x`** | Phase 2 | Verified — `respx 0.21.1` + `httpx 0.27.2` work correctly together |
| R-07 | **`python-dotenv` missing from spec** — `config.py` needs it to load `.env` | Phase 0 | Added `python-dotenv==1.0.1` to `requirements.txt` |
| R-08 | **`asyncio_default_fixture_loop_scope` unset** — `pytest-asyncio 0.24.x` deprecation warning | Phase 1 | Added `asyncio_default_fixture_loop_scope = function` to `pytest.ini` |
| R-09 | **Pydantic `model_tier` namespace warning** — `CacheEntry.model_tier` triggered `UserWarning` | Phase 1 | Added `model_config = ConfigDict(protected_namespaces=())` to `CacheEntry` |
| R-10 | **Broken global `deepeval` pytest plugin** crashes pytest startup | Phase 1 | Created `.venv/` project virtualenv; all pytest runs must use `.venv/bin/python -m pytest` |
| R-12 | **`_run_chain` helper in `test_fallback.py` duplicated router logic** — fallback tests didn't exercise the real router | Phase 3 | Rewrote `test_fallback.py` to import and call the real `route_request()`. Deleted `_run_chain`. |
| R-18 | **`route_request()` had no retry before fallback** — a single `ProviderError` immediately advanced the chain, violating FR3 ("retry once, then fail over") | Phase 3 | Added `MAX_RETRIES = 1` to `router.py`; each provider is now attempted `MAX_RETRIES+1` times before `record_failure()` is called and the chain advances |
| R-19 | **`embeddings.py` loaded model at import time** — any test that transitively imported `gateway.embeddings` paid the ~300ms model-load penalty even without needing embeddings | Phase 4 | Changed to lazy init: `_model = None` at module level; `_get_model()` loads on first `embed()` call |
| R-20 | **`cache.py store()` had dead code and misplaced import** — intermediate `expires_at` assignment was immediately overwritten; `timedelta` was imported inside the function body | Phase 4 | Removed dead assignment; moved `timedelta` to top-level imports |
| R-21 | **No cache eviction** — expired `CacheEntry` rows were filtered at read time but never deleted; DB would grow without bound | Phase 4 | Added `evict_expired(session)` to `cache.py`; called once at startup from `init_db()` in `db.py` |

---

## Accepted Limitations (not bugs)

| ID | Description | Reason accepted |
|----|-------------|-----------------|
| A-01 | No streaming responses | Explicitly NG2 in PRD — out of scope for v1 |
| A-02 | No admin auth on `/v1/admin/*` | Documented simplification for student/demo project per PRD NG1 |
| A-03 | Free-tier provider daily caps outside gateway's control | System correctly returns 502 when all providers exhausted — working as designed |
| A-04 | Brute-force cosine similarity in semantic lookup | Acceptable at this scale (hundreds to low thousands of cache rows per spec). No ANN index needed. |
| A-05 | `hit_count` incremented inside lookup functions (side-effect write) | Intentional — keeps the hit-count logic co-located with the lookup. Documented here so Phase 6 implementers are not surprised by writes during reads. |

---

## Session Continuity Notes

**Always run tests with the venv:**
```
.venv/bin/python -m pytest
```

**Current build state:**
- Phase 0 ✅ — scaffolding, config files, folder structure
- Phase 1 ✅ — models, db, config loaders, conftest, pytest.ini
- Phase 2 ✅ — provider clients (groq, gemini, openrouter, circuit breaker) + FB tests
- Phase 3 ✅ — routing engine (classify, get_chain, route_request with retry)
- Phase 4 ✅ — caching (exact + semantic, eviction, lazy embeddings)
- Phase 5 ⬜ — quota & rate limiting
- Phase 6 ⬜ — cost attribution & logging + main FastAPI app wired
- Phase 7 ⬜ — admin & health endpoints
- Phase 8 ⬜ — Streamlit dashboard
- Phase 9 ⬜ — Docker + demo script
- Phase 10 ⬜ — README & handoff

**Test counts by phase:**
- Phase 2: 8 fallback tests (test_fallback.py)
- Phase 3: 25 routing tests (test_routing.py)
- Phase 4: 14 cache tests (test_cache.py)
- Total passing: 47

**Key file locations:**
- Config: `config/teams.yaml`, `config/routing_rules.yaml`, `config/pricing.yaml`
- DB engine: `gateway/db.py` → `_build_engine()`, `init_db()`, `get_session()`
- Models: `gateway/models.py` → `Team`, `RequestLog`, `CacheEntry`
- Config loaders: `gateway/config.py` → `get_routing_rules()`, `get_pricing()`, `get_settings()`
- Router: `gateway/router.py` → `classify()`, `get_chain()`, `route_request()`, `MAX_RETRIES`
- Cache: `gateway/cache.py` → `make_cache_key()`, `exact_lookup()`, `semantic_lookup()`, `store()`, `evict_expired()`
- Embeddings: `gateway/embeddings.py` → `embed()` (lazy-loads model on first call)
- Test fixtures: `tests/conftest.py`
