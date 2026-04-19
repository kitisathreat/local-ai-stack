"""
CI tests for tool and pipeline Python files.

Checks without running the files (no external packages needed):
- Valid Python syntax
- Module-level docstring with required metadata fields
- Correct class structure (Tools / Filter / Pipeline + Valves)
- No forbidden patterns (blocking calls, hardcoded secrets)
- File count thresholds
"""

import ast
import py_compile
import re
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TOOLS_DIR = ROOT / "tools"
# Phase 6: pipelines/ migrated to backend/middleware/.
MIDDLEWARE_DIR = ROOT / "backend" / "middleware"

TOOL_FILES = sorted(TOOLS_DIR.glob("*.py"))
MIDDLEWARE_FILES = sorted(
    p for p in MIDDLEWARE_DIR.glob("*.py") if p.name != "__init__.py"
)

# ── Thresholds ────────────────────────────────────────────────────────────────

MIN_TOOL_COUNT = 52
MIN_MIDDLEWARE_COUNT = 4

# ── Required docstring metadata fields ───────────────────────────────────────

TOOL_DOCSTRING_REQUIRED = ["title:", "version:", "required_open_webui_version:"]
PIPELINE_DOCSTRING_REQUIRED = ["title:", "version:"]

# ── Patterns that should not appear in tool/pipeline files ────────────────────

