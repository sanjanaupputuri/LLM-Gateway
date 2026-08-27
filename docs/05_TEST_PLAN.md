# 05 — Test Plan

## Document Control
| Field | Value |
|---|---|
| Project | Project 19 — LLM Gateway: Routing, Caching & Cost Control |
| Document | Test Plan |
| Version | 1.0 |
| Status | Approved for build |
| Related documents | 01_PRD.md, 02_ARCHITECTURE.md, 03_API_CONTRACTS.md, 04_BUILD_PLAN.md |

---

## 1. Purpose and scope

This document defines the concrete tests that prove every functional
requirement in `01_PRD.md` and every capability required by the catalog
scenario is implemented and working. It covers three layers:

1. **Automated unit/integration tests** (`pytest`, provider calls mocked
   with `respx` — no real API keys or cost involved).
2. **Manual QA scenarios** (run against the live system with real,
   free-tier API keys) — these are what a reviewer or the builder runs
   before calling the project "done."
3. **Demo script** (`scripts/demo_traffic.py`) — the single command that
   proves all six required capabilities in one pass, for a live
   walkthrough or presentation.

A build is not complete until every test in Section 3 passes and every
scenario in Section 4 has been manually verified at least once against
real providers.

---

## 2. Test environment

| Item | Value |
|---|---|
| Automated tests | Run via `pytest` from repo root; use in-memory SQLite (`sqlite://`); all provider HTTP calls intercepted by `respx` — zero cost, zero network dependency, safe for CI. |
| Manual QA | Run against `docker compose up --build`; requires real `GROQ_API_KEY` and `GEMINI_API_KEY` in `.env` (both free, no card). `OPENROUTER_API_KEY` optional — only needed to test the tertiary fallback path. |
| Test data | Fixture teams: `team-sales`, `team-support`, `team-eng` as seeded in `config/teams.yaml`. |

---

## 3. Automated test suite

### 3.1 `tests/test_routing.py` — Routing correctness

| ID | Case | Input | Expected |
|---|---|---|---|
| RT-01 | Short, plain prompt | `"What's the capital of France?"` | `classify() -> "simple"` |
| RT-02 | Long prompt (>400 chars) | A 500-character prompt with no keywords | `classify() -> "complex"` |
| RT-03 | Keyword trigger | `"Can you analyze this dataset?"` (short) | `classify() -> "complex"` |
| RT-04 | Chain lookup — simple tier | tier=`"simple"` | Chain returned is exactly `[groq/llama-3.1-8b-instant, gemini/gemini-2.0-flash, openrouter/...:free]` in that order, matching `routing_rules.yaml` |
| RT-05 | Chain lookup — complex tier | tier=`"complex"` | Chain returned starts with `groq/llama-3.3-70b-versatile` |

### 3.2 `tests/test_fallback.py` — Provider fallback & circuit breaker

| ID | Case | Setup | Expected |
|---|---|---|---|
| FB-01 | Primary succeeds | Mock Groq 200 | Response comes from `groq`; Gemini/OpenRouter never called (assert mock call counts = 0) |
| FB-02 | Primary fails once, retries, still fails, falls over | Mock Groq to return 500 both attempts, Gemini 200 | Response comes from `gemini`; Groq called exactly twice (1 try + 1 retry), Gemini called once |
| FB-03 | Primary and secondary fail, tertiary succeeds | Mock Groq 500, Gemini 500, OpenRouter 200 | Response comes from `openrouter` |
| FB-04 | All providers fail | Mock all three to 500 | Endpoint returns HTTP 502 `all_providers_failed`; response is never silently empty |
| FB-05 | Circuit breaker opens | Force 3 consecutive `ProviderError`s from Groq (below `failure_threshold`) | 4th request skips Groq entirely — Groq mock is not called; request goes straight to Gemini |
| FB-06 | Circuit breaker half-open recovery | After FB-05, advance mocked clock past `cooldown_seconds`, mock Groq to succeed | Next request calls Groq (half-open trial), succeeds, breaker returns to `closed` |
| FB-07 | Timeout handling | Mock Groq to hang past 8s | Request fails over to Gemini within a bounded total time (assert < 9.5s), not a hang |

### 3.3 `tests/test_cache.py` — Caching

| ID | Case | Setup | Expected |
|---|---|---|---|
| CA-01 | Exact cache miss then hit | Send identical request twice | 1st: `cache_status: "miss"`, provider called once. 2nd: `cache_status: "exact"`, provider call count unchanged (not called again) |
| CA-02 | Semantic cache hit | Send `"Summarize the Q3 earnings report"`, then a paraphrase `"Give me a summary of the Q3 earnings report"` | 2nd request: `cache_status: "semantic"`, no provider call |
| CA-03 | Below similarity threshold | Send two clearly unrelated prompts in the same tier | 2nd request: `cache_status: "miss"` (semantic lookup does not false-positive) |
| CA-04 | TTL expiry | Insert a `CacheEntry` with `expires_at` in the past, then request the same prompt | `cache_status: "miss"` — expired entries are not returned |
| CA-05 | Cache is tier-scoped | Same prompt text sent once classified `"simple"` and once forced `"complex"` | No cross-tier cache hit — each tier caches independently |

