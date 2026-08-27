# 03 — API Contracts

## Document Control
| Field | Value |
|---|---|
| Project | Project 19 — LLM Gateway: Routing, Caching & Cost Control |
| Document | API Contracts |
| Version | 1.0 |
| Status | Approved for build |
| Related documents | 02_ARCHITECTURE.md, 04_BUILD_PLAN.md, 05_TEST_PLAN.md |

## Executive Summary
Four endpoints make up the entire public surface: the core chat-completion
gateway, two lightweight admin endpoints for team management, and a health
check. The core endpoint is deliberately OpenAI-compatible so it can be
adopted with a one-line `base_url` change in any existing client.

All endpoints are served by the FastAPI app at base URL `http://localhost:8000`.

---

## POST /v1/chat/completions

The core gateway endpoint. OpenAI-compatible shape so any existing OpenAI
client can point at this with a different `base_url`.

**Headers**
| Header | Required | Description |
|---|---|---|
| `X-Team-Key` | Yes | Team's API key, defined in `config/teams.yaml` |
| `Content-Type` | Yes | `application/json` |

**Request body**
```json
{
  "messages": [
    {"role": "user", "content": "Summarize this changelog in 3 bullets: ..."}
  ],
  "temperature": 0.7,
  "max_tokens": 512
}
```
| Field | Type | Required | Notes |
|---|---|---|---|
| `messages` | array of `{role, content}` | Yes | `role` ∈ `system`, `user`, `assistant`. Must contain at least one `user` message. |
| `temperature` | float | No | Default `0.7`. Range 0.0–2.0. |
| `max_tokens` | int | No | Default `512`. Max `4096`. |

**Success response — 200**
```json
{
  "id": "req_a1b2c3",
  "route_decision": "simple",
  "provider": "groq",
  "model": "llama-3.1-8b-instant",
  "cache_status": "miss",
  "message": {"role": "assistant", "content": "..."},
  "usage": {
    "input_tokens": 142,
    "output_tokens": 87,
    "cost_usd": 0.0000178
  },
  "latency_ms": 612
}
```

**Error responses**
| Status | Body | Trigger |
|---|---|---|
| 401 | `{"error": "invalid_api_key"}` | Missing/unknown `X-Team-Key` |
| 400 | `{"error": "invalid_request", "detail": "..."}` | Malformed body (Pydantic validation failure) |
| 429 | `{"error": "rate_limited", "retry_after_seconds": 12}` | RPM limit exceeded |
| 429 | `{"error": "quota_exceeded", "reset_at": "2026-08-26T00:00:00Z"}` | Daily token budget exceeded |
| 502 | `{"error": "all_providers_failed", "detail": "..."}` | Every provider in the fallback chain failed |
| 504 | `{"error": "timeout"}` | All providers timed out |

---

## GET /v1/admin/teams

Lists all configured teams and their current usage (no secrets exposed).

**Headers:** none required for this project's scope (no admin auth — note
this explicitly as a known simplification, not a gap to silently ignore).

**Response — 200**
```json
{
  "teams": [
    {
      "id": "team-sales",
      "name": "Sales Team",
      "daily_token_budget": 100000,
      "tokens_used_today": 23421,
      "rpm_limit": 15
    }
  ]
}
```

## POST /v1/admin/teams

Adds a new team at runtime (writes to the `Team` table; does not persist
back to `teams.yaml`).

**Request body**
```json
{
  "id": "team-marketing",
  "name": "Marketing Team",
  "api_key": "sk-team-marketing-demo-key",
  "daily_token_budget": 75000,
  "rpm_limit": 10
}
```
**Response — 201** the created team object (same shape as GET, `tokens_used_today: 0`).
**Response — 409** `{"error": "team_id_exists"}` if `id` already present.

---

## GET /healthz

Liveness check for Docker/hosting platforms.

**Response — 200**
```json
{"status": "ok", "providers": {"groq": "closed", "gemini": "closed", "openrouter": "closed"}}
```
`providers` reflects each circuit breaker's current state
(`closed`/`open`/`half_open`), useful for the dashboard's provider-health
panel and for debugging during the demo.

---

## Response metadata fields — precise definitions (no ambiguity)

- `route_decision`: `"simple"` or `"complex"`, the classifier's output —
  always present, even on a cache hit (the tier that was looked up).
- `provider`: which provider actually served the response. Value is
  `"cache"` when served from cache (not the original provider that
  produced the cached response); the dashboard's per-provider spend chart
  should therefore use `cost_usd` (which is `0` on cache hits) rather than
  `provider` alone to compute savings.
- `cache_status`: `"miss"`, `"exact"`, or `"semantic"`.
- `usage.cost_usd`: computed from `config/pricing.yaml`; `0.0` for any
  `openrouter/*:free` model and for all cache hits.
