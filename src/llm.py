"""Model access with a hard call budget, plus tolerant JSON extraction."""
from __future__ import annotations

import json
import os
import re


class BudgetExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, api_key: str | None = None, max_calls: int | None = None, client=None):
        self.max_calls = int(os.environ.get("MAX_LLM_CALLS", "0")) or max_calls
        self.calls = 0
        self._client = client
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def ask(self, system: str, user: str, model: str, max_tokens: int = 4000) -> str:
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExceeded(f"LLM call budget of {self.max_calls} reached")
        self.calls += 1
        msg = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def extract_json(text: str):
    """Parse the first JSON object in a model reply; None if there isn't a valid one."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None
