"""Persistent JSON state store for Vercel (Private Vercel Blob or local files for dev)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Optional, Protocol


class StateStore(Protocol):
    """Durable key-value JSON store (used for rotating Tesla OAuth tokens)."""

    @property
    def backend_name(self) -> str: ...

    def get_json(self, key: str) -> Optional[Dict[str, Any]]: ...

    def set_json(self, key: str, value: Dict[str, Any]) -> None: ...


class VercelBlobStateStore:
    """Stores JSON documents in a private Vercel Blob store via `vercel.blob.sync`."""

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

    def _blob(self) -> Any:
        if self._blob_module is None:
            from vercel.blob import sync as blob_sync

            self._blob_module = blob_sync
        return self._blob_module

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            result = self._blob().get_blob(self._pathname(key), access='private')
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
        return json.loads(body.decode('utf-8') if isinstance(body, bytes) else str(body))

    def set_json(self, key: str, value: Dict[str, Any]) -> None:
        body = json.dumps(value, indent=2).encode('utf-8')
        self._blob().put_blob(
            self._pathname(key),
            body,
            access='private',
            allow_overwrite=True,
            add_random_suffix=False,
            content_type='application/json',
        )


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
        return json.loads(path.read_text(encoding='utf-8'))

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
