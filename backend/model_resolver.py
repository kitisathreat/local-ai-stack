"""Model version resolver.

Polls Hugging Face + the Ollama registry for each tier declared in
``config/model-sources.yaml``, picks which revision/tag to load this run,
and writes the result to ``data/resolved-models.json`` for the backend
to consume.

Design goals:
    * No hard dependency on ``huggingface_hub`` — if the import fails
      we fall back to the pinned spec.
    * Never raise to the caller. A failed poll downgrades to pinned;
      a failed pinned lookup produces a clear ``error`` field in the
      output JSON so the GUI can surface the problem.
    * Cache results for 24 h in ``data/model-cache.json`` so ``-Start``
      is cheap. ``-CheckUpdates`` forces a refresh (``force=True``).

CLI:
    python -m backend.model_resolver resolve
    python -m backend.model_resolver resolve --force
    python -m backend.model_resolver resolve --offline
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger("backend.model_resolver")

CACHE_TTL_SECONDS = 24 * 60 * 60


def _repo_root() -> Path:
    # backend/model_resolver.py → repo root
    return Path(__file__).resolve().parent.parent


def _config_dir() -> Path:
    env = os.getenv("LAI_CONFIG_DIR")
    if env:
        return Path(env)
    return _repo_root() / "config"


def _data_dir() -> Path:
    env = os.getenv("LAI_DATA_DIR")
    if env:
        p = Path(env)
    else:
        p = _repo_root() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Data model ─────────────────────────────────────────────────────────────

@dataclass
class Resolved:
    tier: str
    source: str                         # huggingface | ollama | static
    identifier: str                     # tag (ollama) or repo/file (hf)
    revision: str | None = None         # commit SHA (hf) or digest (ollama)
    origin: str = "pinned"              # latest | pinned | cache | static
    update_available: bool = False
    pending_version: str | None = None
    error: str | None = None
    resolved_at: float = field(default_factory=time.time)


@dataclass
class ResolveResult:
    resolved: dict[str, Resolved]
    cached: bool = False
    offline: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "resolved_at": time.time(),
            "cached": self.cached,
            "offline": self.offline,
            "tiers": {k: asdict(v) for k, v in self.resolved.items()},
        }


# ── Upstream probes ────────────────────────────────────────────────────────

def _probe_huggingface(repo: str, file_pattern: str | None) -> tuple[str | None, str | None]:
    """Returns (revision_sha, matching_file) or (None, None) on failure."""
    try:
        from huggingface_hub import HfApi
    except ImportError:
        logger.warning("huggingface_hub not installed — skipping HF poll for %s", repo)
        return None, None
    token = os.getenv("HF_TOKEN") or None
    try:
        api = HfApi(token=token)
        info = api.model_info(repo)
        sha = getattr(info, "sha", None)
        siblings = getattr(info, "siblings", []) or []
        chosen = None
        if file_pattern:
            import fnmatch
            for s in siblings:
                name = getattr(s, "rfilename", "") or ""
                if fnmatch.fnmatch(name, file_pattern):
                    chosen = name
                    break
        return sha, chosen
    except Exception as exc:
        logger.warning("HF poll failed for %s: %s", repo, exc)
        return None, None


def _probe_ollama_local(name: str) -> str | None:
    """Returns the local digest of `name` if it's pulled, else None."""
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    try:
        import httpx
        r = httpx.get(f"{ollama_url}/api/tags", timeout=4.0)
        r.raise_for_status()
        for model in (r.json() or {}).get("models", []):
            if model.get("name") == name or model.get("model") == name:
                return model.get("digest")
    except Exception as exc:
        logger.debug("Ollama local probe failed for %s: %s", name, exc)
    return None


def _probe_ollama_registry(name: str) -> str | None:
    """Queries the Ollama registry for the latest manifest digest.

    The `registry.ollama.ai` manifest endpoint isn't officially public,
    so we defensively handle failures. Format: name[:tag] → library/<name>/manifests/<tag>.
    `hf.co/...` tags are backed by Hugging Face and can't be polled via
    registry.ollama.ai; they're handled by the huggingface source instead.
    """
    if name.startswith("hf.co/"):
        return None
    if ":" in name:
        base, tag = name.rsplit(":", 1)
    else:
        base, tag = name, "latest"
    if "/" not in base:
        base = f"library/{base}"
    url = f"https://registry.ollama.ai/v2/{base}/manifests/{tag}"
    try:
        import httpx
        headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
        r = httpx.get(url, headers=headers, timeout=6.0)
        if r.status_code != 200:
            return None
        return r.headers.get("Docker-Content-Digest") or (r.json().get("config") or {}).get("digest")
    except Exception as exc:
        logger.debug("Ollama registry probe failed for %s: %s", name, exc)
        return None


# ── Resolution ─────────────────────────────────────────────────────────────

