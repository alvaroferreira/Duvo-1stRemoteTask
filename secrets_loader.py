"""
secrets_loader.py
-----------------
Loads the per-store StoreLink API keys (the ``X-Korral-Store-Key`` header) and hands
them to the StoreLink client.

Keys are NEVER baked into the image. They are loaded at runtime from a mounted JSON
file (the default, good for the demo) or, in production, from GCP Secret Manager behind
the *same* ``KeyProvider`` interface.

Design decisions
================
* **TTL cache (default 300s).** Korral rotates store keys weekly. The deploy pipeline
  updates the mounted secret; this server picks up the new value on the next cache miss
  (or immediately when ``reload()`` is called). No redeploy is needed for rotation.
* **The raw key never leaves memory except in the upstream HTTP header.** Anything that
  is logged uses :func:`key_fingerprint` (first 8 chars of a sha256), never the key.
* **Fail fast on a missing credential.** :meth:`KeyProvider.get_key` raises
  :class:`MissingStoreCredentialError` so a tool can refuse the request *before* spending
  an upstream round-trip that could never be authenticated.

The exception base class lives here (not in ``storelink_client``) so this module has no
dependency on the client and the import graph stays acyclic; StoreLink-specific errors
extend :class:`KorralError` over in ``storelink_client.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional, Union


# --------------------------------------------------------------------------- #
# Error hierarchy (foundational; extended in storelink_client.py)
# --------------------------------------------------------------------------- #
class KorralError(Exception):
    """Base class for every typed error this server raises internally.

    Tools translate these into clean MCP ``ToolError`` messages; tracebacks never
    reach the agent.
    """


class MissingStoreCredentialError(KorralError):
    """Raised when a tool is invoked for a store that has no configured key.

    We fail fast on this *before* making any StoreLink call so we never burn an
    upstream round-trip on a request that cannot be authenticated. The message tells
    the operator exactly how to fix it.
    """

    def __init__(self, store_id: Union[int, str], source_hint: str) -> None:
        self.store_id = store_id
        self.source_hint = source_hint
        super().__init__(
            f'No StoreLink key configured for store {store_id}. '
            f'Add an entry for "{store_id}" to {source_hint}; '
            f"the server will pick it up within the key-cache TTL window."
        )


def key_fingerprint(raw_key: str) -> str:
    """Return the first 8 hex chars of ``sha256(key)``.

    Safe to log. The raw key is never logged.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:8]


@dataclass
class KeyRecord:
    """A resolved store key plus the metadata we are allowed to log."""

    store_id: str
    key: str  # raw secret -- NEVER log this field directly
    rotated_at: Optional[str]  # ISO-8601 string from the secret payload, may be None

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.key)

    def __repr__(self) -> str:  # defensive: keep the raw key out of any accidental log
        return (
            f"KeyRecord(store_id={self.store_id!r}, "
            f"fingerprint={self.fingerprint!r}, rotated_at={self.rotated_at!r})"
        )


# --------------------------------------------------------------------------- #
# Provider interface + implementations
# --------------------------------------------------------------------------- #
class KeyProvider(ABC):
    """Interface for resolving a store key. File and Secret Manager share this."""

    @abstractmethod
    def get_key(self, store_id: Union[int, str]) -> KeyRecord:
        """Return the :class:`KeyRecord` for ``store_id`` or raise
        :class:`MissingStoreCredentialError`."""

    @abstractmethod
    def reload(self) -> None:
        """Force a refresh from the backing store, ignoring the TTL."""

    @abstractmethod
    def source_hint(self) -> str:
        """Human-readable description of where keys come from (used in error text)."""


