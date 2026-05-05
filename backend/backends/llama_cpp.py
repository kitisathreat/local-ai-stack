"""llama.cpp client + per-tier process manager.

The backend manages one ``llama-server`` subprocess per tier. The
``LlamaCppClient`` exposes:

  - chat_stream / chat_once  — OpenAI-compatible streaming + non-streaming
                              chat over the tier's per-process endpoint.
  - ensure_loaded / unload   — spawn / terminate the per-tier llama-server
                              subprocess. Wired into VRAMScheduler.
  - list_running             — diagnostic snapshot of live processes.

Tool calling: when llama-server is launched with ``--jinja`` and a
tool-aware chat template, it returns OpenAI-shaped streaming tool-call
deltas. The client provides a small accumulator that merges
``choices[0].delta.tool_calls`` fragments into complete calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from ..config import TierConfig
from ..model_residency import plan_residency, ResidencyPolicy
from ..schemas import ChatMessage


logger = logging.getLogger(__name__)


# ── Argv builder ────────────────────────────────────────────────────────────

def llama_server_binary() -> str:
    """Resolve the path to ``llama-server[.exe]``.

    Honours the ``LLAMA_SERVER_BIN`` env var; otherwise looks for the
    vendored Windows binary at ``vendor/llama-server/llama-server.exe`` and
    falls back to whatever is on PATH.
    """
    explicit = os.getenv("LLAMA_SERVER_BIN")
    if explicit:
        return explicit
    repo_root = Path(__file__).resolve().parent.parent.parent
    exe_name = "llama-server.exe" if os.name == "nt" else "llama-server"
    vendored = repo_root / "vendor" / "llama-server" / exe_name
    if vendored.exists():
        return str(vendored)
    found = shutil.which(exe_name) or shutil.which("llama-server")
    return found or exe_name


_jinja_supported_cache: bool | None = None
_help_text_cache: str | None = None


def _llama_help_text() -> str:
    """Cached output of `llama-server --help` (stdout+stderr concatenated)."""
    global _help_text_cache
    if _help_text_cache is not None:
        return _help_text_cache
    import subprocess
    try:
        out = subprocess.run(
            [llama_server_binary(), "--help"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _help_text_cache = (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        _help_text_cache = ""
    return _help_text_cache


def _llama_supports_jinja() -> bool:
    """Older builds (≤ b4499) emit only `--chat-template`; newer ones list
    `--jinja` separately. Cached."""
    global _jinja_supported_cache
    if _jinja_supported_cache is None:
        _jinja_supported_cache = bool(
            re.search(r"(?m)^\s*--jinja\b", _llama_help_text())
        )
    return _jinja_supported_cache


def _draft_flag_names() -> tuple[str, str]:
    """Return the (max-flag, min-flag) names for the spec-decode draft.

    Recent llama.cpp upstream renamed `--draft-max`/`--draft-min` to
    `--spec-draft-n-max`/`--spec-draft-n-min`. The vendored binary may
    be either side of that change depending on when it was last
    re-downloaded by the launcher, so probe `--help` instead of pinning
    a single name. Spawning with the wrong name kills the server at
    startup with the cryptic `argument has been removed` error.
    """
    help_text = _llama_help_text()
    if re.search(r"(?m)^\s*--spec-draft-n-max\b", help_text):
        return ("--spec-draft-n-max", "--spec-draft-n-min")
    return ("--draft-max", "--draft-min")


def _resolve_for_llama(gguf_path: str) -> str:
    """Resolve symlinks before passing to llama-server.

    For sharded GGUFs (`<base>-NNNNN-of-MMMMM.gguf`), llama.cpp walks
    the directory of the first shard to discover shards 2..M by
    matching the filename pattern. The canonical `<tier>.gguf` symlink
    we maintain in `data/models/` doesn't match that pattern, so
    passing the symlink path as-is makes llama.cpp error with
    `invalid split file name: <tier>.gguf`. Resolving to the symlink
    target (which IS named `<base>-00001-of-MMMMM.gguf`) makes the
    pattern walk succeed for all sharded tiers (reasoning_max,
    reasoning_xl, frontier, coding_80b when sharded). Single-file
    GGUFs are unaffected — the resolution is a no-op when the path
    isn't a symlink.
    """
    try:
        # Path.resolve() follows symlinks transitively; on Windows this
        # works for both NTFS symlinks and reparse points.
        from pathlib import Path
        target = Path(gguf_path).resolve(strict=False)
        return str(target)
    except (OSError, RuntimeError):
        # If resolution fails (race, missing file), fall through to the
        # original path so llama-server's own error surfacing kicks in
        # with the actual filesystem error (rather than silent breakage).
        return gguf_path


# ── Bench-mode helpers ──────────────────────────────────────────────
# Cache the bench-process scan so spawning N tiers in quick succession
# doesn't shell out N times. 5s TTL aligns with admin._bench_is_running.
_BENCH_SCAN_TTL_S = 5.0
_bench_scan_cache: dict = {"checked_at": 0.0, "active": False}


def _is_bench_active() -> bool:
    """Cheap-ish check for an in-flight scripts/run_full_bench.py.

    Prefers the centralised admin helper when available (one cache for
    the whole process); falls back to a local WMI / /proc scan when
    imported in isolation (test fixtures, standalone llama-server
    spawn during -Setup, etc.)."""
    now = time.time()
    if (now - _bench_scan_cache["checked_at"]) < _BENCH_SCAN_TTL_S:
        return _bench_scan_cache["active"]
    found = False
    try:
        from .. import admin as _admin
        found = _admin._scan_for_bench_process()
    except Exception:
        # Local fallback so this module stays importable without admin.
        try:
            if os.name == "nt":
                cmd = (
                    "powershell.exe -NoProfile -Command "
                    "\"Get-CimInstance Win32_Process -Filter 'Name=\\\"python.exe\\\"' "
                    "| Where-Object { $_.CommandLine -like '*run_full_bench*' } "
                    "| Select-Object -First 1 ProcessId\""
                )
                r = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, timeout=4)
                found = bool(r.stdout and "ProcessId" in r.stdout)
            else:
                for d in Path("/proc").iterdir():
                    if not d.name.isdigit():
                        continue
                    try:
                        cl = (d / "cmdline").read_text(errors="ignore")
                    except (OSError, PermissionError):
                        continue
                    if "run_full_bench" in cl:
                        found = True
                        break
        except Exception:
            pass
    _bench_scan_cache["checked_at"] = now
    _bench_scan_cache["active"] = found
    return found


def _is_serving_role(tier: TierConfig) -> bool:
    """True for non-chat tiers where parallel slots are functionally
    necessary (embedding, reranker, vision). Bench-mode parallel
    override skips these."""
    role = getattr(tier, "role", None)
    return role in ("embedding", "reranker", "vision")


# ── MoE expert offload computer ─────────────────────────────────────
#
# For Mixture-of-Experts models, llama.cpp lets us pin specific tensors
# to CPU via --override-tensor. The original config used a blanket
# `-ot ".ffn_.*_exps.=CPU"` that pushed EVERY expert tensor onto CPU,
# trading throughput for the ability to fit big-MoE weights on a 24 GB
# card. With ~9 GB of GPU headroom free during bench-mode we can
# instead pick a layer-bounded subset that fits — the bench's CPU-bound
# bottleneck (CPU ~99 %, GPU ~30 %) eases as more layers' experts run
# on HBM-fed tensor cores.
#
# This computer reads block_count + expert_count + expert_used_count
# from the model's GGUF metadata, estimates per-layer expert footprint
# from the actual file size, and solves for the maximum number of
# layers whose experts will fit alongside attention + KV + draft on
# the GPU. Returns a regex that pins the LOWER (CPU) layers, leaving
# the upper layers' experts on GPU.

# Architecture-specific share of file weight that is expert tensor
# (the rest is attention, embeddings, layer norms, output head). Hand-
# measured from gguf-dump on representative checkpoints.
_EXPERT_FRACTION_BY_ARCH: dict[str, float] = {
    "qwen3moe":   0.92,
    "qwen35moe":  0.92,
    "qwen3next":  0.90,
    "gpt-oss":    0.85,
    "deepseek2":  0.92,
    "glm45":      0.90,
    # Fallback for unknown MoE architectures
    "default":    0.88,
}


def _gguf_meta(path: str) -> dict:
    """Pull architecture, block_count, expert_count, expert_used_count
    from the GGUF header AND scan the tensor list to compute the exact
    size of expert tensors per layer. Multi-shard GGUFs are aggregated:
    metadata comes from shard 1, tensor sizes are summed across every
    shard (the experts of a sharded model often live in shards 2-N).
    Returns an empty dict on any parse failure (caller falls back to
    full-CPU offload)."""
    # Bytes/element by GGUF type. Source: ggml/src/ggml-quants.h. Q4
    # block size 32 elem in 18 bytes = 0.5625 byte/elem.
    GGUF_TYPE_BPE = {
        0: 4.0,        # F32
        1: 2.0,        # F16
        2: 0.5625,     # Q4_0   (block 32, 18 bytes)
        3: 0.625,      # Q4_1   (block 32, 20 bytes)
        6: 0.6875,     # Q5_0   (block 32, 22 bytes)
        7: 0.75,       # Q5_1   (block 32, 24 bytes)
        8: 1.0625,     # Q8_0   (block 32, 34 bytes)
        9: 1.0625,     # Q8_1
        10: 0.5625,    # Q2_K   (super-block 256, 144 bytes ≈ 0.5625)
        11: 0.40625,   # Q3_K_S (super-block 256, 104 bytes)
        12: 0.4375,    # Q3_K   (super-block 256, 112 bytes)
        13: 0.5,       # Q4_K_S
        14: 0.5625,    # Q4_K   (super-block 256, 144 bytes)
        15: 0.625,     # Q5_K   (super-block 256, 160 bytes)
        16: 0.6875,    # Q6_K   (super-block 256, 176 bytes)
        17: 1.0625,    # Q8_K
        18: 0.5625,    # IQ2_XXS — actual: ~2.06 bpw → 0.2575 byte/elem
        19: 0.5625,    # IQ2_XS
        20: 0.5625,    # IQ3_XXS
        21: 0.5625,    # IQ1_S
        22: 0.5625,    # IQ4_NL
        23: 0.5625,    # IQ3_S
        24: 0.5625,    # IQ2_S
        25: 0.5625,    # IQ4_XS
        26: 0.3125,    # IQ2_M  (~2.5 bpw)
        27: 0.5,       # BF16
        # ... many more; default to 0.5 byte/elem if unknown
    }
    try:
        with open(path, "rb") as f:
            import struct
            if f.read(4) != b"GGUF":
                return {}
            struct.unpack("<I", f.read(4))[0]
            tensor_count = struct.unpack("<Q", f.read(8))[0]
            mcount = struct.unpack("<Q", f.read(8))[0]
            out: dict = {}
            for _ in range(mcount):
                klen = struct.unpack("<Q", f.read(8))[0]
                key = f.read(klen).decode("utf-8", "ignore")
                vt = struct.unpack("<I", f.read(4))[0]

                def read_val(t):
                    if t in (0, 1):
                        f.read(1); return None
                    if t in (2, 3):
                        f.read(2); return None
                    if t == 4:
                        return struct.unpack("<I", f.read(4))[0]
                    if t == 5:
                        return struct.unpack("<i", f.read(4))[0]
                    if t == 6:
                        f.read(4); return None
                    if t == 7:
                        f.read(1); return None
                    if t == 8:
                        sl = struct.unpack("<Q", f.read(8))[0]
                        return f.read(sl).decode("utf-8", "ignore")
                    if t == 9:
                        at = struct.unpack("<I", f.read(4))[0]
                        al = struct.unpack("<Q", f.read(8))[0]
                        for _i in range(al):
                            read_val(at)
                        return None
                    if t == 10:
                        return struct.unpack("<Q", f.read(8))[0]
                    if t == 11:
                        return struct.unpack("<q", f.read(8))[0]
                    if t == 12:
                        f.read(8); return None

                v = read_val(vt)
                if key == "general.architecture":
                    out["arch"] = v
                for tail in ("block_count", "expert_count",
                             "expert_used_count", "embedding_length",
                             "context_length"):
                    if key.endswith("." + tail):
                        out[tail] = v

            # Now scan THIS shard's tensor list. Each entry: name,
            # n_dims, dim[0..n-1], type (uint32), offset (uint64).
            expert_bytes = 0
            nonexpert_bytes = 0
            for _ in range(tensor_count):
                nlen = struct.unpack("<Q", f.read(8))[0]
                tname = f.read(nlen).decode("utf-8", "ignore")
                ndims = struct.unpack("<I", f.read(4))[0]
                dims = [struct.unpack("<Q", f.read(8))[0] for _ in range(ndims)]
                ttype = struct.unpack("<I", f.read(4))[0]
                f.read(8)  # skip offset
                n_elems = 1
                for d in dims:
                    n_elems *= d
                bpe = GGUF_TYPE_BPE.get(ttype, 0.5)
                size = int(n_elems * bpe)
                if "_exps" in tname:
                    expert_bytes += size
                else:
                    nonexpert_bytes += size
            out["expert_bytes"] = expert_bytes
            out["nonexpert_bytes"] = nonexpert_bytes

            # If this is a multi-shard model (filename ends in
            # -NNNNN-of-MMMMM.gguf), open shards 2..M and accumulate
            # their tensor bytes into the same expert/nonexpert totals.
            # Sharded GGUFs store metadata only in shard 1, so we just
            # need their tensor lists.
            try:
                p = Path(path)
                m = re.match(r"^(.+)-(\d{5})-of-(\d{5})\.gguf$", p.name)
                if m:
                    prefix = m.group(1)
                    n_shards = int(m.group(3))
                    for i in range(2, n_shards + 1):
                        shard = p.parent / f"{prefix}-{i:05d}-of-{n_shards:05d}.gguf"
                        if not shard.exists():
                            continue
                        e2, n2 = _gguf_tensor_bytes(str(shard), GGUF_TYPE_BPE)
                        out["expert_bytes"] += e2
                        out["nonexpert_bytes"] += n2
            except Exception as exc:
                logger.debug("gguf_meta shard scan failed: %s", exc)
            return out
    except Exception as e:
        logger.debug("gguf_meta parse failed for %s: %s", path, e)
        return {}


def _gguf_tensor_bytes(path: str, bpe_table: dict) -> tuple[int, int]:
    """Helper: scan tensor list of a (typically non-first) GGUF shard
    and return (expert_bytes, nonexpert_bytes). Skips the metadata KV
    block (shards 2..N have empty metadata in practice but we parse
    them anyway since some tools write per-shard metadata)."""
    import struct
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return (0, 0)
            struct.unpack("<I", f.read(4))[0]
            tcount = struct.unpack("<Q", f.read(8))[0]
            mcount = struct.unpack("<Q", f.read(8))[0]

            def skip_val(t):
                if t in (0, 1): f.read(1)
                elif t in (2, 3): f.read(2)
                elif t == 4: f.read(4)
                elif t == 5: f.read(4)
                elif t == 6: f.read(4)
                elif t == 7: f.read(1)
                elif t == 8:
                    sl = struct.unpack("<Q", f.read(8))[0]; f.read(sl)
                elif t == 9:
                    at = struct.unpack("<I", f.read(4))[0]
                    al = struct.unpack("<Q", f.read(8))[0]
                    for _i in range(al): skip_val(at)
                elif t == 10: f.read(8)
                elif t == 11: f.read(8)
                elif t == 12: f.read(8)
            for _ in range(mcount):
                kl = struct.unpack("<Q", f.read(8))[0]
                f.read(kl)  # key
                vt = struct.unpack("<I", f.read(4))[0]
                skip_val(vt)
            ex = ne = 0
            for _ in range(tcount):
                nl = struct.unpack("<Q", f.read(8))[0]
                nm = f.read(nl).decode("utf-8", "ignore")
                nd = struct.unpack("<I", f.read(4))[0]
                ds = [struct.unpack("<Q", f.read(8))[0] for _ in range(nd)]
                tt = struct.unpack("<I", f.read(4))[0]
                f.read(8)
                ne_ = 1
                for d in ds: ne_ *= d
                bpe = bpe_table.get(tt, 0.5)
                sz = int(ne_ * bpe)
                if "_exps" in nm: ex += sz
                else: ne += sz
            return (ex, ne)
    except Exception:
        return (0, 0)


def _model_total_size_gb(gguf_path: str) -> float:
    """File size on disk for a GGUF, summing shards if the path is the
    first of an N-shard set (filename pattern ...-00001-of-NNNNN.gguf).
    Returns 0 when the file is missing."""
    p = Path(gguf_path)
    if not p.exists():
        return 0.0
    total = p.stat().st_size
    # Detect multi-shard pattern and sum siblings
    name = p.name
    m = re.match(r"^(.+)-(\d{5})-of-(\d{5})\.gguf$", name)
    if m:
        prefix, _idx, total_shards = m.group(1), m.group(2), int(m.group(3))
        for i in range(1, total_shards + 1):
            shard = p.parent / f"{prefix}-{i:05d}-of-{total_shards:05d}.gguf"
            if shard.exists() and shard != p:
                total += shard.stat().st_size
    return total / (1024 ** 3)


def _gpu_total_gb_default() -> float:
    """Best effort: read total VRAM from nvidia-smi. Falls back to 24
    when nvidia-smi isn't available (matches the rig the configs were
    sized against — operators running smaller cards get a more
    conservative offload than ideal but no OOM)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode == 0 and r.stdout.strip():
            mb = int(r.stdout.strip().split("\n")[0])
            return mb / 1024.0
    except Exception:
        pass
    return 24.0


