from __future__ import annotations

import os
from typing import Any

from langchain_anthropic import ChatAnthropic


class AnthropicProvider:
    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs

    def create(self) -> ChatAnthropic:
        api_key = self.kwargs.pop("api_key", os.getenv("ANTHROPIC_API_KEY"))
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        return ChatAnthropic(model=self.name, api_key=api_key, **self.kwargs)