class FileKeyProvider(KeyProvider):
    """Loads ``store_id -> key`` from a mounted JSON file, with a TTL cache.

    Accepted JSON shapes::

        {"rotated_at": "2026-06-23T00:00:00Z", "keys": {"47": "sk_...", "102": "sk_..."}}

    or the plain map::

        {"47": "sk_...", "102": "sk_..."}

    A value may also be an object ``{"key": "sk_...", "rotated_at": "..."}`` to carry a
    per-store rotation timestamp.
    """

    def __init__(self, path: str, ttl_seconds: int = 300) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._keys: Dict[str, KeyRecord] = {}
        self._loaded_monotonic: Optional[float] = None

    def source_hint(self) -> str:
        return f"the keys file {self._path}"

    def _expired(self) -> bool:
        if self._loaded_monotonic is None:
            return True
        return (time.monotonic() - self._loaded_monotonic) >= self._ttl

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError as exc:
            raise KorralError(
                f"Keys file not found at {self._path}. Mount the secret there or set "
                f"KORRAL_KEYS_FILE to its path."
            ) from exc
        except json.JSONDecodeError as exc:
            raise KorralError(f"Keys file {self._path} is not valid JSON: {exc}") from exc

        default_rotated_at: Optional[str] = None
        entries = raw
        if isinstance(raw, dict) and isinstance(raw.get("keys"), dict):
            default_rotated_at = raw.get("rotated_at")
            entries = raw["keys"]

        records: Dict[str, KeyRecord] = {}
        for sid, value in entries.items():
            if isinstance(value, dict):
                records[str(sid)] = KeyRecord(
                    str(sid), value["key"], value.get("rotated_at", default_rotated_at)
                )
            else:
                records[str(sid)] = KeyRecord(str(sid), str(value), default_rotated_at)

        self._keys = records
        self._loaded_monotonic = time.monotonic()

    def reload(self) -> None:
        with self._lock:
            self._load()

    def get_key(self, store_id: Union[int, str]) -> KeyRecord:
        sid = str(store_id)
        with self._lock:
            if self._expired():
                self._load()
            record = self._keys.get(sid)
        if record is None:
            raise MissingStoreCredentialError(store_id, self.source_hint())
        return record


class SecretManagerKeyProvider(KeyProvider):
    """Production backend: same interface, GCP Secret Manager source.

    This is a documented stub for the demo (the file backend is the default). Wiring the
    real backend is a drop-in: implement ``_load`` with ``google-cloud-secret-manager``::

        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{self._project_id}/secrets/{self._secret_id}/versions/latest"
        payload = client.access_secret_version(name=name).payload.data.decode("utf-8")
        # ...parse the same JSON shape FileKeyProvider accepts...

    The TTL cache + ``reload()`` semantics are identical, so weekly rotation is still
    picked up without a redeploy.
    """

    def __init__(self, project_id: str, secret_id: str, ttl_seconds: int = 300) -> None:
        self._project_id = project_id
        self._secret_id = secret_id
        self._ttl = ttl_seconds

    def source_hint(self) -> str:
        return (
            f"GCP Secret Manager secret '{self._secret_id}' "
            f"in project '{self._project_id}'"
        )

    def reload(self) -> None:
        raise NotImplementedError(
            "Wire google-cloud-secret-manager here; see the class docstring."
        )

    def get_key(self, store_id: Union[int, str]) -> KeyRecord:
        raise NotImplementedError(
            "Wire google-cloud-secret-manager here; see the class docstring."
        )


def build_key_provider() -> KeyProvider:
    """Construct the configured key provider from the environment.

    * ``KORRAL_SECRETS_BACKEND``  -> ``file`` (default) or ``gcp``
    * ``KORRAL_KEYS_FILE``        -> path for the file backend (default ``./secrets/keys.json``)
    * ``KORRAL_KEY_TTL_SECONDS``  -> cache TTL (default ``300``)
    * ``KORRAL_GCP_PROJECT`` / ``KORRAL_SECRET_ID`` -> for the gcp backend
    """
    backend = os.environ.get("KORRAL_SECRETS_BACKEND", "file").lower()
    ttl = int(os.environ.get("KORRAL_KEY_TTL_SECONDS", "300"))
    if backend == "gcp":
        return SecretManagerKeyProvider(
            project_id=os.environ["KORRAL_GCP_PROJECT"],
            secret_id=os.environ.get("KORRAL_SECRET_ID", "storelink-store-keys"),
            ttl_seconds=ttl,
        )
    path = os.environ.get("KORRAL_KEYS_FILE", "./secrets/keys.json")
    return FileKeyProvider(path=path, ttl_seconds=ttl)
