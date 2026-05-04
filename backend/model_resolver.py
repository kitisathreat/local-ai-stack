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
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger("backend.model_resolver")

CACHE_TTL_SECONDS = 24 * 60 * 60

# hf_transfer (the Rust HF accelerator) deadlocks silently when running
# parallel-shard pulls on Windows: connections stay ESTABLISHED, no
# exception, but zero bytes flow to disk for tens of minutes. The
# exception-based fallback in `_download_with_retry` below can't catch
# this because nothing ever raises. Default it OFF process-wide so the
# pure-Python downloader is used; opt back in by exporting
# `HF_HUB_ENABLE_HF_TRANSFER=1` before launching. Throughput cost is
# small in our setup because we already parallelize at the shard level
# (`LAI_PARALLEL_SHARDS=4`), so most of the wall-clock win is preserved.
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")

# How many shards of a sharded GGUF download in parallel. HF allows
# multiple concurrent connections per IP; 4 is well below their soft
# rate-limit budget and gets us most of the wall-clock win over
# sequential pulls. Override at runtime via LAI_PARALLEL_SHARDS.
# Set to 1 to force purely sequential downloads (the legacy behaviour).
try:
    _PARALLEL_SHARD_WORKERS = max(1, int(os.getenv("LAI_PARALLEL_SHARDS", "4")))
