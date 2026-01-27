from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI

class QwenProvider:
    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs

    def create(self) -> ChatOpenAI:
        api_key = self.kwargs.pop("api_key", os.getenv("DASHSCOPE_API_KEY"))
        base_url = self.kwargs.pop("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        # Disable Qwen "thinking" mode (thought traces) for workflow control.
        extra_body = self.kwargs.pop("extra_body", {}) or {}
        extra_body.setdefault("enable_thoughts", False)
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY not set")
        return ChatOpenAI(
            model=self.name,
            api_key=api_key,
            base_url=base_url,
            extra_body=extra_body,
            **self.kwargs,
        )

