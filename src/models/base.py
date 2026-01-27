from __future__ import annotations

import os
from typing import Dict, Protocol

from langchain_core.language_models.chat_models import BaseChatModel


class ModelProvider(Protocol):
    def create(self) -> BaseChatModel:  # pragma: no cover - interface
        ...


class ProviderFactory:
    _registry: Dict[str, type]

    def __init__(self) -> None:
        self._registry = {}

    def register(self, vendor: str, provider_cls: type) -> None:
        self._registry[vendor] = provider_cls

    def create(self, vendor: str, name: str, kwargs: Dict) -> BaseChatModel:
        vendor = vendor.lower()
        if vendor not in self._registry:
            raise ValueError(f"Unknown vendor: {vendor}")
        provider = self._registry[vendor](name=name, **kwargs)
        if not hasattr(provider, "create"):
            raise ValueError("Provider missing create()")
        return provider.create()


factory = ProviderFactory()