except ValueError:
    _PARALLEL_SHARD_WORKERS = 4


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
        # Skip tiers explicitly marked `disabled: true` in
        # model-sources.yaml. Useful for tiers we know we can't pull yet
        # (e.g. sharded GGUFs we don't support stitching for) so we
        # don't burn 500 retries 404'ing the wrong filename. Surfaced
        # as a clear `error` in the resolved record so the operator
        # sees why the tier is unavailable.
        if (spec or {}).get("disabled"):
            reason = (spec or {}).get("disabled_reason") or "marked disabled"
            resolved[tier] = Resolved(
                source=spec.get("source", "huggingface"),
                origin="disabled",
                repo=spec.get("repo", ""),
                error=f"DISABLED: {reason}",
            )
            continue
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

    # MERGE into the existing manifest (don't wipe non-resolved tiers when
    # called with --tier X). Same rationale as in pull(); see the comment
    # above the manifest write at the end of that function.
    resolved_path = _data_dir() / "resolved-models.json"
    try:
        existing: dict = {}
        if resolved_path.exists():
            try:
                existing = json.loads(resolved_path.read_text(encoding="utf-8")) or {}
            except (OSError, json.JSONDecodeError):
                existing = {}
        merged_tiers = dict(existing.get("tiers") or {})
        merged_tiers.update(result.to_json().get("tiers") or {})
        merged = {**(existing or {}), **result.to_json(), "tiers": merged_tiers}
        resolved_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
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

    # ── Sharded-GGUF support ─────────────────────────────────────────────
    # llama.cpp loads sharded models by following the
    # `<base>-NNNNN-of-MMMMM.gguf` naming convention: point it at the
    # FIRST shard and it walks the same directory for shards 2..M. So
    # to support sharded HF releases (e.g. gpt-oss-120b at 58.5 GB
    # split into 2 files because of HF's 50 GB single-file limit) we
    # need to:
    #   1. detect that the resolved filename matches the shard pattern
    #   2. enumerate every companion shard via the HF API
    #   3. download all of them into target_root (preserving subdir
    #      paths so the side-by-side discovery works)
    #   4. symlink ONLY the first shard to <tier>.gguf — the rest are
    #      found by their original filenames in the same directory
    _SHARD_RE = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$")

    def _list_shard_companions(repo: str, first_filename: str, revision: str) -> list[str]:
        """Return every shard for the file group containing
        ``first_filename`` (including ``first_filename`` itself), sorted
        by shard index. When ``first_filename`` doesn't look sharded,
        returns ``[first_filename]`` unchanged."""
        # Subdirectory and base-name handled separately so the regex
        # only has to match on the basename.
        head, sep, base = first_filename.rpartition("/")
        m = _SHARD_RE.match(base)
        if not m:
            return [first_filename]
        prefix, _, total = m.group(1), m.group(2), m.group(3)
        try:
            from huggingface_hub import HfApi
            info = HfApi().model_info(
                repo, files_metadata=False, revision=revision or "main",
            )
            siblings = list(info.siblings or [])
        except Exception as exc:
            logger.warning(
                "Could not enumerate shards for %s/%s: %s — falling back to single-file pull",
                repo, first_filename, exc,
            )
            return [first_filename]
        prefix_full = f"{head}/{prefix}" if head else prefix
        pat = re.compile(
            rf"^{re.escape(prefix_full)}-(\d{{5}})-of-{total}\.gguf$"
        )
        shards = []
        for s in siblings:
            n = getattr(s, "rfilename", "")
            if pat.match(n):
                shards.append(n)
        # Strict sanity: the count must match the `MMMMM` in the names.
        # If a shard is missing on HF (mid-upload), fall back to the
        # single first-shard pull so we at least don't crash here —
        # llama.cpp will fail more loudly if shards are actually missing.
        shards.sort()
        if len(shards) != int(total):
            logger.warning(
                "Shard count mismatch for %s/%s: found %d shards, manifest says %s. "
                "Pulling what's listed; llama-server will report any actual gap.",
                repo, prefix_full, len(shards), total,
            )
        return shards or [first_filename]

    # Per-shard stall watchdog: when its target .incomplete file
    # hasn't grown in `LAI_PULL_STALL_SECONDS` seconds, disables
    # hf_transfer process-wide so the *next* retry attempt uses the
    # pure-Python path. (Python can't actually kill a thread that's
    # blocked inside the Rust extension — see the comment in
    # `_watchdog` for the full story. Belt-and-suspenders with the
    # module-load default of HF_HUB_ENABLE_HF_TRANSFER=0; mostly
    # matters when the user explicitly re-enables hf_transfer.)
    # Threshold is generous because legitimate slow CDN ranges can
    # pause for 10–30s between chunks; we only act after *no*
    # progress for 90s by default.
    try:
        _STALL_SECONDS = max(20, int(os.getenv("LAI_PULL_STALL_SECONDS", "90")))
    except ValueError:
        _STALL_SECONDS = 90

    def _download_with_retry(**kwargs) -> Path:
        """hf_hub_download wrapper with hf_transfer fallback + N retries.

        Three failure modes we hit on multi-GB GGUFs:
          1. hf_transfer (Rust parallel downloader) bombs on any
             transient error — no built-in retry. We give it one shot
             then disable it process-wide.
          2. The pure-Python downloader gets `IncompleteRead` when the
             CDN connection drops mid-stream. It does NOT auto-resume
             across calls in our setup — but a fresh call resumes from
             the partial blob in ~/.cache/huggingface, so we just retry.
          3. hf_transfer silently deadlocks on Windows under parallel
             shard load: TCP stays ESTABLISHED, no exception, no bytes.
             The stall-watchdog thread below catches this by polling
             the .incomplete file size and killing the worker if it
             plateaus for `_STALL_SECONDS`. (We also default
             HF_HUB_ENABLE_HF_TRANSFER=0 at module load — this watchdog
             is belt-and-suspenders for users who flip it back on.)

        For multi-GB GGUFs the CDN tends to drop connections every
        ~100 MB / 60 s, so an 8-attempt budget never finishes — a 46 GB
        file would need ~500 retries to stitch through. We allow up to
        500 attempts and back off shorter (capped at 10 s) so wall-clock
        progress dominates over wait time.
        """
        import threading

        max_attempts = 500
        last_exc: Exception | None = None
        # Find the .incomplete blob the watchdog should poll. hf_hub
        # writes to `<local_dir>/.cache/huggingface/download/<filename>.<etag>.incomplete`
        # but the etag isn't known until after the download starts — so
        # we glob for any file matching `<basename>.*.incomplete` under
        # the download cache rooted at local_dir.
        local_dir = Path(kwargs.get("local_dir") or _models_dir())
        target_basename = Path(kwargs.get("filename") or "").name
        download_cache = local_dir / ".cache" / "huggingface" / "download"

        def _current_incomplete_size() -> int:
            if not download_cache.exists():
                return 0
            try:
                # filename may include a subdir (e.g. "UD-IQ1_S/foo.gguf"),
                # so search recursively. Take the largest matching file —
                # there should typically be just one but we guard against
                # leftover hash-mismatched stubs.
                candidates = list(
                    download_cache.rglob(f"{target_basename}.*.incomplete")
                )
                return max((c.stat().st_size for c in candidates), default=0)
            except OSError:
                return 0

        for attempt in range(1, max_attempts + 1):
            stall_event = threading.Event()

            def _watchdog() -> None:
                last_size = _current_incomplete_size()
                last_progress = time.monotonic()
                while not stall_event.wait(10):
                    cur = _current_incomplete_size()
                    if cur > last_size:
                        last_size = cur
                        last_progress = time.monotonic()
                        continue
                    if time.monotonic() - last_progress >= _STALL_SECONDS:
                        logger.warning(
                            "Stall-watchdog: %s frozen at %.2f GB for %ds — "
                            "disabling hf_transfer for next retry",
                            target_basename, cur / (1024 ** 3), _STALL_SECONDS,
                        )
                        # Python can't kill the worker thread from here:
                        # if it's blocked inside the hf_transfer Rust
                        # extension, no signal reaches it. What we *can*
                        # do is flip the env so the next retry attempt
                        # uses the pure-Python downloader. The currently
                        # blocked call eventually times out at the TCP
                        # layer (or stays wedged, in which case nothing
                        # short of process kill recovers it). The module
                        # defaults HF_HUB_ENABLE_HF_TRANSFER=0 so this
                        # watchdog only matters when the user has
                        # explicitly re-enabled the accelerator.
                        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
                        return

            wd = threading.Thread(
                target=_watchdog,
                name=f"hf-stall-wd-{target_basename[:30]}",
                daemon=True,
            )
            wd.start()
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
                # Short backoff: HF CDN drops are transient and
                # waiting longer just costs throughput. Capped at 10 s.
                sleep_s = min(10, 2 + (attempt % 4))
                # Only log every 10th retry past attempt 10 — these can
                # accumulate to hundreds for a 46 GB file and we don't
                # want to flood the log with the same one-liner.
                if attempt <= 10 or attempt % 10 == 0:
                    logger.warning(
                        "hf_hub_download attempt %d/%d failed (%s) — retrying in %ds",
                        attempt, max_attempts,
                        msg.splitlines()[0][:140] if msg else type(exc).__name__,
                        sleep_s,
                    )
                time.sleep(sleep_s)
            finally:
                stall_event.set()
                wd.join(timeout=2)
        assert last_exc is not None
        raise last_exc

    for tier, r in result.resolved.items():
        if r.source != "huggingface" or not r.repo or not r.filename:
            continue
        # Defence-in-depth: even if a disabled tier slipped through
        # `resolve()` (e.g. cached pre-disable), don't try to pull it.
        if (r.error or "").startswith("DISABLED:"):
            logger.info("Skipping pull for disabled tier %s: %s", tier, r.error)
            continue

        target_gguf = target_root / f"{tier}.gguf"
        target_mmproj = (
            target_root / f"{tier}.mmproj.gguf" if r.mmproj_filename else None
        )

        # Sharded-aware "is this tier already pulled?" check. The previous
        # logic (`not target_gguf.exists()`) returned False as soon as
        # the symlink existed, even when only the tiny first manifest
        # shard was on disk and shards 2..N were still .incomplete.
        # That broke the watchdog's auto-resume loop: watchdog detects
        # the partial-shard state, kills + respawns the resolver, the
        # respawned resolver sees the symlink and exits without doing
        # anything, watchdog respawns again — infinite loop with no
        # progress.
        #
        # Now: if the resolved filename matches the shard naming
        # pattern, additionally check that EVERY companion shard
        # exists at its expected path under target_root. Any gap
        # forces need_gguf=True so the pull resumes the missing ones.
        # Non-sharded files don't get the per-companion check — the
        # canonical `<tier>.gguf` is the only place we expect to find
        # them on disk; the resolved filename may differ from that
        # canonical name (e.g. `model-q4.gguf` symlinked as `vision.gguf`)
        # and demanding both exist would re-pull a complete file.
        need_gguf = not target_gguf.exists()
        if (not need_gguf) and r.filename:
            base = r.filename.rpartition("/")[2]
            if _SHARD_RE.match(base):
                try:
                    shard_companions = _list_shard_companions(
                        r.repo, r.filename, r.revision or "main",
                    )
                except Exception:
                    shard_companions = [r.filename]
                for sh in shard_companions:
                    shard_path = target_root / sh
                    if not shard_path.exists():
                        logger.info(
                            "tier %s: symlink exists but shard %s missing — re-pulling",
                            tier, sh,
                        )
                        need_gguf = True
                        break

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
                # Sharded GGUF? Pull every companion shard into the
                # same directory so llama.cpp finds them by walking
                # `<base>-NNNNN-of-MMMMM.gguf`. The symlink to
                # data/models/<tier>.gguf points at the FIRST shard;
                # llama-server resolves it, then looks side-by-side
                # for the rest.
                shards = _list_shard_companions(r.repo, r.filename, revision)
                if len(shards) > 1:
                    logger.info(
                        "Pulling %d shards for tier %s in parallel "
                        "(max workers: %d): %s + %d more",
                        len(shards), tier, _PARALLEL_SHARD_WORKERS,
                        shards[0], len(shards) - 1,
                    )
                # Parallel shard download via ThreadPoolExecutor.
                # hf_hub_download is sync I/O, so threads parallelize
                # cleanly. HF allows multiple concurrent connections per
                # IP (we cap at LAI_PARALLEL_SHARDS=4 to avoid burning
                # the CDN's good-graces budget). Each shard has its own
                # ~/.cache/huggingface/.../<basename>.lock so two
                # workers can't trample each other's partial blobs even
                # if the same script is re-run mid-download.
                #
                # Single-shard tiers fall through to the same code path
                # — ThreadPoolExecutor with 1 task degenerates to a
                # synchronous call with negligible overhead.
                shard_paths: dict[str, Path] = {}
                shard_errors: list[tuple[str, Exception]] = []
                from concurrent.futures import ThreadPoolExecutor, as_completed
                workers = max(1, min(_PARALLEL_SHARD_WORKERS, len(shards)))
                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix=f"hf-pull-{tier}",
                ) as pool:
                    futures = {
                        pool.submit(
                            _download_with_retry,
                            repo_id=r.repo,
                            filename=sh,
                            revision=revision,
                            local_dir=str(target_root),
                            token=token,
                        ): sh
                        for sh in shards
                    }
                    for fut in as_completed(futures):
                        sh = futures[fut]
                        try:
                            shard_paths[sh] = fut.result()
                            if len(shards) > 1:
                                logger.info(
                                    "Tier %s shard complete: %s (%.2f GB)",
                                    tier, sh,
                                    shard_paths[sh].stat().st_size / (1024 ** 3),
                                )
                        except Exception as exc:
                            shard_errors.append((sh, exc))
                            logger.warning(
                                "Tier %s shard FAILED: %s — %s",
                                tier, sh, exc,
                            )
                if shard_errors:
                    # Re-raise the first failure so the outer try/except
                    # records the tier as unpulled. Other shards may have
                    # downloaded successfully — they stay in the HF cache
                    # for the next attempt to pick up.
                    raise shard_errors[0][1]
                first_shard_path = shard_paths.get(shards[0])
                if first_shard_path is None:
                    raise RuntimeError(
                        f"first shard {shards[0]!r} missing from completed "
                        "downloads — should be unreachable"
                    )
                # Symlink the canonical <tier>.gguf to the first shard
                # (or to the only file when not sharded — same code path).
                _link_or_copy(first_shard_path, target_gguf)
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

    # Refresh resolved-models.json with the on-disk paths. MERGE into the
    # existing manifest rather than replacing it: when called with a tier
    # subset (e.g. `resolve --pull --tier reasoning_max`), the previous
    # implementation wrote out *only* that tier and wiped every other
    # cached entry — leaving subsequent chat requests on those tiers to
    # fail with `tier 'X' has no gguf_path` even though the .gguf was on
    # disk. Merging preserves entries for tiers we weren't asked about.
    try:
        manifest = _data_dir() / "resolved-models.json"
        existing: dict = {}
        if manifest.exists():
            try:
                existing = json.loads(manifest.read_text(encoding="utf-8")) or {}
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Existing resolved-models.json unreadable, replacing: %s", exc)
                existing = {}
        merged_tiers = dict(existing.get("tiers") or {})
        merged_tiers.update(result.to_json().get("tiers") or {})
        merged = {
            **(existing or {}),
            **result.to_json(),
            "tiers": merged_tiers,
        }
        manifest.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not update resolved-models.json: %s", exc)

    return pulled


# ── CLI ────────────────────────────────────────────────────────────────────

def _main() -> int:
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
    # Install observability AFTER argparse so the suffix can encode which
    # tiers this run targets — useful when several resolve --pull --tier <X>
    # invocations run concurrently (each writes its own runtime state file
    # and gets its own log + proctitle suffix).
    from . import observability as _obs
    _suffix = ",".join(args.tiers) if args.tiers else "all"
    _obs.install("resolver", suffix=_suffix)

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
