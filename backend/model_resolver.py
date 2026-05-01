"""Model version resolver — Hugging Face only.

Polls Hugging Face for each tier declared in
``config/model-sources.yaml``, picks which GGUF revision/file to load this
run, and writes the result to ``data/resolved-models.json`` for the backend
to consume.

Design goals:
    * No hard dependency on ``huggingface_hub`` — if the import fails
      we fall back to the pinned spec.
    * Never raise to the caller. A failed poll downgrades to pinned;
      a failed pinned lookup produces a clear ``error`` field in the
      output JSON so the GUI can surface the problem.
    * Cache results for 24 h in ``data/model-cache.json`` so ``-Start``
      is cheap. ``-CheckUpdates`` forces a refresh (``force=True``).
    * Downloaded GGUFs land at a deterministic path: ``data/models/<tier>.gguf``
      (and ``<tier>.mmproj.gguf`` for vision). The backend predicts these
      paths from config, so models.yaml never needs exact filenames.

CLI:
    python -m backend.model_resolver resolve
    python -m backend.model_resolver resolve --force
    python -m backend.model_resolver resolve --offline
    python -m backend.model_resolver resolve --pull
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger("backend.model_resolver")

CACHE_TTL_SECONDS = 24 * 60 * 60


def _repo_root() -> Path:
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


def _models_dir() -> Path:
    d = _data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Data model ─────────────────────────────────────────────────────────────

@dataclass
class Resolved:
    tier: str
    source: str = "huggingface"         # always "huggingface" now
    repo: str = ""
    filename: str = ""                  # GGUF file in the repo
    mmproj_filename: str | None = None
    revision: str | None = None         # commit SHA
    origin: str = "pinned"              # latest | pinned | cache
    update_available: bool = False
    pending_version: str | None = None
    error: str | None = None
    gguf_path: str | None = None        # absolute on-disk path after pull
    mmproj_path: str | None = None
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

def _probe_huggingface(
    repo: str, file_pattern: str | None, mmproj_pattern: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Returns (revision_sha, gguf_filename, mmproj_filename) or (None, ...) on failure."""
    try:
        from huggingface_hub import HfApi
    except ImportError:
        logger.warning("huggingface_hub not installed — skipping HF poll for %s", repo)
        return None, None, None
    token = os.getenv("HF_TOKEN") or None
    try:
        api = HfApi(token=token)
        info = api.model_info(repo)
        sha = getattr(info, "sha", None)
        siblings = getattr(info, "siblings", []) or []
        chosen_gguf = _match_first(siblings, file_pattern) if file_pattern else None
        chosen_mmproj = _match_first(siblings, mmproj_pattern) if mmproj_pattern else None
        return sha, chosen_gguf, chosen_mmproj
    except Exception as exc:
        logger.warning("HF poll failed for %s: %s", repo, exc)
        return None, None, None


def _match_first(siblings, pattern: str) -> str | None:
    import fnmatch
    for s in siblings:
        name = getattr(s, "rfilename", "") or ""
        if fnmatch.fnmatch(name, pattern):
            return name
    return None


# ── Resolution ─────────────────────────────────────────────────────────────

def _resolve_tier(tier: str, spec: dict, *, offline: bool) -> Resolved:
    source = (spec.get("source") or "huggingface").lower()
    if source != "huggingface":
        return Resolved(
            tier=tier, source=source, error=f"unsupported source {source!r}",
        )

    tracking = (spec.get("tracking") or "latest").lower()
    pinned = spec.get("pinned") or {}
    repo = spec.get("repo") or ""
    file_pattern = spec.get("file")
    mmproj_pattern = spec.get("mmproj")
    pinned_file = pinned.get("file") or ""
    pinned_mmproj = pinned.get("mmproj")
    pinned_rev = pinned.get("revision") or "main"

    if offline or tracking == "pinned":
        return Resolved(
            tier=tier,
            repo=repo,
            filename=pinned_file,
            mmproj_filename=pinned_mmproj,
            revision=pinned_rev,
            origin="pinned",
        )

    sha, chosen, chosen_mmproj = _probe_huggingface(repo, file_pattern, mmproj_pattern)
    if not sha:
        return Resolved(
            tier=tier,
            repo=repo,
            filename=pinned_file,
            mmproj_filename=pinned_mmproj,
            revision=pinned_rev,
            origin="pinned",
            error="HF poll failed; using pinned",
        )
    return Resolved(
        tier=tier,
        repo=repo,
        filename=chosen or pinned_file,
        mmproj_filename=chosen_mmproj or pinned_mmproj,
        revision=sha,
        origin="latest",
        update_available=(
            (bool(chosen) and bool(pinned_file) and chosen != pinned_file)
            or sha != pinned_rev
        ),
        pending_version=sha if sha != pinned_rev else None,
    )


