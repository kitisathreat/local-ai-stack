"""Backend client factory + re-exports.

Downstream code should construct clients via `client_for(host)` rather than
instantiating OllamaClient / LlamaCppClient / OpenAIClient directly so auth
and TLS settings flow uniformly from `HostConfig`.
"""

from __future__ import annotations

from typing import Union

from ..config import HostConfig
from .base import resolve_bearer_token
from .llama_cpp import LlamaCppClient
from .ollama import OllamaClient
from .openai import OpenAIClient


BackendClient = Union[OllamaClient, LlamaCppClient, OpenAIClient]


def client_for(host: HostConfig) -> BackendClient:
    """Build a backend client configured for the given host.

    Resolves `auth_env` → bearer token, plumbs TLS + timeout settings, and
    selects the client class by `host.kind`.
    """
    token = resolve_bearer_token(host.auth_env)
    common = dict(
        endpoint=host.url,
        timeout_sec=host.request_timeout_sec,
        auth_token=token,
        verify_tls=host.verify_tls,
        connect_timeout_sec=host.connect_timeout_sec,
    )
    if host.kind == "ollama":
        return OllamaClient(**common)
    if host.kind == "llama_cpp":
        return LlamaCppClient(**common)
    if host.kind == "openai":
        return OpenAIClient(**common)
    raise ValueError(f"Unknown host kind: {host.kind!r}")


__all__ = [
    "BackendClient",
    "LlamaCppClient",
    "OllamaClient",
    "OpenAIClient",
    "client_for",
    "resolve_bearer_token",
]
