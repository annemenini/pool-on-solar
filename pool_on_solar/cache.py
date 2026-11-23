"""Regional ephemeral cache backed by Vercel Runtime Cache (Data Cache) with local fallback."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, Union


class AppCache(Protocol):
    """Key-value ephemeral cache with TTL and tag-based invalidation."""

    @property
    def backend_name(self) -> str: ...

    def get(self, key: str) -> Any: ...

    def set(
        self,
        key: str,
        value: Any,
        ttl: int,
        tags: Optional[Sequence[str]] = None,
        name: Optional[str] = None,
    ) -> None: ...

    def delete(self, key: str) -> None: ...

    def expire_tag(self, tags: Union[str, Sequence[str]]) -> None: ...


class MemoryCache:
    """Thread-safe in-memory TTL + tag cache for local dev, tests, and fail-open fallback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (expires_at, value, frozenset(tags))
        self._entries: Dict[str, Tuple[float, Any, frozenset[str]]] = {}

    @property
    def backend_name(self) -> str:
        return 'memory'

    def get(self, key: str) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value, _ = entry
            if time.time() >= expires_at:
                self._entries.pop(key, None)
                return None
            return value

    def set(
        self,
        key: str,
        value: Any,
        ttl: int,
        tags: Optional[Sequence[str]] = None,
        name: Optional[str] = None,
    ) -> None:
        del name
        if ttl <= 0:
            self.delete(key)
            return
        with self._lock:
            self._entries[key] = (
                time.time() + ttl,
                value,
                frozenset(tags or ()),
            )

    def delete(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def expire_tag(self, tags: Union[str, Sequence[str]]) -> None:
        tag_set = {tags} if isinstance(tags, str) else set(tags)
        if not tag_set:
            return
        with self._lock:
            to_delete = [
                key
                for key, (_, _, entry_tags) in self._entries.items()
                if entry_tags & tag_set
            ]
            for key in to_delete:
                self._entries.pop(key, None)


class VercelRuntimeCache:
    """Wrapper around official `vercel.cache.RuntimeCache` with fail-open local fallback."""

    def __init__(
        self,
        namespace: str = 'pool-on-solar',
        runtime_cache: Optional[Any] = None,
        fallback: Optional[MemoryCache] = None,
    ) -> None:
        self._namespace = namespace
        self._runtime_cache = runtime_cache
        self._fallback = fallback or MemoryCache()

    @property
    def backend_name(self) -> str:
        return 'vercel-runtime-cache'

    def _client(self) -> Any:
        if self._runtime_cache is None:
            from vercel.cache import RuntimeCache

            self._runtime_cache = RuntimeCache(namespace=self._namespace)
        return self._runtime_cache

    def get(self, key: str) -> Any:
        try:
            return self._client().get(key)
        except Exception as err:
            print(f'Vercel Runtime Cache get({key}) fell back to memory: {err}')
            return self._fallback.get(key)

    def set(
        self,
        key: str,
        value: Any,
        ttl: int,
        tags: Optional[Sequence[str]] = None,
        name: Optional[str] = None,
    ) -> None:
        if ttl <= 0:
            self.delete(key)
            return
        options: Dict[str, Any] = {'ttl': int(ttl)}
        if tags:
            options['tags'] = list(tags)
        if name:
            options['name'] = name
        try:
            self._client().set(key, value, options)
        except Exception as err:
            print(f'Vercel Runtime Cache set({key}) fell back to memory: {err}')
            self._fallback.set(key, value, ttl=ttl, tags=tags, name=name)

    def delete(self, key: str) -> None:
        try:
            self._client().delete(key)
        except Exception as err:
            print(f'Vercel Runtime Cache delete({key}) failed: {err}')
        self._fallback.delete(key)

    def expire_tag(self, tags: Union[str, Sequence[str]]) -> None:
        normalized: Union[str, List[str]] = (
            tags if isinstance(tags, str) else list(tags)
        )
        try:
            self._client().expire_tag(normalized)
        except Exception as err:
            print(f'Vercel Runtime Cache expire_tag({normalized}) failed: {err}')
        self._fallback.expire_tag(normalized)


def create_app_cache(namespace: str = 'pool-on-solar') -> AppCache:
    """Uses Vercel Runtime Cache on Vercel, or in-memory TTL cache locally."""
    if os.getenv('VERCEL') or os.getenv('VERCEL_ENV'):
        return VercelRuntimeCache(namespace=namespace)
    return MemoryCache()
