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


class Provider(Protocol):
    def complete(self, *, system: str, user: str) -> str: ...


class ClaudeProvider:
    def __init__(self, model: str):
        self.model = model

    def complete(self, *, system: str, user: str) -> str:
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text


class BedrockProvider:
    """Claude via AWS Bedrock. Credentials come from the standard AWS env
    vars / boto3 credential chain, never from this class. The Bedrock model
    id is account- and region-specific, so it is read from an env var rather
    than guessed: an unset or wrong id fails loudly at call time instead of
    silently hitting the wrong model.
    """

    def __init__(self, model: str):
        self.model = model

    def complete(self, *, system: str, user: str) -> str:
        import anthropic

        client = anthropic.AnthropicBedrock(aws_region=os.environ.get("AWS_REGION"))
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text


class OllamaProvider:
    def __init__(self, model: str = OLLAMA_MODEL):
        self.model = model

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
        return response.json()["message"]["content"]


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
