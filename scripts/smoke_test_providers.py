#!/usr/bin/env python
# scripts/smoke_test_providers.py
# Manual smoke test — sends one request to each provider directly (bypassing
# the gateway) using real API keys from .env. Run this to verify your keys work
# before running the full gateway.
#
# Usage:
#   .venv\Scripts\python.exe scripts/smoke_test_providers.py
#
# Requires: GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY set in .env
# This script is NOT part of the automated test suite — it costs real quota.

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Ensure repo root is on the path when running as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from gateway.providers.groq_client import GroqClient
from gateway.providers.gemini_client import GeminiClient
from gateway.providers.openrouter_client import OpenRouterClient
from gateway.providers.base import ProviderError

PROMPT = [{"role": "user", "content": "Say hello in exactly 3 words."}]
MODEL_MAP = {
    "groq": "llama-3.1-8b-instant",
    "gemini": "gemini-2.0-flash",
    "openrouter": "meta-llama/llama-3.1-8b-instruct:free",
}

CLIENTS = [
    GroqClient(),
    GeminiClient(),
    OpenRouterClient(),
]


async def test_provider(client, model: str) -> None:
    print(f"\n{'─' * 50}")
    print(f"Provider : {client.name}")
    print(f"Model    : {model}")
    start = time.monotonic()
    try:
        response = await client.complete(
            model=model,
            messages=PROMPT,
            temperature=0.3,
            max_tokens=32,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        print(f"Status   : ✅ OK  ({elapsed_ms}ms)")
        print(f"Response : {response.text.strip()!r}")
        print(f"Tokens   : in={response.input_tokens}  out={response.output_tokens}")
    except ProviderError as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        print(f"Status   : ❌ FAILED  ({elapsed_ms}ms)")
        print(f"Error    : {exc}")


async def main() -> None:
    print("LLM Gateway — Provider Smoke Test")
    print("==================================")
    print(f"Prompt: {PROMPT[0]['content']!r}")
    for client in CLIENTS:
        model = MODEL_MAP[client.name]
        await test_provider(client, model)
    print(f"\n{'─' * 50}")
    print("Done. Check ✅/❌ above for each provider.")


if __name__ == "__main__":
    asyncio.run(main())