def _resolve_tier(tier: str, spec: dict, *, offline: bool) -> Resolved:
    source = (spec.get("source") or "").lower()
    tracking = (spec.get("tracking") or "latest").lower()
    pinned = spec.get("pinned") or {}

    if source == "huggingface":
        repo = spec.get("repo") or ""
        file_pattern = spec.get("file")
        pinned_file = pinned.get("file") or ""
        pinned_rev = pinned.get("revision") or "main"
        if offline or tracking == "pinned":
            return Resolved(
                tier=tier, source="huggingface",
                identifier=f"{repo}/{pinned_file}",
                revision=pinned_rev, origin="pinned",
            )
        sha, chosen = _probe_huggingface(repo, file_pattern)
        if not sha:
            return Resolved(
                tier=tier, source="huggingface",
                identifier=f"{repo}/{pinned_file}",
                revision=pinned_rev, origin="pinned",
                error="HF poll failed; using pinned",
            )
        return Resolved(
            tier=tier, source="huggingface",
            identifier=f"{repo}/{chosen or pinned_file}",
            revision=sha, origin="latest",
            update_available=bool(chosen and pinned_file and chosen != pinned_file) or sha != pinned_rev,
            pending_version=sha if sha != pinned_rev else None,
        )

    if source == "ollama":
        name = spec.get("name") or ""
        pinned_name = pinned.get("name") or name
        pinned_digest = pinned.get("digest")
        if offline or tracking == "pinned":
            return Resolved(
                tier=tier, source="ollama",
                identifier=pinned_name, revision=pinned_digest,
                origin="pinned",
            )
        remote_digest = _probe_ollama_registry(name)
        local_digest = _probe_ollama_local(name)
        if not remote_digest:
            return Resolved(
                tier=tier, source="ollama",
                identifier=name, revision=local_digest or pinned_digest,
                origin="pinned" if not local_digest else "cache",
                error="Ollama registry poll failed; using local/pinned",
            )
        update = bool(local_digest and remote_digest and local_digest != remote_digest)
        return Resolved(
            tier=tier, source="ollama",
            identifier=name, revision=remote_digest,
            origin="latest",
            update_available=update,
            pending_version=remote_digest if update else None,
        )

    return Resolved(
        tier=tier, source="static",
        identifier=(spec.get("name") or spec.get("repo") or ""),
        origin="static",
        error=f"unknown source {source!r}",
    )


def load_sources(path: Path | None = None) -> dict[str, dict]:
    src_path = path or (_config_dir() / "model-sources.yaml")
    if not src_path.exists():
        return {}
    data = yaml.safe_load(src_path.read_text(encoding="utf-8")) or {}
    return data.get("tiers") or {}


def resolve(*, force: bool = False, offline: bool | None = None) -> ResolveResult:
    if offline is None:
        offline = os.getenv("OFFLINE", "").strip() in {"1", "true", "yes"}
    sources = load_sources()
    cache_path = _data_dir() / "model-cache.json"

    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - cached.get("resolved_at", 0) < CACHE_TTL_SECONDS:
                tiers = {
                    k: Resolved(**{**v, "origin": "cache"})
                    for k, v in (cached.get("tiers") or {}).items()
                }
                return ResolveResult(resolved=tiers, cached=True, offline=offline)
        except Exception as exc:
            logger.debug("Model cache read failed: %s", exc)

    resolved: dict[str, Resolved] = {}
    for tier, spec in sources.items():
        resolved[tier] = _resolve_tier(tier, spec or {}, offline=offline)

    result = ResolveResult(resolved=resolved, cached=False, offline=offline)

    try:
        cache_path.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write model cache: %s", exc)

    resolved_path = _data_dir() / "resolved-models.json"
    try:
        resolved_path.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write resolved-models.json: %s", exc)

    return result


# ── Pull orchestration helpers (used by the launcher's -Start) ─────────────

def pull_missing_ollama_tags(result: ResolveResult) -> list[str]:
    """For every Ollama tier whose resolved tag isn't local, run
    `ollama pull <tag>`. Returns the list of tags that were pulled.
    """
    pulled: list[str] = []
    for tier, r in result.resolved.items():
        if r.source != "ollama" or r.error:
            continue
        if _probe_ollama_local(r.identifier):
            continue
        logger.info("Pulling missing Ollama tag for tier %s: %s", tier, r.identifier)
        try:
            subprocess.run(
                ["ollama", "pull", r.identifier],
                check=True,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            pulled.append(r.identifier)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            logger.warning("ollama pull %s failed: %s", r.identifier, exc)
    return pulled


# ── CLI ────────────────────────────────────────────────────────────────────

def _main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    parser = argparse.ArgumentParser(prog="backend.model_resolver")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", help="resolve tiers, write data/resolved-models.json")
    r.add_argument("--force", action="store_true", help="ignore cache, re-poll")
    r.add_argument("--offline", action="store_true", help="skip polling, use pinned")
    r.add_argument("--pull", action="store_true", help="pull missing Ollama tags after resolution")
    args = parser.parse_args()

    if args.cmd == "resolve":
        result = resolve(force=args.force, offline=args.offline)
        for tier, info in result.resolved.items():
            flag = " (update!)" if info.update_available else ""
            err = f"  ERROR: {info.error}" if info.error else ""
            print(f"  {tier:20s} {info.source:12s} {info.identifier}{flag}{err}")
        if args.pull:
            pulled = pull_missing_ollama_tags(result)
            if pulled:
                print(f"Pulled: {', '.join(pulled)}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
