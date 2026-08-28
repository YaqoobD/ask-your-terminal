"""One interface, four backends. `get_provider(role)` reads $ASK_PROVIDER
("claude" for the direct Anthropic API, "bedrock" for Claude via AWS
Bedrock, or "ollama") and returns the model for the given role: "intent"
(Claude Opus 5) or "narrate" (Claude Sonnet 5). Ollama serves both roles from
one local model, documented as reproducibility mode with expected quality
degradation.
"""

from __future__ import annotations

import os
from typing import Protocol

INTENT_MODEL = "claude-opus-5"
NARRATE_MODEL = "claude-sonnet-5"
OLLAMA_MODEL = os.environ.get("ASK_OLLAMA_MODEL", "llama3")

# USD per 1M tokens (input, output), Anthropic first-party API rates as of 2026-06-24.
# Claude on Bedrock is partner-priced separately, so a Bedrock-backed run's cost here is
# an estimate against these rates, not an actual bill. Ollama is local: always $0 below,
# regardless of token counts, because there is no per-token API charge to estimate.
PRICING_USD_PER_MTOK = {
    INTENT_MODEL: (5.00, 25.00),
    NARRATE_MODEL: (2.00, 10.00),
}


def estimate_cost_usd(role: str, input_tokens: int, output_tokens: int) -> float:
    if os.environ.get("ASK_PROVIDER") == "ollama":
        return 0.0
    model = INTENT_MODEL if role == "intent" else NARRATE_MODEL
    in_price, out_price = PRICING_USD_PER_MTOK[model]
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


class Provider(Protocol):
    last_usage: dict

    def complete(self, *, system: str, user: str) -> str: ...


def _first_text_block(response) -> str:
    """Some models (Sonnet 5 with extended thinking) put a ThinkingBlock
    ahead of the TextBlock, so the text is never reliably content[0].
    """
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError(f"no text block in response content: {response.content!r}")


class ClaudeProvider:
    def __init__(self, model: str):
        self.model = model
        self.last_usage: dict = {}

    def complete(self, *, system: str, user: str) -> str:
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        self.last_usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return _first_text_block(response)


class BedrockProvider:
    """Claude via AWS Bedrock. Credentials come from the standard AWS env
    vars / boto3 credential chain, never from this class. The Bedrock model
    id is account- and region-specific, so it is read from an env var rather
    than guessed: an unset or wrong id fails loudly at call time instead of
    silently hitting the wrong model.
    """

    def __init__(self, model: str):
        self.model = model
        self.last_usage: dict = {}

    def complete(self, *, system: str, user: str) -> str:
        import anthropic

        client = anthropic.AnthropicBedrock(aws_region=os.environ.get("AWS_REGION"))
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        self.last_usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return _first_text_block(response)


class OllamaProvider:
    def __init__(self, model: str = OLLAMA_MODEL):
        self.model = model
        self.last_usage: dict = {}

    def complete(self, *, system: str, user: str) -> str:
        import httpx

        response = httpx.post(
            "http://localhost:11434/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        # Best-effort: Ollama reports these as prompt_eval_count/eval_count, not the
        # input_tokens/output_tokens shape the Anthropic SDK uses. Local inference has
        # no per-token API charge either way, so estimate_cost_usd() ignores these.
        self.last_usage = {
            "input_tokens": body.get("prompt_eval_count", 0),
            "output_tokens": body.get("eval_count", 0),
        }
        return body["message"]["content"]


def get_provider(role: str) -> Provider:
    backend = os.environ.get("ASK_PROVIDER", "claude")
    if backend == "ollama":
        return OllamaProvider()
    if backend == "claude":
        model = INTENT_MODEL if role == "intent" else NARRATE_MODEL
        return ClaudeProvider(model)
    if backend == "bedrock":
        env_var = "ASK_BEDROCK_INTENT_MODEL" if role == "intent" else "ASK_BEDROCK_NARRATE_MODEL"
        model = os.environ.get(env_var)
        if not model:
            raise ValueError(f"ASK_PROVIDER=bedrock requires {env_var} to be set")
        return BedrockProvider(model)
    raise ValueError(f"unknown ASK_PROVIDER: {backend!r}")