def load_sources(path: Path | None = None) -> dict[str, dict]:
    src_path = path or (_config_dir() / "model-sources.yaml")
    if not src_path.exists():
        return {}
    data = yaml.safe_load(src_path.read_text(encoding="utf-8")) or {}
    return data.get("tiers") or {}


def resolve(
    *,
    force: bool = False,
    offline: bool | None = None,
    tiers: list[str] | None = None,
) -> ResolveResult:
    """Resolve model versions for all tiers (or just *tiers* when provided).

    `tiers` is an optional allow-list of tier names from
    `config/model-sources.yaml`. When set, resolution and pull are
    restricted to those tiers — the wizard's "Select which models to
    download" checkboxes use this.
    """
    if offline is None:
        offline = os.getenv("OFFLINE", "").strip() in {"1", "true", "yes"}
    sources = load_sources()

    # Build the tier allow-list once; both fresh-resolve and cache-hit
    # branches need it. (Earlier the cache branch ignored the filter
    # AND the local `tiers = {…}` rebind shadowed the parameter.)
    wanted: set[str] | None = None
    if tiers:
        # Tolerate common alias the wizard sends ('embed' → 'embedding')
        # and case variations. Unknown names are silently dropped.
        wanted = {t.strip().lower() for t in tiers if t}
        if "embed" in wanted:
            wanted.discard("embed")
            wanted.add("embedding")
        sources = {k: v for k, v in sources.items() if k.lower() in wanted}

    cache_path = _data_dir() / "model-cache.json"

    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - cached.get("resolved_at", 0) < CACHE_TTL_SECONDS:
                cached_tiers = cached.get("tiers") or {}
                if wanted is not None:
                    cached_tiers = {
                        k: v for k, v in cached_tiers.items()
                        if k.lower() in wanted
                    }
                resolved_from_cache = {
                    k: Resolved(**{**v, "origin": "cache"})
                    for k, v in cached_tiers.items()
                }
                return ResolveResult(
                    resolved=resolved_from_cache, cached=True, offline=offline,
                )
        except Exception as exc:
            logger.debug("Model cache read failed: %s", exc)

    resolved: dict[str, Resolved] = {}
    for tier, spec in sources.items():
        resolved[tier] = _resolve_tier(tier, spec or {}, offline=offline)

    # Predict the on-disk path for each tier even before pull.
    for tier_name, r in resolved.items():
        r.gguf_path = str(_models_dir() / f"{tier_name}.gguf")
        if r.mmproj_filename:
            r.mmproj_path = str(_models_dir() / f"{tier_name}.mmproj.gguf")

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


# ── Pull orchestration ─────────────────────────────────────────────────────

