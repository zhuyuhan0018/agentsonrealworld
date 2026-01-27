from typing import Any
from langchain_openai import ChatOpenAI
import os


class GeminiProvider:
    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs

    def create(self) -> ChatOpenAI:
        api_key = self.kwargs.pop("api_key", os.getenv("GOOGLE_API_KEY"))

        # extra_body = {
        #     "extra_body": {
        #         "google": {
        #             "thinking_config": {
        #                 "thinking_level": "low",
        #                 "include_thoughts": True,
        #             }
        #         }
        #     }
        # }
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        return ChatOpenAI(
            model=self.name, api_key=api_key, 
            **self.kwargs
        )
