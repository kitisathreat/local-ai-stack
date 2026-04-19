"""Unit tests for backend/rag.py — text extraction, chunking, and the
Qdrant client layer (mocked via monkey-patched httpx calls)."""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run(coro):
    return asyncio.run(coro)


# ── Text extraction ────────────────────────────────────────────────────

def test_extract_text_plain():
    from backend.rag import extract_text
    assert extract_text("notes.txt", b"Hello world").strip() == "Hello world"


def test_extract_text_markdown():
    from backend.rag import extract_text
    assert "#" in extract_text("a.md", b"# Heading\n\nBody text")


def test_extract_text_html_strips_tags():
    from backend.rag import extract_text
    html = b"<html><body><script>x=1</script><p>Hello <b>world</b>.</p></body></html>"
    out = extract_text("page.html", html)
    assert "Hello" in out
    assert "<script" not in out and "x=1" not in out


# ── Chunking ────────────────────────────────────────────────────────────

def test_chunk_empty_returns_empty():
    from backend.rag import chunk_text
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_chunk_small_fits_single():
    from backend.rag import chunk_text
    out = chunk_text("Short text.", size=500)
    assert len(out) == 1


def test_chunk_splits_long_text():
    from backend.rag import chunk_text
    # Generate ~5 sentences of ~200 chars each
    sents = [f"This is sentence number {i}, padded with some filler words to hit length. " * 3
             for i in range(10)]
    text = " ".join(s.strip() for s in sents)
    chunks = chunk_text(text, size=400, overlap=50)
    assert len(chunks) > 1
    # Each chunk should be roughly within the size bound
    for c in chunks:
        assert len(c) < 800   # allow some slop because we split on sentences


# ── Collection naming ─────────────────────────────────────────────────

def test_collection_names_are_user_scoped():
    from backend.rag import collection_name, memory_collection_name
    assert collection_name(42) == "user_42_docs"
    assert memory_collection_name(42) == "user_42_memory"
    # Different users get different collections
    assert collection_name(1) != collection_name(2)


# ── format_context_block ───────────────────────────────────────────────

def test_format_context_block_empty():
    from backend.rag import format_context_block
    assert format_context_block([]) == ""


def test_format_context_block_renders_hits():
    from backend.rag import format_context_block
    hits = [
        {"filename": "readme.md", "chunk_index": 0, "text": "Install: `pip install foo`", "score": 0.9},
        {"filename": "readme.md", "chunk_index": 1, "text": "Usage: `foo --help`", "score": 0.8},
    ]
    out = format_context_block(hits)
    assert "Knowledge base" in out
    assert "readme.md" in out
    assert "pip install foo" in out
    assert "foo --help" in out
