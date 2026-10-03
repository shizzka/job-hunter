"""Opt-in synthetic LAN smoke for the temporary emergency compatibility path."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import llm_client


async def smoke(base_url: str, selected_models: list[str]) -> bool:
    # Explicit ProviderSpecs avoid loading private provider files or singleton config.
    # Do not write even synthetic metadata to a production analytics destination.
    llm_client._record_usage = lambda *args, **kwargs: None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), trust_env=False) as http:
            models = await asyncio.wait_for(http.get(base_url + "/models"), timeout=10)
            models.raise_for_status()
            available = {entry["id"] for entry in models.json()["data"]}
        print(json.dumps({"models_reachable": True, "expected_models_present": {
            model: model in available for model in ("qwen3-coder:30b", "qwen3:8b")}}), flush=True)
    except Exception as exc:
        print(json.dumps({"models_reachable": False, "error_kind": type(exc).__name__}), flush=True)
        return False

    ok = True
    for provider, actual in (("ollama", "qwen3-coder:30b"), ("ollama2", "qwen3:8b")):
        if actual not in selected_models:
            continue
        client = llm_client.FallbackLLMClient([llm_client.ProviderSpec(
            provider, base_url, "ollama", text_fallback_model=actual, timeout_seconds=60)])
        start = time.monotonic()
        result = {"provider": provider, "requested_model": "gpt-oss:120b", "actual_model": actual}
        try:
            reply = await client.chat.completions.create(
                model="gpt-oss:120b", messages=[{"role": "user", "content":
                    "/no_think\nReply with exactly LAN_SMOKE_OK. No explanation."}],
                max_tokens=96, temperature=0)
            choice = reply.choices[0]
            valid = (reply.model == actual and choice.finish_reason == "stop"
                     and choice.message.content.strip() == "LAN_SMOKE_OK")
            result.update(reachable=True, returned_model=reply.model, valid=valid)
            ok = ok and valid
        except Exception as exc:
            result.update(reachable=False, valid=False, error_kind=type(exc).__name__)
            ok = False
        finally:
            result["latency_seconds"] = round(time.monotonic() - start, 3)
            await client.aclose()
        print(json.dumps(result), flush=True)
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Explicit trusted LAN Ollama /v1 URL")
    parser.add_argument("--allow-network", action="store_true", help="Authorize synthetic network calls")
    parser.add_argument("--models", nargs="+", choices=["qwen3-coder:30b", "qwen3:8b"],
                        default=["qwen3-coder:30b", "qwen3:8b"])
    args = parser.parse_args()
    url = args.base_url.strip().rstrip("/")
    parsed = urlsplit(url)
    if (not args.allow_network or parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/v1"):
        parser.error("Require --allow-network and a credential-free trusted LAN /v1 URL")
    sys.exit(0 if asyncio.run(smoke(url, args.models)) else 1)