def _link_or_copy(src: Path, target: Path) -> Path:
    """Create `target` pointing at `src`. Symlink first, file copy on failure
    (Windows accounts without Developer Mode can't create symlinks)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        try:
            if target.resolve() == src.resolve():
                return target
        except OSError:
            pass
        try:
            target.unlink()
        except OSError as exc:
            logger.warning("Could not remove stale %s: %s", target, exc)
            return src
    try:
        target.symlink_to(src)
        return target
    except (OSError, NotImplementedError):
        import shutil
        shutil.copy2(src, target)
        return target


def pull_missing_hf_files(
    result: ResolveResult,
    *,
    dry_run: bool = False,
) -> list[str]:
    """For every Hugging-Face-sourced tier whose resolved file isn't on
    disk at data/models/<tier>.gguf, download via ``hf_hub_download`` and
    rename/symlink to the canonical path.

    Returns the list of tier names that were pulled (or would be pulled if
    ``dry_run=True``).
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        logger.warning("huggingface_hub not installed — skipping HF file pulls")
        return []

    pulled: list[str] = []
    target_root = _models_dir()
    token = os.getenv("HF_TOKEN") or None

    def _download_with_retry(**kwargs) -> Path:
        """hf_hub_download wrapper with hf_transfer fallback + N retries.

        Two failure modes we hit on multi-GB GGUFs:
          1. hf_transfer (Rust parallel downloader) bombs on any
             transient error — no built-in retry. We give it one shot
             then disable it process-wide.
          2. The pure-Python downloader gets `IncompleteRead` when the
             CDN connection drops mid-stream. It does NOT auto-resume
             across calls in our setup — but a fresh call resumes from
             the partial blob in ~/.cache/huggingface, so we just retry.

        Retry up to 8 times per file with exponential-ish backoff
        (capped at 30 s). Re-raises only after exhausting attempts.
        """
        max_attempts = 8
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return Path(hf_hub_download(**kwargs))
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "hf_transfer" in msg or "HF_HUB_ENABLE_HF_TRANSFER" in msg:
                    logger.warning(
                        "hf_transfer failed (attempt %d) — disabling and retrying",
                        attempt,
                    )
                    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
                    continue   # retry immediately with the pure-Python path
                if attempt >= max_attempts:
                    break
                # Backoff for transient network errors. The next call
                # will pick up the partial blob from ~/.cache/huggingface
                # so we don't lose what already streamed in.
                sleep_s = min(30, 2 ** attempt)
                logger.warning(
                    "hf_hub_download attempt %d/%d failed (%s) — retrying in %ds",
                    attempt, max_attempts,
                    msg.splitlines()[0][:140] if msg else type(exc).__name__,
                    sleep_s,
                )
                time.sleep(sleep_s)
        assert last_exc is not None
        raise last_exc

    for tier, r in result.resolved.items():
        if r.source != "huggingface" or not r.repo or not r.filename:
            continue

        target_gguf = target_root / f"{tier}.gguf"
        target_mmproj = (
            target_root / f"{tier}.mmproj.gguf" if r.mmproj_filename else None
        )

        need_gguf = not target_gguf.exists()
        need_mmproj = bool(target_mmproj) and not target_mmproj.exists()
        if not (need_gguf or need_mmproj):
            continue

        if dry_run:
            logger.info(
                "would-pull HF for tier %s: %s@%s %s -> %s%s",
                tier, r.repo, r.revision or "main", r.filename, target_gguf,
                f" (+ mmproj {r.mmproj_filename})" if need_mmproj else "",
            )
            pulled.append(tier)
            continue

        revision = r.revision or "main"
        try:
            if need_gguf:
                downloaded = _download_with_retry(
                    repo_id=r.repo,
                    filename=r.filename,
                    revision=revision,
                    local_dir=str(target_root),
                    token=token,
                )
                _link_or_copy(downloaded, target_gguf)
                r.gguf_path = str(target_gguf)
            if need_mmproj:
                downloaded = Path(hf_hub_download(
                    repo_id=r.repo,
                    filename=r.mmproj_filename,
                    revision=revision,
                    local_dir=str(target_root),
                    token=token,
                ))
                _link_or_copy(downloaded, target_mmproj)
                r.mmproj_path = str(target_mmproj)
        except Exception as exc:
            logger.warning("hf_hub_download failed for tier %s: %s", tier, exc)
            continue
        pulled.append(tier)

    # Refresh resolved-models.json with the on-disk paths.
    try:
        manifest = _data_dir() / "resolved-models.json"
        manifest.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not update resolved-models.json: %s", exc)

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
    # --pull-hf is the legacy launcher name; --pull is the canonical form.
    r.add_argument(
        "--pull", "--pull-hf", action="store_true",
        dest="pull",
        help="pull missing GGUFs after resolution",
    )
    r.add_argument(
        "--dry-run", action="store_true",
        help="with --pull, enumerate would-be-pulled tiers without downloading. "
             "Exits non-zero if any tier resolved with an error.",
    )
    r.add_argument(
        "--tier", action="append", dest="tiers", default=None, metavar="NAME",
        help="Restrict resolve+pull to specific tier(s). Repeatable. "
             "If unspecified, all tiers in model-sources.yaml are processed. "
             "Aliases: 'embed' → 'embedding'.",
    )
    args = parser.parse_args()

    if args.cmd == "resolve":
        result = resolve(force=args.force, offline=args.offline, tiers=args.tiers)
        for tier, info in result.resolved.items():
            flag = " (update!)" if info.update_available else ""
            err = f"  ERROR: {info.error}" if info.error else ""
            print(f"  {tier:20s} {info.source:12s} {info.repo}/{info.filename}{flag}{err}")
        if args.pull:
            pulled = pull_missing_hf_files(result, dry_run=args.dry_run)
            if pulled:
                prefix = "Would-pull" if args.dry_run else "Pulled"
                print(f"{prefix}: {', '.join(pulled)}")
        if args.dry_run:
            errored = [t for t, r in result.resolved.items() if r.error]
            if errored:
                print(f"Tier errors in dry-run: {', '.join(errored)}")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
