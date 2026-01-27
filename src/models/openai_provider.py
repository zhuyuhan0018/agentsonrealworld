from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI

class OpenAIProvider:
    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs

    def create(self) -> ChatOpenAI:
        api_key = self.kwargs.pop("api_key", os.getenv("OPENAI_API_KEY"))
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        return ChatOpenAI(model=self.name, api_key=api_key, **self.kwargs)