def _compute_moe_offload_regex(tier: TierConfig) -> str | None:
    """Return a `-ot` regex value that pins the lower-N layers' MoE
    expert tensors to CPU and leaves the upper layers on GPU. Returns
    None when:
      - The model isn't MoE (no expert_count in GGUF)
      - The whole model fits on GPU (no offload needed)
      - The model is so big even one layer of experts won't fit
        (caller should use full-CPU offload as fallback)

    The split is computed from architecture metrics: per-layer expert
    weight × layer count vs available GPU budget."""
    if not tier.gguf_path:
        return None
    meta = _gguf_meta(tier.gguf_path)
    expert_count = meta.get("expert_count") or 0
    block_count = meta.get("block_count") or 0
    if expert_count <= 0 or block_count <= 0:
        # Not MoE, or metadata missing — caller decides.
        return None

    arch = (meta.get("arch") or "").lower()

    # Use the EXACT tensor-list sums we extracted from the GGUF when
    # available — file × global expert_fraction was off by 2× on
    # heavy-shared MoEs like Qwen3.6-35B-A3B (50% non-expert) compared
    # to sparse ones like Qwen3-Next-80B-A3B (12% non-expert). The
    # tensor list gives us per-tier truth without architecture
    # guessing.
    file_gb = _model_total_size_gb(tier.gguf_path)
    if file_gb <= 0:
        return None
    expert_bytes = meta.get("expert_bytes") or 0
    nonexpert_bytes = meta.get("nonexpert_bytes") or 0
    parser_total_gb = (expert_bytes + nonexpert_bytes) / (1024 ** 3)
    if parser_total_gb > 0 and file_gb > 0:
        # Normalise against the actual file size on disk — the BPE
        # table is approximate (especially for UD-style mixed-quant
        # GGUFs with IQ-family types) and can be off by 2× either way.
        # We trust the EXPERT/NON-EXPERT RATIO from the parser but
        # rescale to file size so absolute numbers align with reality.
        scale = file_gb / parser_total_gb
        expert_gb = (expert_bytes / (1024 ** 3)) * scale
        nonexpert_gb = (nonexpert_bytes / (1024 ** 3)) * scale
    else:
        # No tensor scan or empty result — fall back to per-arch
        # global fraction of file size.
        expert_fraction = _EXPERT_FRACTION_BY_ARCH.get(
            arch, _EXPERT_FRACTION_BY_ARCH["default"])
        expert_gb = file_gb * expert_fraction
        nonexpert_gb = file_gb * (1.0 - expert_fraction)
    per_layer_expert_gb = expert_gb / block_count

    # Baseline GPU footprint that is NOT expert weights:
    #   - Non-expert weights (attention, embeds, output head, norms)
    #   - KV cache at this tier's context_window × parallel × bytes/dtype
    #   - Spec-decode draft model (if configured)
    #   - 4 GB safety margin (activations, cuda graphs, prompt-eval
    #     scratch buffers — measured 3-5 GB on Qwen3 spec-decode)

    # KV cache = 2 (k+v) × block_count × n_kv_heads × head_dim × ctx × bytes
    # Without n_kv_heads in the gguf scan, approximate from embedding_length
    # × ctx_window × per-token-bytes-per-layer-pair. Q4_0 ≈ 0.5 byte/elem.
    embed = meta.get("embedding_length") or 4096
    ctx = tier.context_window
    parallel = max(1, tier.parallel_slots) if not _is_bench_active() else 1
    pool_ctx = ctx * parallel
    bytes_per_kv_elem = 0.5 if (tier.cache_type_k or "").startswith("q4") else (
        1.0 if (tier.cache_type_k or "").startswith("q8") else 2.0)
    # Conservative: assume 1/4 of embed_dim is the KV head dim aggregate
    # (typical GQA ratio). Result is a rough overestimate which is what
    # we want for the safety budget.
    kv_gb = (2 * block_count * (embed // 4) * pool_ctx * bytes_per_kv_elem) / (1024 ** 3)

    draft_gb = 0.0
    if getattr(tier, "draft_gguf_path", None):
        draft_gb = _model_total_size_gb(tier.draft_gguf_path) * 0.6  # weights only

    # 6 GB covers CUDA graphs, prompt-evaluation scratch buffers, and
    # general activation overhead beyond the weights + KV. Empirical
    # observation on this rig (versatile loaded with full-CPU experts
    # measured 15 GB GPU but theoretical weight+KV+draft was only 7 GB),
    # the gap is ~5-8 GB of CUDA-side overhead that scales with model
    # size and prompt length. Sized at 6 GB to absorb variation across
    # tiers without pushing partial-offload tiers into "fits entirely"
    # (which would actually OOM at runtime).
    safety_gb = 6.0
    gpu_total_gb = _gpu_total_gb_default()
    gpu_baseline_gb = nonexpert_gb + kv_gb + draft_gb + safety_gb
    gpu_budget_for_experts_gb = max(0.0, gpu_total_gb - gpu_baseline_gb)

    if per_layer_expert_gb <= 0:
        return None
    gpu_layers = int(gpu_budget_for_experts_gb // per_layer_expert_gb)
    gpu_layers = max(0, min(gpu_layers, block_count))

    if gpu_layers <= 0:
        # Can't fit even one layer of experts — caller should use full
        # CPU offload (which is what the existing config does).
        logger.info(
            "moe-offload[%s]: gpu_layers=0 (per-layer experts %.1f GB > budget %.1f GB) "
            "→ keeping full-CPU expert offload",
            tier.gguf_path.split("\\")[-1].split("/")[-1],
            per_layer_expert_gb, gpu_budget_for_experts_gb,
        )
        return ".ffn_.*_exps.=CPU"
    if gpu_layers >= block_count:
        # Full model fits on GPU — no offload needed.
        return ""

    cpu_layers = block_count - gpu_layers
    # Build a regex that matches blk.<i> for i in [0, cpu_layers).
    # Pattern: 0|1|2|...|<cpu_layers-1>. Compact form using ranges.
    cpu_indices = list(range(cpu_layers))
    # Group by digit count for a tighter regex than verbose alternation.
    # blk\.(0|1|2|...|9|10|11|...)\.ffn_.*_exps\.=CPU
    alt = "|".join(str(i) for i in cpu_indices)
    regex = rf"blk\.({alt})\.ffn_.*_exps\.=CPU"
    logger.info(
        "moe-offload[%s]: arch=%s layers=%d experts=%d/%d  "
        "file=%.1f GB  nonexpert=%.1f GB  KV=%.1f GB  draft=%.1f GB  "
        "→ CPU layers 0..%d (%d), GPU layers %d..%d (%d)",
        tier.gguf_path.split("\\")[-1].split("/")[-1], arch,
        block_count, meta.get("expert_used_count") or 0, expert_count,
        file_gb, nonexpert_gb, kv_gb, draft_gb,
        cpu_layers - 1, cpu_layers, cpu_layers, block_count - 1, gpu_layers,
    )
    return regex


def build_argv(tier: TierConfig) -> list[str]:
    """Produce the llama-server argv for `tier`."""
    if not tier.gguf_path:
        raise ValueError(
            f"tier {tier.name!r} has no gguf_path — run -Setup or model_resolver"
        )
    if tier.port is None:
        raise ValueError(f"tier {tier.name!r} has no port")

    # llama-server's --ctx-size is the TOTAL KV pool divided across
    # --parallel slots; per-slot context = ctx_size / parallel. The
    # config's `context_window` is the per-slot value the operator
    # actually wants (= what a single conversation can use), so the
    # launcher must multiply by parallel_slots before passing to
    # llama-server.
    #
    # Without this multiplication the bench's long-context cells (and
    # any user prompt over 2k tokens on tiers with parallel_slots ≥ 8)
    # would silently fail with HTTP 400 "exceeds the available context
    # size" — for example swarm with context_window=16384 and
    # parallel_slots=8 used to give EACH slot only 2048 tokens of
    # context, rejecting every needle prompt.
    parallel = max(1, tier.parallel_slots)
    # Bench-mode override: when run_full_bench.py is the active driver
    # we run requests serially, so reserving 4-8 KV slots per chat tier
    # is just wasted VRAM. Detect the bench process and force
    # parallel=1 for chat tiers — saves ~6 GB on fast/coding/versatile
    # and keeps the 24 GB card from OOMing when the bigger tiers
    # (highest_quality, reasoning_max, reasoning_xl) load. Skips
    # embedding/reranker because those have their own concurrency
    # needs (multiple tools query embeddings in parallel for a single
    # chat turn). Cached for 5s — same TTL as admin._bench_is_running.
    if _is_bench_active() and not _is_serving_role(tier):
        parallel = 1
    total_ctx = tier.context_window * parallel
    argv: list[str] = [
        llama_server_binary(),
        "--host", "127.0.0.1",
        "--port", str(tier.port),
        "-m", _resolve_for_llama(tier.gguf_path),
        "--ctx-size", str(total_ctx),
        "--parallel", str(parallel),
        "-ngl", str(tier.n_gpu_layers),
        "--cache-type-k", tier.cache_type_k,
        "--cache-type-v", tier.cache_type_v,
    ]
    # `--jinja` enables strict Jinja2-template-driven tool-call grammar
    # but only exists in llama.cpp ≥ b4500 (early 2025). Older builds
    # (e.g. b4404, the launcher's current pin) reject it with
    # "error: invalid argument: --jinja" and exit during startup,
    # killing every chat tier. Probe the binary's --help once and only
    # add the flag when supported. Without it, llama-server falls back
    # to the model's built-in chat template which is fine for Qwen.
    if _llama_supports_jinja():
        argv.append("--jinja")
    if tier.flash_attention:
        # llama.cpp ≥ b8992 requires -fa to take a value (on/off/auto);
        # older builds accepted bare -fa. Always pass `on` — both old
        # and new builds tolerate it.
        argv += ["-fa", "on"]
    if tier.mmproj_path:
        argv += ["--mmproj", tier.mmproj_path]
    if tier.use_mlock:
        argv.append("--mlock")
    if not tier.use_mmap:
        argv.append("--no-mmap")
    # When the residency cascade chose to spill the KV cache to system
    # RAM (set via tier.kv_offload after plan_residency tightens for fit),
    # llama-server's --no-kv-offload flag keeps the cache off the GPU.
    # Frees several GB at long contexts; costs attention bandwidth.
    if getattr(tier, "kv_offload", False):
        argv.append("--no-kv-offload")
    if tier.rope_scaling and tier.rope_scaling.factor and tier.rope_scaling.factor != 1.0:
        argv += ["--rope-scaling", tier.rope_scaling.type]
        argv += ["--rope-scale", str(tier.rope_scaling.factor)]
        if tier.rope_scaling.orig_ctx:
            argv += ["--yarn-orig-ctx", str(tier.rope_scaling.orig_ctx)]
    # Auto-compute MoE expert offload from GGUF metadata + GPU budget.
    # If the model is MoE, this returns a regex that pins only the
    # lower-N layers' experts to CPU (the rest run on GPU). The split
    # is a function of block_count, expert_fraction, file size, KV
    # footprint, draft model, and total VRAM — so each tier gets its
    # own optimum without hand-baked layer indices in the YAML.
    #
    # An empty string means "no offload needed, model fits". Returning
    # None means "not MoE or metadata missing" — in either case we
    # fall through to whatever extra_args/override_tensors the YAML
    # specifies.
    auto_ot = _compute_moe_offload_regex(tier)
    extra_args = list(tier.extra_args) if tier.extra_args else []
    override_patterns = list(tier.override_tensors) if tier.override_tensors else []
    if auto_ot is not None:
        # Strip any operator-supplied `-ot <expr>` pair from extra_args
        # AND any pattern from tier.override_tensors when it targets
        # MoE expert tensors — the auto-computed regex replaces it.
        # Without dropping override_patterns too, llama.cpp would see
        # both `-ot blk\.(0..12)\.ffn_.*_exps=CPU` and the wildcard
        # `-ot .ffn_.*_exps.=CPU` and the wildcard would catch every
        # remaining layer, undoing the partial split.
        cleaned: list[str] = []
        i = 0
        while i < len(extra_args):
            tok = extra_args[i]
            nxt = extra_args[i + 1] if i + 1 < len(extra_args) else ""
            if tok == "-ot" and "_exps" in nxt:
                i += 2
                continue
            cleaned.append(tok)
            i += 1
        extra_args = cleaned
        override_patterns = [p for p in override_patterns if "_exps" not in p]
        if auto_ot:
            extra_args += ["-ot", auto_ot]
    argv += extra_args
    for pattern in override_patterns:
        argv += ["-ot", pattern]
    # Speculative decoding. When a draft GGUF is resolved for this tier,
    # llama-server runs Leviathan-style spec decode against it: the
    # draft proposes draft_max tokens, the target verifies them in one
    # parallel batch, rejection-sampling keeps the joint distribution
    # identical to running the target alone. Quality is preserved
    # exactly — speedup is purely from amortizing memory bandwidth.
    # Required: draft and target must share a tokenizer (caller's
    # responsibility — the YAML wires Qwen3-0.6B for Qwen3-family tiers
    # only). Flag spelling matches llama.cpp ≥ b8992 (LocalAIStack.ps1
    # pinned version).
    if tier.draft_gguf_path:
        # Defensive: if the draft GGUF file isn't actually on disk
        # (resolver wrote the manifest entry but the pull failed — common
        # when HF_TOKEN isn't set and the draft model is gated),
        # llama-server crashes during startup with a cryptic error. Skip
        # spec-decode in that case and run target-only — quality is
        # identical, just no speedup.
        from pathlib import Path as _P
        if _P(tier.draft_gguf_path).exists():
            # Recent llama.cpp upstream renamed --draft-max / --draft-min to
            # --spec-draft-n-max / --spec-draft-n-min. Probe the binary for
            # the right pair instead of pinning either — wrong name aborts
            # the server before it even loads weights.
            max_flag, min_flag = _draft_flag_names()
            argv += [
                "-md", _resolve_for_llama(tier.draft_gguf_path),
                "-ngld", str(tier.draft_n_gpu_layers),
                max_flag, str(tier.draft_max),
                min_flag, str(tier.draft_min),
            ]
        else:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Spec-decode draft missing for tier %s (%s) — running "
                "target-only. To enable spec-decode, set HF_TOKEN in .env "
                "and run `pwsh .\\LocalAIStack.ps1 -CheckUpdates`.",
                tier.name, tier.draft_gguf_path,
            )
    return argv


# ── Per-tier process handle ────────────────────────────────────────────────

@dataclass
class LlamaServerProcess:
    """One llama-server subprocess. Owned by the LlamaCppClient registry."""

    tier_id: str
    port: int
    endpoint: str
    argv: list[str] = field(default_factory=list)
    popen: subprocess.Popen | None = None
    started_at: float = 0.0
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=128))
    _reader_task: asyncio.Task | None = None
    _externally_managed: bool = False    # True when adopted (PS1 pre-spawn)

    def is_alive(self) -> bool:
        if self._externally_managed:
            return True
        if self.popen is None:
            return False
        return self.popen.poll() is None

    async def wait_ready(self, timeout: float = 180.0) -> None:
        """Poll GET /health on the endpoint until 200 or timeout. The
        embedding server doesn't expose /health; we fall back to /v1/models."""
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=4.0) as client:
            while time.monotonic() < deadline:
                if not self.is_alive():
                    raise RuntimeError(
                        f"llama-server for {self.tier_id} exited during startup\n"
                        + "\n".join(list(self.stderr_tail)[-20:])
                    )
                for path in ("/health", "/models"):
                    try:
                        url = self.endpoint.rstrip("/") + path
                        r = await client.get(url)
                        if r.status_code == 200:
                            return
                    except httpx.HTTPError as exc:
                        last_err = exc
                await asyncio.sleep(0.5)
        raise TimeoutError(
            f"llama-server for {self.tier_id} did not become ready in "
            f"{timeout:.0f}s ({last_err})"
        )

    async def start(self) -> None:
        if self.is_alive():
            return
        creationflags = 0
        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP so terminate() works on Windows.
            # CREATE_NO_WINDOW hides the conhost flash that would
            # otherwise pop on every chat-tier cold-spawn — visible to
            # the user even when the launcher itself is hidden, because
            # conhost.exe is a foreground UI window the OS opens for any
            # console subprocess spawned without this flag.
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        logger.info("Starting llama-server for %s: %s", self.tier_id, " ".join(self.argv))
        try:
            self.popen = subprocess.Popen(
                self.argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                close_fds=(os.name != "nt"),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"llama-server binary not found ({self.argv[0]!r}). "
                "Run -Setup or set LLAMA_SERVER_BIN."
            ) from exc
        self.started_at = time.monotonic()
        self._reader_task = asyncio.create_task(self._tail_stderr())

    async def _tail_stderr(self) -> None:
        if self.popen is None or self.popen.stderr is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, self.popen.stderr.readline)
            if not line:
                break
            try:
                self.stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
            except Exception:
                pass

    async def stop(self, timeout: float = 30.0) -> None:
        if self._externally_managed:
            return
        if self.popen is None:
            return
        if self.popen.poll() is not None:
            self.popen = None
            return
        logger.info("Stopping llama-server for %s (pid=%s)", self.tier_id, self.popen.pid)
        try:
            if os.name == "nt":
                self.popen.terminate()
            else:
                self.popen.send_signal(signal.SIGTERM)
        except Exception as exc:
            logger.warning("terminate() failed for %s: %s", self.tier_id, exc)

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self.popen.wait(timeout))
        except subprocess.TimeoutExpired:
            logger.warning("llama-server %s did not exit; killing", self.tier_id)
            try:
                self.popen.kill()
            except Exception:
                pass
            with contextlib.suppress(Exception):
                await loop.run_in_executor(None, lambda: self.popen.wait(5))
        finally:
            self.popen = None
            if self._reader_task is not None:
                self._reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._reader_task
                self._reader_task = None