### 3.4 `tests/test_quota.py` — Quotas & rate limiting

| ID | Case | Setup | Expected |
|---|---|---|---|
| QT-01 | Under budget | Team with `daily_token_budget: 10000`, 2000 tokens used so far | Request proceeds normally (200) |
| QT-02 | Over budget | Same team, next request would push usage over 10000 | HTTP 429 `quota_exceeded`; provider mock call count unchanged (no call made) |
| QT-03 | RPM limit | Team with `rpm_limit: 5`; send 6 requests within 60 seconds | 6th request returns HTTP 429 `rate_limited`; `retry_after_seconds` present and > 0 |
| QT-04 | RPM window rolls over | After QT-03, wait (or mock clock past) 60 seconds | Next request succeeds (200) |
| QT-05 | Invalid API key | Request with `X-Team-Key: does-not-exist` | HTTP 401 `invalid_api_key`; no DB row written for a team, but the failed attempt is still logged (with `team_id: null` or a sentinel) for audit purposes |
| QT-06 | 429s are logged | Trigger QT-02 | A `RequestLog` row exists for the rejected request with `success: false`, `error_message` set — visible in the dashboard's request volume, not silently dropped |

### 3.5 `tests/test_cost_attribution.py` — Cost calculation

| ID | Case | Setup | Expected |
|---|---|---|---|
| CO-01 | Known token counts, priced model | Mock Groq response: 1000 input / 500 output tokens, model `llama-3.1-8b-instant` | `cost_usd == (1000/1_000_000)*0.05 + (500/1_000_000)*0.08` exactly |
| CO-02 | Free model | Response from `openrouter/...:free` | `cost_usd == 0.0` |
| CO-03 | Cache hit | Response served from cache | `cost_usd == 0.0`, `input_tokens`/`output_tokens` still reflect the original cached values (for dashboard token-volume accuracy) |
| CO-04 | Unknown model in pricing table | Response from a model not listed in `pricing.yaml` | `cost_usd == 0.0`, a warning is logged, request does not fail |

---

## 4. Manual QA scenarios (run against live providers)

These map directly to the catalog's six required capabilities. Run each
once against the real running system and record pass/fail.

| # | Scenario | Steps | Pass criteria |
|---|---|---|---|
| 1 | Single endpoint fronts multiple providers | Send 5 varied requests via `curl` to `/v1/chat/completions` | Response `provider` field shows at least 2 different providers across the batch (routing is actually happening, not hardcoded) |
| 2 | Task-based routing + fallback | Send one short factual prompt and one long "analyze this in detail..." prompt | Short prompt routes `"simple"` tier, long one routes `"complex"` tier — visible in response `route_decision` |
| 2b | Fallback under real failure | Temporarily set an invalid `GROQ_API_KEY` in `.env`, restart, send a request | Request still succeeds, served by Gemini; `provider: "gemini"` in response |
| 3 | Caching | Send the exact same request twice, then a paraphrased version a third time | 2nd response `cache_status: "exact"`; 3rd response `cache_status: "semantic"`; both near-instant (<100ms) vs. the ~500-1500ms of the first call |
| 4 | Quotas & rate limits | Temporarily set `team-eng`'s `rpm_limit: 3` in `teams.yaml`, restart, send 4 rapid requests | 4th request returns 429 |
| 5 | Spend dashboard + anomaly alert | Run `scripts/demo_traffic.py`, then open the dashboard | Spend-by-team and spend-by-model charts populated; cache-hit-rate panel shows a non-zero percentage |
| 5b | Anomaly alert trigger | Manually send 10 rapid requests from one team (enough to be 3x its recent hourly average) | Red anomaly banner appears in the dashboard for that team within one refresh cycle (≤10s) |
| 6 | Cost attribution | Open the dashboard's spend-by-team panel after running the demo script | Every team that made requests shows a non-negative `cost_usd` total; sum of per-team costs equals the dashboard's total spend figure |

---

## 5. Regression checklist (run before any demo/presentation)

- [ ] `docker compose up --build` starts cleanly with no errors in either
      container's logs.
- [ ] `GET /healthz` returns 200 with all three providers `"closed"`.
- [ ] `python scripts/demo_traffic.py` completes without unhandled
      exceptions.
- [ ] Dashboard loads at `:8501` and all 6 panels render with data (no
      panel stuck on "no data").
- [ ] `pytest` — full suite — passes with 0 failures.
- [ ] `.env` with real keys is **not** committed to git (`git status`
      shows it untracked/ignored).

---

## 6. Known limitations to disclose (not defects)

- No streaming responses (NG2 in the PRD) — acceptable per scope.
- No admin authentication on `/v1/admin/*` (documented simplification for
  a student project, not a production posture).
- Free-tier providers impose their own daily/rate caps outside this
  system's control; if all three are simultaneously exhausted, the
  gateway correctly returns 502 rather than hanging — this is the system
  working as designed, not a bug, and should be stated as such if it
  happens live during a demo.
