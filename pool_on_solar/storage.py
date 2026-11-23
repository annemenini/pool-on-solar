"""Persistent JSON state store for Vercel (Private Vercel Blob or local files for dev)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Optional, Protocol


class StateStore(Protocol):
    """Key-value JSON store for rotating OAuth tokens and automation schedule state."""

    @property
    def backend_name(self) -> str: ...

    def get_json(self, key: str) -> Optional[Dict[str, Any]]: ...

    def set_json(self, key: str, value: Dict[str, Any]) -> None: ...


class VercelBlobStateStore:
    """Stores JSON documents in a private Vercel Blob store via vercel-blob."""

    def __init__(
        self,
        prefix: str = 'pool-on-solar/',
        blob_module: Optional[Any] = None,
    ) -> None:
        self._prefix = prefix.strip('/') + '/'
        self._blob_module = blob_module

    @property
    def backend_name(self) -> str:
        return 'vercel-blob'

    def _pathname(self, key: str) -> str:
        return f'{self._prefix}{key.lstrip("/")}'

    def _get_blob_module(self) -> Any:
        if self._blob_module is None:
            try:
                from vercel.blob import sync as vercel_blob
            except ImportError:
                from vercel import blob as vercel_blob

            self._blob_module = vercel_blob
        return self._blob_module

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        blob = self._get_blob_module()
        pathname = self._pathname(key)
        getter = getattr(blob, 'get_blob', None) or getattr(blob, 'get', None)
        if getter is None:
            raise RuntimeError('Installed vercel-blob SDK does not expose get_blob/get.')

        try:
            result = getter(pathname, access='private')
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)
        except Exception as err:
            err_text = str(err).lower()
            if 'not found' in err_text or '404' in err_text or 'blobnotfound' in err_text:
                return None
            raise

        if result is None:
            return None
        body = getattr(result, 'body', None)
        if body is None and hasattr(result, 'read'):
            body = result.read()
        if body is None:
            return None
        if isinstance(body, bytes):
            return json.loads(body.decode('utf-8'))
        return json.loads(str(body))

    def set_json(self, key: str, value: Dict[str, Any]) -> None:
        blob = self._get_blob_module()
        pathname = self._pathname(key)
        putter = getattr(blob, 'put_blob', None) or getattr(blob, 'put', None)
        if putter is None:
            raise RuntimeError('Installed vercel-blob SDK does not expose put_blob/put.')

        body = json.dumps(value, indent=2).encode('utf-8')
        result = putter(
            pathname,
            body,
            access='private',
            allow_overwrite=True,
            add_random_suffix=False,
            content_type='application/json',
        )
        if asyncio.iscoroutine(result):
            asyncio.run(result)


class LocalFileStateStore:
    """Persists JSON documents on the local filesystem (for local dev and unit tests)."""

    def __init__(self, base_dir: str) -> None:
        self._base = Path(base_dir)

    @property
    def backend_name(self) -> str:
        return 'local-file'

    def _path(self, key: str) -> Path:
        return self._base / key.lstrip('/')

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._path(key)
        if not path.is_file():
            return None
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)

    def set_json(self, key: str, value: Dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix='.state_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(value, f, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


def create_state_store() -> StateStore:
    """Selects Private Vercel Blob on Vercel, or local filesystem for local development."""
    if os.getenv('BLOB_READ_WRITE_TOKEN') or os.getenv('BLOB_STORE_ID'):
        return VercelBlobStateStore()

    if os.getenv('VERCEL'):
        raise RuntimeError(
            'Private Vercel Blob store not configured. Create and connect one with: '
            'vercel blob create-store pool-on-solar --access private'
        )

    return LocalFileStateStore(os.getenv('LOCAL_STATE_DIR', '.local_state'))