FORBIDDEN_PATTERNS = [
    # Synchronous blocking network calls (tools must be async)
    (r"\btime\.sleep\s*\(", "time.sleep() blocks the event loop — use asyncio.sleep()"),
    # Obvious hardcoded secret values (long alphanumeric strings assigned to key/token vars)
    (
        r'(?i)(api_key|apikey|secret_key|access_token)\s*=\s*["\'][A-Za-z0-9+/]{32,}["\']',
        "Possible hardcoded secret — use Valves instead",
    ),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _class_names(tree: ast.Module) -> list[str]:
    return [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


def _top_class_names(tree: ast.Module) -> list[str]:
    """Only top-level class names (not nested)."""
    return [node.name for node in tree.body if isinstance(node, ast.ClassDef)]


def _has_nested_class(tree: ast.Module, outer: str, inner: str) -> bool:
    """Check if class `outer` contains a nested class `inner`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == outer:
            nested = [n.name for n in ast.walk(node) if isinstance(n, ast.ClassDef)]
            return inner in nested
    return False


# ── Tool file counts ──────────────────────────────────────────────────────────

def test_tool_count():
    """Must have at least MIN_TOOL_COUNT tool files."""
    assert len(TOOL_FILES) >= MIN_TOOL_COUNT, (
        f"Expected {MIN_TOOL_COUNT}+ tool files, found {len(TOOL_FILES)}"
    )


# test_pipeline_count was removed in Phase 6 when pipelines/ was migrated
# to backend/middleware/. Replaced by test_middleware_count below.


# ── Tool file tests ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", TOOL_FILES, ids=lambda p: p.name)
def test_tool_syntax(path):
    """Tool file must have valid Python syntax."""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
        tmp.write(path.read_bytes())
        tmp_path = tmp.name
    try:
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as e:
        pytest.fail(f"{path.name}: syntax error — {e}")


@pytest.mark.parametrize("path", TOOL_FILES, ids=lambda p: p.name)
def test_tool_has_module_docstring(path):
    """Tool file must have a module-level docstring."""
    tree = _parse(path)
    docstring = ast.get_docstring(tree)
    assert docstring, f"{path.name}: missing module docstring"


@pytest.mark.parametrize("path", TOOL_FILES, ids=lambda p: p.name)
def test_tool_docstring_metadata(path):
    """Tool docstring must contain required Open WebUI metadata fields."""
    source = path.read_text(encoding="utf-8")
    for field in TOOL_DOCSTRING_REQUIRED:
        assert field in source, f"{path.name}: docstring missing '{field}'"


@pytest.mark.parametrize("path", TOOL_FILES, ids=lambda p: p.name)
def test_tool_has_tools_class(path):
    """Tool file must define a 'Tools' class at the top level."""
    tree = _parse(path)
    assert "Tools" in _top_class_names(tree), (
        f"{path.name}: missing top-level 'class Tools:'"
    )


@pytest.mark.parametrize("path", TOOL_FILES, ids=lambda p: p.name)
def test_tool_has_valves(path):
    """Tools class must contain a nested 'Valves' class (even if empty)."""
    tree = _parse(path)
    assert _has_nested_class(tree, "Tools", "Valves"), (
        f"{path.name}: 'class Tools' must contain a nested 'class Valves(BaseModel)'"
    )


@pytest.mark.parametrize("path", TOOL_FILES, ids=lambda p: p.name)
def test_tool_no_forbidden_patterns(path):
    """Tool files must not contain forbidden patterns."""
    source = path.read_text(encoding="utf-8")
    for pattern, message in FORBIDDEN_PATTERNS:
        matches = re.findall(pattern, source)
        assert not matches, f"{path.name}: {message} (matched: {matches[:2]})"


# ── Middleware tests (Phase 6: formerly pipelines/) ──────────────────────────

def test_middleware_count():
    """Expect at least MIN_MIDDLEWARE_COUNT .py files in backend/middleware/."""
    assert len(MIDDLEWARE_FILES) >= MIN_MIDDLEWARE_COUNT, (
        f"Expected {MIN_MIDDLEWARE_COUNT}+ middleware files, found {len(MIDDLEWARE_FILES)}"
    )


@pytest.mark.parametrize("path", MIDDLEWARE_FILES, ids=lambda p: p.name)
def test_middleware_syntax(path):
    """Middleware file must have valid Python syntax."""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
        tmp.write(path.read_bytes())
        tmp_path = tmp.name
    try:
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as e:
        pytest.fail(f"{path.name}: syntax error — {e}")


@pytest.mark.parametrize("path", MIDDLEWARE_FILES, ids=lambda p: p.name)
def test_middleware_has_module_docstring(path):
    """Middleware file must have a module-level docstring."""
    tree = _parse(path)
    docstring = ast.get_docstring(tree)
    assert docstring, f"{path.name}: missing module docstring"


# ── Specific tool presence tests ──────────────────────────────────────────────

REQUIRED_TOOLS = [
    # Base tools
    "web_search", "wikipedia", "arxiv_search", "url_reader",
    "calculator", "datetime_tool", "weather",
    # Academic
    "pubmed", "semantic_scholar", "crossref", "openalex", "zenodo",
    "dblp", "unpaywall", "nasa_ads",
    # Extended
    "finance", "clinicaltrials", "openfda", "pubchem", "open_library",
    "rss_reader", "hackernews", "dictionary", "dev_utils",
    "package_search", "network_tools", "n8n_trigger",
    # Phase 4
    "excel_tool", "fred", "yahoo_finance_extended", "sec_edgar",
    "forex", "financial_calculator", "world_bank", "technical_analysis",
    # Phase 5
    "nasa_apis", "alpha_vantage", "finnhub", "acled", "europeana",
    "noaa_climate", "materials_project", "simbad", "uniprot", "usgs", "ensembl",
    # Phase 6
    "chart_generator", "financial_model", "jupyter_tool", "ask_clarification",
    # Memory
    "memory_tool",
]

REQUIRED_MIDDLEWARE = [
    # Phase 6: ported from pipelines/ to backend/middleware/
    "rate_limit", "context", "web_search", "clarification",
]


@pytest.mark.parametrize("tool_name", REQUIRED_TOOLS)
def test_required_tool_exists(tool_name):
    """All expected tool files must exist."""
    assert (TOOLS_DIR / f"{tool_name}.py").exists(), (
        f"Required tool file missing: tools/{tool_name}.py"
    )


@pytest.mark.parametrize("middleware_name", REQUIRED_MIDDLEWARE)
def test_required_middleware_exists(middleware_name):
    """All expected middleware modules must exist in backend/middleware/."""
    assert (MIDDLEWARE_DIR / f"{middleware_name}.py").exists(), (
        f"Required middleware file missing: backend/middleware/{middleware_name}.py"
    )
