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
PIPELINES_DIR = ROOT / "pipelines"

TOOL_FILES = sorted(TOOLS_DIR.glob("*.py"))
PIPELINE_FILES = sorted(PIPELINES_DIR.glob("*.py"))

# ── Thresholds ────────────────────────────────────────────────────────────────

MIN_TOOL_COUNT = 52
MIN_PIPELINE_COUNT = 4

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


def test_pipeline_count():
    """Must have at least MIN_PIPELINE_COUNT pipeline files."""
    assert len(PIPELINE_FILES) >= MIN_PIPELINE_COUNT, (
        f"Expected {MIN_PIPELINE_COUNT}+ pipeline files, found {len(PIPELINE_FILES)}"
    )


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


# ── Pipeline file tests ───────────────────────────────────────────────────────

@pytest.mark.parametrize("path", PIPELINE_FILES, ids=lambda p: p.name)
def test_pipeline_syntax(path):
    """Pipeline file must have valid Python syntax."""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
        tmp.write(path.read_bytes())
        tmp_path = tmp.name
    try:
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as e:
        pytest.fail(f"{path.name}: syntax error — {e}")


@pytest.mark.parametrize("path", PIPELINE_FILES, ids=lambda p: p.name)
def test_pipeline_has_module_docstring(path):
    """Pipeline file must have a module-level docstring."""
    tree = _parse(path)
    docstring = ast.get_docstring(tree)
    assert docstring, f"{path.name}: missing module docstring"


@pytest.mark.parametrize("path", PIPELINE_FILES, ids=lambda p: p.name)
def test_pipeline_docstring_metadata(path):
    """Pipeline docstring must contain title: and version:."""
    source = path.read_text(encoding="utf-8")
    for field in PIPELINE_DOCSTRING_REQUIRED:
        assert field in source, f"{path.name}: docstring missing '{field}'"


@pytest.mark.parametrize("path", PIPELINE_FILES, ids=lambda p: p.name)
def test_pipeline_has_correct_class(path):
    """Pipeline file must define a top-level 'Filter' or 'Pipeline' class."""
    tree = _parse(path)
    top_classes = _top_class_names(tree)
    assert "Filter" in top_classes or "Pipeline" in top_classes, (
        f"{path.name}: must define a top-level 'class Filter' or 'class Pipeline'"
    )


@pytest.mark.parametrize("path", PIPELINE_FILES, ids=lambda p: p.name)
def test_pipeline_has_valves(path):
    """Filter/Pipeline class must contain a nested 'Valves' class."""
    tree = _parse(path)
    top_classes = _top_class_names(tree)
    outer = "Filter" if "Filter" in top_classes else "Pipeline"
    assert _has_nested_class(tree, outer, "Valves"), (
        f"{path.name}: '{outer}' class must contain a nested 'class Valves(BaseModel)'"
    )


@pytest.mark.parametrize("path", PIPELINE_FILES, ids=lambda p: p.name)
def test_pipeline_filter_has_inlet_outlet(path):
    """Filter pipelines must implement both inlet() and outlet() methods."""
    tree = _parse(path)
    top_classes = _top_class_names(tree)
    if "Filter" not in top_classes:
        pytest.skip("Not a filter pipeline")

    source = path.read_text(encoding="utf-8")
    assert "def inlet" in source, f"{path.name}: Filter missing inlet() method"
    assert "def outlet" in source, f"{path.name}: Filter missing outlet() method"


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

REQUIRED_PIPELINES = [
    "rate_limiter", "context_injector", "web_search_rag", "clarification_filter",
]


@pytest.mark.parametrize("tool_name", REQUIRED_TOOLS)
def test_required_tool_exists(tool_name):
    """All expected tool files must exist."""
    assert (TOOLS_DIR / f"{tool_name}.py").exists(), (
        f"Required tool file missing: tools/{tool_name}.py"
    )


@pytest.mark.parametrize("pipeline_name", REQUIRED_PIPELINES)
def test_required_pipeline_exists(pipeline_name):
    """All expected pipeline files must exist."""
    assert (PIPELINES_DIR / f"{pipeline_name}.py").exists(), (
        f"Required pipeline file missing: pipelines/{pipeline_name}.py"
    )
