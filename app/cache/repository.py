"""Single abstraction for persisted cache sections (JSON-backed default).

Call sites use :class:`JsonCacheRepository`; a SQL backend can swap in later
without changing provider modules.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from app import model_cache_store


class CacheRepository(Protocol):
    def read_section(self, section: str) -> dict[str, Any]:
        ...

    def update_section(
        self, section: str, updater: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        ...

    def load_root(self) -> dict[str, Any]:
        ...

    def save_root(self, payload: dict[str, Any]) -> None:
        ...


class JsonCacheRepository:
    """Delegates to :mod:`app.model_cache_store` (``.cache/model_cache.json``)."""

    def read_section(self, section: str) -> dict[str, Any]:
        return model_cache_store.read_section(section)

    def update_section(
        self, section: str, updater: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        return model_cache_store.update_section(section, updater)

    def load_root(self) -> dict[str, Any]:
        return model_cache_store.load_model_cache()

    def save_root(self, payload: dict[str, Any]) -> None:
        model_cache_store.save_model_cache(payload)


def default_json_repository() -> JsonCacheRepository:
    return JsonCacheRepository()
