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
| R-03 | High | **`pricing.yaml` key format coupling** — `cost.py` (Phase 6) must build the lookup key as exactly `f"{provider}/{model}"` to match the keys in `pricing.yaml`. Any deviation silently returns `cost_usd=0.0` with only a log warning. Must be verified with test CO-01. | Phase 6 (cost.py) | Key format is `groq/llama-3.1-8b-instant` etc. |
| R-04 | Medium | **`sentence-transformers` installs ~2GB of transitive deps** (torch, transformers, tokenizers). First `pip install` will be slow and surprising. Must be called out in README and in the Dockerfile layer ordering (install heavy deps first so Docker cache is effective). | Phase 8 (README) / Phase 9 (Dockerfile) | Not a bug — just a UX/DX issue |
| R-05 | Low | **`lru_cache` on config loaders** — tests that modify `os.environ` directly (not via pytest `monkeypatch`) and forget to call `clear_config_cache()` will read stale config silently. The `autouse` `clear_caches` fixture in `conftest.py` runs before/after every test, which mitigates this. | Ongoing — watch in Phases 3–6 | Always use `monkeypatch.setenv` in tests, never `os.environ[...] =` directly |
| R-11 | Medium | **Circuit breaker state is process-local and resets on restart** — if the gateway restarts mid-incident, all breakers reset to CLOSED and the failing provider gets hammered again. Acceptable for demo (per spec), but document in README. | Phase 10 (README) | By design per spec — no persistence needed |
| R-12 | Low | **`_run_chain` helper in test_fallback.py duplicates router logic** — if router.py (Phase 3) diverges from the helper, fallback tests won't catch real router bugs. In Phase 3, refactor test_fallback.py to import and use the real `route_request()` function. | Phase 3 (router.py) | Currently tests provider layer only — router integration tested in Phase 3 |

---

## Resolved

| ID | Description | Resolved in | Fix applied |
|----|-------------|-------------|-------------|
| R-07 | **`python-dotenv` missing from spec** — `02_ARCHITECTURE.md §2` does not list it but `config.py` needs it to load `.env`. | Phase 0 | Added `python-dotenv==1.0.1` to `requirements.txt` |
| R-08 | **`asyncio_default_fixture_loop_scope` unset** — `pytest-asyncio 0.24.x` emits a `PytestDeprecationWarning` if not set, and future versions will change default behavior. | Phase 1 | Added `asyncio_default_fixture_loop_scope = function` to `pytest.ini` |
| R-09 | **Pydantic `model_tier` namespace warning** — Pydantic reserves the `model_` prefix; `CacheEntry.model_tier` triggered a `UserWarning` at import. | Phase 1 | Added `model_config = ConfigDict(protected_namespaces=())` to `CacheEntry` |
| R-10 | **Broken global `deepeval` pytest plugin** — globally-installed `deepeval` package crashes pytest startup with `ModuleNotFoundError: langchain.schema`. `-p no:deepeval` in `pytest.ini` does not help because the crash happens before `pytest.ini` is read. | Phase 1 | Created `.venv/` project virtualenv; all subsequent `pytest` runs must use `.venv\Scripts\python.exe -m pytest` |
| R-06 | **`respx` version compatibility with `httpx 0.27.x`** — `respx==0.21.1` was pinned but not yet verified against real provider client code. | Phase 2 | Verified — `respx 0.21.1` + `httpx 0.27.2` work correctly together. Also fixed `respx.pattern.url` typo (should be plain `mock.post(url)`) in FB-01. |

---

## Accepted Limitations (not bugs)

| ID | Description | Reason accepted |
|----|-------------|-----------------|
| A-01 | No streaming responses | Explicitly NG2 in PRD — out of scope for v1 |
| A-02 | No admin auth on `/v1/admin/*` | Documented simplification for student/demo project per PRD NG1 |
| A-03 | Free-tier provider daily caps outside gateway's control | System correctly returns 502 when all providers exhausted — working as designed |

---

## Session Continuity Notes

**Always run tests with the venv:**
```
.venv\Scripts\python.exe -m pytest
```

**Current build state:**
- Phase 0 ✅ — scaffolding, config files, folder structure
- Phase 1 ✅ — models, db, config loaders, conftest, pytest.ini
- Phase 2 ✅ — provider clients (groq, gemini, openrouter, circuit breaker) + FB tests 7/7
- Phase 3 ⬜ — routing engine
- Phase 4 ⬜ — caching
- Phase 5 ⬜ — quota & rate limiting
- Phase 6 ⬜ — cost attribution & logging + main FastAPI app wired
- Phase 7 ⬜ — admin & health endpoints
- Phase 8 ⬜ — Streamlit dashboard
- Phase 9 ⬜ — Docker + demo script
- Phase 10 ⬜ — README & handoff

**Key file locations:**
- Config: `config/teams.yaml`, `config/routing_rules.yaml`, `config/pricing.yaml`
- DB engine: `gateway/db.py` → `_build_engine()`, `init_db()`, `get_session()`
- Models: `gateway/models.py` → `Team`, `RequestLog`, `CacheEntry`
- Config loaders: `gateway/config.py` → `get_routing_rules()`, `get_pricing()`, `get_settings()`
- Test fixtures: `tests/conftest.py`