# ── Message conversion ─────────────────────────────────────────────────────

def _messages_to_payload(messages: list[ChatMessage] | list[dict]) -> list[dict]:
    """Convert ChatMessage objects to OpenAI-shaped dicts. Pre-serialized
    dicts (used by the tool loop for role=tool entries) pass through."""
    out: list[dict] = []
    for m in messages:
        if isinstance(m, dict):
            out.append(m)
            continue
        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
        else:
            parts: list[dict] = []
            for p in m.content:
                if p.type == "text":
                    parts.append({"type": "text", "text": p.text or ""})
                elif p.type == "image_url" and p.image_url:
                    parts.append({"type": "image_url", "image_url": p.image_url})
            out.append({"role": m.role, "content": parts})
    return out


# ── Streaming tool-call accumulator ────────────────────────────────────────

class ToolCallAccumulator:
    """Aggregates streaming OpenAI-shaped tool_call delta fragments into
    complete `{id, type, function: {name, arguments}}` records."""

    def __init__(self) -> None:
        self._buf: dict[int, dict] = {}

    def feed(self, fragments: list[dict] | None) -> None:
        if not fragments:
            return
        for frag in fragments:
            idx = int(frag.get("index", 0))
            slot = self._buf.setdefault(
                idx,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if frag.get("id"):
                slot["id"] = frag["id"]
            if frag.get("type"):
                slot["type"] = frag["type"]
            fn_frag = frag.get("function") or {}
            if fn_frag.get("name"):
                slot["function"]["name"] = fn_frag["name"]
            if fn_frag.get("arguments"):
                slot["function"]["arguments"] += fn_frag["arguments"]

    def calls(self) -> list[dict]:
        return [self._buf[k] for k in sorted(self._buf)]


# ── Client ─────────────────────────────────────────────────────────────────

class LlamaCppClient:
    """OpenAI-compatible client managing one llama-server per tier.

    The constructor takes no global endpoint — each call routes by
    `tier.resolved_endpoint()`. The registry is used to track which tiers
    are currently RESIDENT (process alive + /health 200).
    """

    def __init__(self, timeout_sec: float = 600.0):
        self.timeout = httpx.Timeout(timeout_sec, connect=10.0)
        self.processes: dict[str, LlamaServerProcess] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, tier_id: str) -> asyncio.Lock:
        lock = self._locks.get(tier_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[tier_id] = lock
        return lock

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def ensure_loaded(
        self,
        tier: TierConfig,
        *,
        free_vram_gb: float | None = None,
        live_user_text: str = "",
        spawn_timeout: float | None = None,
    ) -> float:
        """Make sure the per-tier llama-server is up + healthy.

        Returns elapsed seconds (useful for VRAMScheduler observed-cost
        measurement). Idempotent.

        If a process is already listening on `tier.port` (e.g. pre-spawned by
        the launcher for vision/embedding), the client adopts it instead of
        spawning a duplicate.

        When ``LAI_RESIDENCY_PLANNER=1`` is set in the environment AND
        ``free_vram_gb`` is provided, the per-spawn ``n_gpu_layers /
        use_mmap / use_mlock`` are computed by ``plan_residency`` instead
        of read from YAML. Otherwise the YAML values are used verbatim
        (existing behavior).
        """
        endpoint = tier.resolved_endpoint()
        timeout = spawn_timeout if spawn_timeout is not None else float(tier.spawn_timeout_sec)
        async with self._lock(tier.name):
            proc = self.processes.get(tier.name)
            t0 = time.monotonic()
            if proc and proc.is_alive():
                return 0.0
            # Pre-spawn orphan reap: a previous evict() may have logged
            # "Evicted tier X" but the llama-server process can survive
            # the terminate() and keep ~10–15 GB of VRAM allocated until
            # we forcibly kill it. Reaping NOW (before we spawn this
            # tier) means the new process loads into clean VRAM rather
            # than triggering the residency planner's KV→CPU cascade
            # because the GPU looks falsely full. Skipped silently if
            # there's nothing to reap.
            try:
                killed = await self.kill_orphans()
                if killed:
                    logger.warning(
                        "Pre-spawn reap killed %d stray llama-server PID(s) "
                        "before loading %s: %s", len(killed), tier.name, killed,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Pre-spawn reap failed for %s: %s", tier.name, exc)
            # Adopt a pre-spawned external process, if any.
            if await self._port_is_serving(endpoint):
                logger.info("Adopting external llama-server for tier %s on %s", tier.name, endpoint)
                self.processes[tier.name] = LlamaServerProcess(
                    tier_id=tier.name,
                    port=tier.port or 0,
                    endpoint=endpoint,
                    _externally_managed=True,
                )
                return time.monotonic() - t0

            # Residency planner — fitting cascade: layer offload → KV-on-CPU
            # → ctx shrink. Gated by `vram.residency.enable` in YAML; the
            # legacy LAI_RESIDENCY_PLANNER env var still force-enables the
            # planner so existing rollouts keep working until the YAML
            # default propagates.
            cfg = None
            try:
                from ..config import get_config
                cfg = get_config()
            except Exception:
                cfg = None
            planner_on = (
                (cfg is not None and getattr(cfg.vram.residency, "enable", False))
                or os.getenv("LAI_RESIDENCY_PLANNER") == "1"
            )
            if planner_on and free_vram_gb is not None and cfg is not None:
                policy = ResidencyPolicy(
                    full_headroom_multiplier=cfg.vram.residency.full_headroom_multiplier,
                    partial_min_ratio=cfg.vram.residency.partial_min_ratio,
                    minimal_ratio=cfg.vram.residency.minimal_ratio,
                    low_complexity_savings=cfg.vram.residency.low_complexity_savings,
                    mlock_full_mode=cfg.vram.residency.mlock_full_mode,
                    mlock_partial_mode=cfg.vram.residency.mlock_partial_mode,
                    enable_kv_offload=cfg.vram.residency.enable_kv_offload,
                    enable_ctx_shrink=cfg.vram.residency.enable_ctx_shrink,
                    min_context_window=cfg.vram.residency.min_context_window,
                )
                plan = plan_residency(
                    tier,
                    free_vram_gb=free_vram_gb,
                    live_user_text=live_user_text,
                    policy=policy,
                )
                # Apply the plan to a tier copy (don't mutate the registry singleton).
                # to_backend_options() already returns kv_offload + ctx override
                # when the cascade flipped them.
                tier = tier.model_copy(update=plan.to_backend_options())
                logger.info(
                    "Residency plan for %s: mode=%s layers=%d/%d ctx=%d "
                    "kv_offload=%s reason=%s",
                    tier.name, plan.mode.value,
                    plan.num_gpu_layers, plan.total_layers,
                    plan.context_window or tier.context_window,
                    plan.kv_offload, plan.reason,
                )

            argv = build_argv(tier)
            proc = LlamaServerProcess(
                tier_id=tier.name,
                port=tier.port or 0,
                endpoint=endpoint,
                argv=argv,
            )
            await proc.start()
            # Register in self.processes BEFORE wait_ready: the 80B
            # tiers take 25–30s to become HTTP-ready, and the periodic
            # orphan-reap (every ~30s) would otherwise see the live
            # PID with no registry entry and SIGKILL it as a stray.
            # Observed May 2026: highest_quality (pid 26616, port 8010)
            # was killed mid-spawn, breaking the cell with tok=0
            # cascades. Registering early closes the race; if
            # wait_ready fails we still pop+stop in the except.
            self.processes[tier.name] = proc
            try:
                await proc.wait_ready(timeout=timeout)
            except Exception:
                self.processes.pop(tier.name, None)
                await proc.stop(timeout=5.0)
                raise
            return time.monotonic() - t0

    async def unload(self, tier: TierConfig) -> None:
        async with self._lock(tier.name):
            proc = self.processes.pop(tier.name, None)
            if proc is None:
                return
            await proc.stop()
        # After stop(), the popen handle is gone but Windows can leave the
        # llama-server process briefly in zombie state, OR — as observed
        # during May 2026 benches — the process can survive terminate()
        # entirely (especially with -ot expert offload + KV pinning) and
        # keep its VRAM allocated. Sweep llama-server PIDs not tracked by
        # the registry and SIGKILL anything that isn't the embedding /
        # reranker / vision tier the launcher pre-spawned.
        try:
            killed = await self.kill_orphans()
            if killed:
                logger.warning(
                    "Post-unload reap killed %d stray llama-server PID(s) after "
                    "evicting %s: %s", len(killed), tier.name, killed,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Post-unload reap failed for %s: %s", tier.name, exc)

    async def list_running(self) -> list[dict]:
        return [
            {
                "tier_id": p.tier_id,
                "port": p.port,
                "endpoint": p.endpoint,
                "alive": p.is_alive(),
                "external": p._externally_managed,
                "uptime_sec": (time.monotonic() - p.started_at) if p.started_at else 0,
            }
            for p in self.processes.values()
        ]

    async def stop_all(self) -> None:
        for tier_id in list(self.processes):
            proc = self.processes.pop(tier_id, None)
            if proc is not None:
                with contextlib.suppress(Exception):
                    await proc.stop()

    async def kill_orphans(self, preserve_ports: set[int] | None = None) -> list[int]:
        """Kill llama-server processes not tracked by this client.

        Backend bounces (refresh-backend.ps1, dev autoreload, crashes)
        can leave llama-server children alive but unreachable from the
        new backend's process registry. Their VRAM stays allocated, NVML
        reports it as used, and the next ``_make_room_for`` decision
        sees a phantom shortfall. This walks the live llama-server PIDs
        and SIGTERMs anything we don't own.

        ``preserve_ports`` is the set of ports that belong to externally-
        managed processes the backend should leave alone (typically the
        launcher's pre-spawned vision / embedding / reranker on
        :8089–8091). PIDs whose listening port is in that set are
        skipped even if not in ``self.processes``.

        Returns the list of PIDs killed.
        """
        preserve_ports = set(preserve_ports or set())
        # Always preserve the launcher's pre-spawned support tiers
        # (vision 8089, embedding 8090, reranker 8091). They're spawned
        # by LocalAIStack.ps1 before the backend starts and only get
        # adopted into self.processes lazily on first request, so a
        # pre-spawn reap that runs before adoption would kill them and
        # silently break embeddings + retrieval.
        preserve_ports.update({8089, 8090, 8091})
        # Tracked PIDs from our own subprocess.Popen handles.
        tracked: set[int] = set()
        for proc in self.processes.values():
            if proc.popen is not None and proc.popen.poll() is None:
                tracked.add(proc.popen.pid)
            # Externally-managed (we adopted on startup) — preserve by port.
            if proc._externally_managed and proc.port:
                preserve_ports.add(proc.port)

        candidates = await _list_llama_server_pids()
        killed: list[int] = []
        for pid, port in candidates:
            if pid in tracked:
                continue
            if port and port in preserve_ports:
                continue
            try:
                if os.name == "nt":
                    # Use taskkill for clean exit; fall back to os.kill.
                    proc = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/PID", str(pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    await proc.wait()
                else:
                    os.kill(pid, signal.SIGTERM)
                killed.append(pid)
                logger.warning(
                    "Killed orphan llama-server pid=%s port=%s "
                    "(left from previous backend run)", pid, port,
                )
            except OSError as exc:
                logger.debug("Failed to kill orphan pid=%s: %s", pid, exc)
        return killed

    async def _port_is_serving(self, endpoint: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                for path in ("/health", "/models"):
                    r = await client.get(endpoint.rstrip("/") + path)
                    if r.status_code == 200:
                        return True
        except httpx.HTTPError:
            pass
        return False

    # ── Chat ────────────────────────────────────────────────────────────

    async def chat_stream(
        self,
        tier: TierConfig,
        messages: list[ChatMessage] | list[dict],
        think: bool,
        extra_options: dict[str, Any] | None = None,
        tools: list[dict] | None = None,
        keep_alive: Any = None,           # accepted for API parity; ignored
    ) -> AsyncIterator[dict]:
        """Yields OpenAI-shaped streaming events.

        Each event has the shape:
          {choices: [{delta: {content?, tool_calls?}, finish_reason?}], ...}

        Tool-call deltas are NOT pre-aggregated — callers should feed them
        through ``ToolCallAccumulator`` if they need full calls.
        """
        params = tier.params or {}
        chat_template_kwargs = dict(tier.chat_template_kwargs or {})
        if tier.think_supported:
            chat_template_kwargs["enable_thinking"] = think

        payload: dict[str, Any] = {
            "model": tier.model_tag,
            "messages": _messages_to_payload(messages),
            "stream": True,
            "temperature": params.get("temperature"),
            "top_p": params.get("top_p"),
            "top_k": params.get("top_k"),
            "max_tokens": params.get("num_predict"),
            "chat_template_kwargs": chat_template_kwargs,
        }
        if tools:
            payload["tools"] = tools
        if extra_options:
            payload.update(extra_options)
        payload = {k: v for k, v in payload.items() if v is not None}

        endpoint = tier.resolved_endpoint()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", f"{endpoint}/chat/completions", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue

    async def chat_once(
        self,
        tier: TierConfig,
        messages: list[ChatMessage] | list[dict],
        think: bool,
        extra_options: dict[str, Any] | None = None,
        keep_alive: Any = None,
    ) -> str:
        chunks: list[str] = []
        async for ev in self.chat_stream(tier, messages, think, extra_options):
            for choice in ev.get("choices", []):
                delta = choice.get("delta") or {}
                if "content" in delta and delta["content"]:
                    chunks.append(delta["content"])
        return "".join(chunks)

    # ── Embeddings ─────────────────────────────────────────────────────

    async def embed(self, tier: TierConfig, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        endpoint = tier.resolved_endpoint()
        payload = {"model": tier.model_tag, "input": texts}
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as c:
            r = await c.post(f"{endpoint}/embeddings", json=payload)
            r.raise_for_status()
            data = r.json()
        return [row.get("embedding") for row in (data.get("data") or [])]

    # ── Health ─────────────────────────────────────────────────────────

    async def is_ready(self, tier: TierConfig) -> bool:
        try:
            return await self._port_is_serving(tier.resolved_endpoint())
        except Exception:
            return False


async def _list_llama_server_pids() -> list[tuple[int, int | None]]:
    """Return [(pid, listening_port_or_None)] for every live llama-server
    process. Best-effort + cross-platform. Used by
    ``LlamaCppClient.kill_orphans`` and the ``/admin/vram/probe`` diagnostic.
    """
    pids: list[int] = []
    if os.name == "nt":
        # tasklist is faster + dependency-free on Windows.
        try:
            proc = await asyncio.create_subprocess_exec(
                "tasklist", "/FI", "IMAGENAME eq llama-server.exe",
                "/FO", "CSV", "/NH",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            stdout, _ = await proc.communicate()
        except (OSError, FileNotFoundError):
            return []
        for line in stdout.decode("utf-8", errors="ignore").splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-x", "llama-server",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
        except (OSError, FileNotFoundError):
            return []
        for line in stdout.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))

    if not pids:
        return []

    # Resolve listening ports per pid (Windows-only — POSIX path
    # returns ports as None and falls back to PID-only matching, which
    # is fine because preserve_ports only matters for the launcher's
    # adopted services).
    if os.name != "nt":
        return [(pid, None) for pid in pids]

    pid_to_port: dict[int, int] = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "netstat", "-ano", "-p", "TCP",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode("utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) < 5 or "LISTENING" not in parts:
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid not in pids or pid in pid_to_port:
                continue
            local = parts[1]
            try:
                port = int(local.rsplit(":", 1)[-1])
            except ValueError:
                continue
            pid_to_port[pid] = port
    except (OSError, FileNotFoundError):
        pass

    return [(pid, pid_to_port.get(pid)) for pid in pids]
