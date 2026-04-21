"""
tests/test_docker_setup.py

Automated Docker-setup verification and application smoke tests.
No manual launch required — all sections run unattended in CI.

Test sections:
  1. Dockerfile structural tests  — static, no Docker runtime needed
  2. Compose consistency tests    — static; port conflicts, volume refs, dependency graph
  3. Docker CLI validation        — skipped if `docker` is not on PATH
  4. Backend smoke tests          — spawns a uvicorn process, hits HTTP endpoints, tears down
  5. Docker-compose integration   — full stack; opt-in via DOCKER_INTEGRATION_TESTS=1
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import importlib.util

import pytest
import yaml

ROOT = Path(__file__).parent.parent

DOCKER_AVAILABLE = shutil.which("docker") is not None
DOCKER_INTEGRATION_TESTS = os.getenv("DOCKER_INTEGRATION_TESTS") == "1"

# Backend smoke tests (section 4) spin up a real uvicorn process.
# They're skipped when uvicorn isn't importable (e.g. a minimal test env).
_UVICORN_AVAILABLE = importlib.util.find_spec("uvicorn") is not None

def _backend_env_ok() -> bool:
    """Subprocess check that backend.main (including jose + cryptography) is importable."""
    if not _UVICORN_AVAILABLE:
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import backend.main"],
            cwd=ROOT,
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False

_BACKEND_LAUNCHABLE = _backend_env_ok()


# ── shared helpers ────────────────────────────────────────────────────────────

def _load_compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def _free_port() -> int:
    """Ask the OS for an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _good_secret() -> str:
    """Valid 32-byte URL-safe base64 secret accepted by backend auth."""
    return base64.urlsafe_b64encode(b"a" * 32).decode()


def _http_get(url: str, timeout: float = 10.0) -> tuple[int, dict | str]:
    """Return (status_code, parsed_json_or_raw_text). Raises on connection error."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode()
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body


def _wait_for_http(url: str, timeout: float = 30.0, interval: float = 0.5) -> bool:
    """Poll url until it returns HTTP 200 or timeout expires. Returns True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Dockerfile structural tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestDockerfileStructure:
    """Static inspection of Dockerfiles — no Docker runtime required."""

    def test_backend_dockerfile_exists(self):
        assert (ROOT / "backend" / "Dockerfile").exists()

    def test_frontend_dockerfile_exists(self):
        assert (ROOT / "frontend" / "Dockerfile").exists()

    def test_backend_dockerfile_has_healthcheck(self):
        text = (ROOT / "backend" / "Dockerfile").read_text()
        assert "HEALTHCHECK" in text, "backend Dockerfile must define a HEALTHCHECK"

    def test_backend_dockerfile_healthcheck_uses_healthz(self):
        text = (ROOT / "backend" / "Dockerfile").read_text()
        assert "/healthz" in text, "HEALTHCHECK must probe /healthz"

    def test_backend_dockerfile_exposes_8000(self):
        text = (ROOT / "backend" / "Dockerfile").read_text()
        assert "EXPOSE 8000" in text

    def test_backend_dockerfile_copies_requirements_before_source(self):
        """Layer-cache optimisation: requirements.txt must be copied and installed
        before the source tree so rebuilds caused by code changes reuse the
        pip layer."""
        text = (ROOT / "backend" / "Dockerfile").read_text()
        req_pos = text.find("requirements.txt")
        src_pos = text.find("COPY backend /app/backend")
        assert req_pos != -1, "requirements.txt not referenced in backend Dockerfile"
        assert src_pos != -1, "COPY backend /app/backend not found in Dockerfile"
        assert req_pos < src_pos, (
            "requirements.txt should be copied before source for Docker layer caching"
        )

    def test_backend_dockerfile_uses_python_312(self):
        text = (ROOT / "backend" / "Dockerfile").read_text()
        assert "python:3.12" in text.lower()

    def test_frontend_dockerfile_is_multistage(self):
        """Must use a multi-stage build: builder image → slim nginx runtime."""
        text = (ROOT / "frontend" / "Dockerfile").read_text()
        from_count = sum(
            1 for line in text.splitlines()
            if line.strip().upper().startswith("FROM")
        )
        assert from_count >= 2, "Frontend Dockerfile must use a multi-stage build"

    def test_frontend_dockerfile_uses_nginx(self):
        text = (ROOT / "frontend" / "Dockerfile").read_text()
        assert "nginx" in text.lower()

    def test_nginx_conf_exists(self):
        assert (ROOT / "frontend" / "nginx.conf").exists()

    def test_nginx_conf_proxies_api_to_backend(self):
        text = (ROOT / "frontend" / "nginx.conf").read_text()
        assert "backend:8000" in text, "nginx.conf must reverse-proxy to backend:8000"

    def test_nginx_conf_enables_sse_streaming(self):
        """SSE token streaming requires proxy_buffering off."""
        text = (ROOT / "frontend" / "nginx.conf").read_text()
        assert "proxy_buffering off" in text


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Compose consistency tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestComposeConsistency:
    """Internal consistency of docker-compose.yml beyond what test_config.py covers."""

    def test_no_host_port_conflicts(self):
        """No two services may bind the same host-side port."""
        data = _load_compose()
        seen: dict[str, str] = {}
        for svc_name, svc in data["services"].items():
            for mapping in svc.get("ports", []):
                host_port = str(mapping).split(":")[0]
                assert host_port not in seen, (
                    f"Host port {host_port} bound by both "
                    f"'{seen[host_port]}' and '{svc_name}'"
                )
                seen[host_port] = svc_name

    def test_all_named_volumes_declared(self):
        """Every named volume used by a service must appear in the top-level volumes: block."""
        data = _load_compose()
        declared = set(data.get("volumes", {}).keys())
        for svc_name, svc in data["services"].items():
            for vol in svc.get("volumes", []):
                vol_str = str(vol)
                # Bind mounts start with "." or "/"; named volumes do not.
                if ":" in vol_str and not vol_str.startswith((".", "/")):
                    name = vol_str.split(":")[0]
                    assert name in declared, (
                        f"Service '{svc_name}' uses undeclared volume '{name}'"
                    )

    def test_depends_on_services_all_exist(self):
        """Every service listed in depends_on must itself be a defined service."""
        data = _load_compose()
        defined = set(data["services"].keys())
        for svc_name, svc in data["services"].items():
            deps = svc.get("depends_on", {})
            dep_names = list(deps.keys()) if isinstance(deps, dict) else list(deps)
            for dep in dep_names:
                assert dep in defined, (
                    f"Service '{svc_name}' depends_on '{dep}' which is not defined"
                )

    def test_backend_depends_on_redis(self):
        data = _load_compose()
        backend = data["services"]["backend"]
        deps = backend.get("depends_on", {})
        dep_names = list(deps.keys()) if isinstance(deps, dict) else list(deps)
        assert "redis" in dep_names, "backend must list redis in depends_on"

    def test_backend_waits_for_redis_healthy(self):
        """depends_on condition must be service_healthy so Redis is ready before backend starts."""
        data = _load_compose()
        deps = data["services"]["backend"].get("depends_on", {})
        if isinstance(deps, dict) and "redis" in deps:
            assert deps["redis"].get("condition") == "service_healthy", (
                "backend depends_on redis should use condition: service_healthy"
            )

    def test_redis_has_healthcheck(self):
        """Redis healthcheck is required for the depends_on healthy condition."""
        data = _load_compose()
        assert "healthcheck" in data["services"]["redis"]

    def test_cloudflared_is_profile_gated(self):
        """cloudflared must be behind the 'public' profile so local users aren't forced
        to configure a Cloudflare Tunnel token."""
        data = _load_compose()
        profiles = data["services"].get("cloudflared", {}).get("profiles", [])
        assert "public" in profiles, "cloudflared must require --profile public"

    def test_backend_mounts_config_readonly(self):
        """Config directory is runtime-only; mounting it :ro prevents accidental writes."""
        data = _load_compose()
        volumes = data["services"]["backend"].get("volumes", [])
        config_vol = next((v for v in volumes if "config" in str(v)), None)
        assert config_vol is not None, "backend must mount ./config"
        assert ":ro" in str(config_vol), "config mount must be read-only (:ro)"

    def test_all_build_dockerfiles_exist(self):
        """Every service that specifies build.dockerfile must point to an existing file."""
        data = _load_compose()
        for svc_name, svc in data["services"].items():
            build = svc.get("build")
            if not isinstance(build, dict):
                continue
            dockerfile = build.get("dockerfile")
            if dockerfile:
                path = ROOT / dockerfile
                assert path.exists(), (
                    f"Service '{svc_name}' build.dockerfile '{dockerfile}' not found"
                )

    def test_services_have_restart_policy(self):
        """All non-profile-gated services should define restart: to survive crashes."""
        data = _load_compose()
        for svc_name, svc in data["services"].items():
            if svc.get("profiles"):
                continue  # profile-gated services are opt-in; skip
            assert "restart" in svc, f"Service '{svc_name}' has no restart policy"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Docker CLI validation
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker not on PATH")
class TestDockerCLI:
    """Validate the compose file against the Docker engine (skipped when Docker absent)."""

    # The cloudflared service uses ${CLOUDFLARE_TUNNEL_TOKEN:?...} (required interpolation),
    # so docker compose config fails unless the variable is set. Supply a dummy value so
    # the validator can inspect the full spec without a real Cloudflare account.
    _CLI_ENV = {
        **os.environ,
        "AUTH_SECRET_KEY": _good_secret(),
        "CLOUDFLARE_TUNNEL_TOKEN": "dummy-token-for-config-validation",
    }

    def test_compose_config_validates(self):
        """`docker compose config` must exit 0, confirming the full spec is valid."""
        result = subprocess.run(
            ["docker", "compose", "config", "--quiet"],
            cwd=ROOT,
            env=self._CLI_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"docker compose config failed:\n{result.stderr}"
        )

    def test_compose_lists_expected_services(self):
        """`docker compose config --services` must include all core services."""
        result = subprocess.run(
            ["docker", "compose", "--profile", "public", "config", "--services"],
            cwd=ROOT,
            env=self._CLI_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        listed = set(result.stdout.strip().splitlines())
        for svc in ("backend", "frontend", "redis", "ollama", "qdrant", "searxng"):
            assert svc in listed, f"Service '{svc}' not listed by docker compose config --services"

    def test_compose_has_no_unknown_extensions(self):
        """`docker compose config` output must not contain unrecognised extension keys.
        A zero exit-code is the only reliable signal from Docker's validator."""
        result = subprocess.run(
            ["docker", "compose", "config"],
            cwd=ROOT,
            env=self._CLI_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"docker compose config reported errors:\n{result.stderr}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Backend application smoke tests (subprocess, no Docker needed)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def backend_server(tmp_path_factory):
    """
    Start a live uvicorn process for the backend and yield its base URL.

    Environment is configured so that:
    - Auth secrets are set to valid values
    - DB is redirected to a temp file
    - Config and tools dirs point at the real source tree
    - External services (Ollama, Qdrant, Redis, SearXNG) point at non-listening ports,
      triggering graceful WARN-level diagnostics rather than a crash
    - Redis is disabled (empty REDIS_URL) so the rate limiter runs in-memory

    Skipped automatically when uvicorn is not importable or the backend module
    tree cannot be imported (e.g. missing cryptography native extensions).

    The process is torn down after all tests in the module complete.
    """
    if not _BACKEND_LAUNCHABLE:
        pytest.skip(
            "Backend smoke tests require uvicorn + all backend deps. "
            "Install: pip install uvicorn[standard] python-jose[cryptography] "
            "aiosmtplib aiosqlite cryptography redis[hiredis] pypdf python-multipart"
        )
    tmp = tmp_path_factory.mktemp("smoke_db")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "AUTH_SECRET_KEY": _good_secret(),
        "HISTORY_SECRET_KEY": _good_secret(),
        "LAI_DB_PATH": str(tmp / "smoke.db"),
        "LAI_CONFIG_DIR": str(ROOT / "config"),
        "LAI_TOOLS_DIR": str(ROOT / "tools"),
        # Non-listening ports so startup diagnostics warn, not crash
        "OLLAMA_URL": "http://127.0.0.1:19999",
        "LLAMACPP_URL": "http://127.0.0.1:19998/v1",
        "QDRANT_URL": "http://127.0.0.1:19997",
        "SEARXNG_URL": "http://127.0.0.1:19996",
        # Disable Redis; rate limiter falls back to in-memory counters
        "REDIS_URL": "",
        "LOG_LEVEL": "WARNING",
        "PUBLIC_BASE_URL": base_url,
    }

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "backend.main:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--workers", "1",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    ready = _wait_for_http(f"{base_url}/healthz", timeout=40)
    if not ready:
        proc.terminate()
        stdout = proc.stdout.read().decode(errors="replace")
        stderr = proc.stderr.read().decode(errors="replace")
        pytest.fail(
            f"Backend did not become healthy within 40 s on port {port}.\n"
            f"stdout:\n{stdout[-2000:]}\n"
            f"stderr:\n{stderr[-2000:]}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


class TestBackendSmoke:
    """HTTP smoke tests against a live uvicorn process (no Docker required)."""

    # ── /healthz ─────────────────────────────────────────────────────────────

    def test_healthz_status_200(self, backend_server):
        status, _ = _http_get(f"{backend_server}/healthz")
        assert status == 200

    def test_healthz_body_is_ok_true(self, backend_server):
        _, body = _http_get(f"{backend_server}/healthz")
        assert body == {"ok": True}

    # ── /v1/models ────────────────────────────────────────────────────────────

    def test_models_status_200(self, backend_server):
        status, _ = _http_get(f"{backend_server}/v1/models")
        assert status == 200

    def test_models_lists_all_five_tiers(self, backend_server):
        _, body = _http_get(f"{backend_server}/v1/models")
        ids = {m["id"] for m in body.get("data", [])}
        expected = {
            "tier.highest_quality",
            "tier.versatile",
            "tier.fast",
            "tier.coding",
            "tier.vision",
        }
        assert expected == ids, f"Unexpected tier IDs: {ids}"

    def test_models_entries_have_required_fields(self, backend_server):
        _, body = _http_get(f"{backend_server}/v1/models")
        required = {"id", "name", "backend", "context_window"}
        for entry in body.get("data", []):
            missing = required - entry.keys()
            assert not missing, f"Model entry missing fields {missing}: {entry}"

    def test_models_tiers_reference_known_backends(self, backend_server):
        _, body = _http_get(f"{backend_server}/v1/models")
        known_backends = {"ollama", "llama_cpp"}
        for entry in body.get("data", []):
            assert entry["backend"] in known_backends, (
                f"Tier '{entry['id']}' has unknown backend '{entry['backend']}'"
            )

    # ── /api/system ───────────────────────────────────────────────────────────

    def test_system_status_200(self, backend_server):
        status, _ = _http_get(f"{backend_server}/api/system")
        assert status == 200

    def test_system_has_vram_and_ram_keys(self, backend_server):
        _, body = _http_get(f"{backend_server}/api/system")
        assert "vram" in body
        assert "ram" in body
        assert "ts" in body

    def test_system_ram_fields_are_numeric(self, backend_server):
        _, body = _http_get(f"{backend_server}/api/system")
        for field in ("total_gb", "used_gb", "free_gb"):
            assert isinstance(body["ram"][field], (int, float)), (
                f"ram.{field} should be numeric, got {body['ram'][field]!r}"
            )

    # ── /api/tools ────────────────────────────────────────────────────────────

    def test_tools_status_200(self, backend_server):
        status, _ = _http_get(f"{backend_server}/api/tools")
        assert status == 200

    def test_tools_list_is_nonempty(self, backend_server):
        _, body = _http_get(f"{backend_server}/api/tools")
        assert len(body.get("data", [])) > 0, "Tool registry returned no tools"

    # ── /api/vram ─────────────────────────────────────────────────────────────

    def test_vram_status_200(self, backend_server):
        status, _ = _http_get(f"{backend_server}/api/vram")
        assert status == 200

    # ── security headers ──────────────────────────────────────────────────────

    def test_security_headers_present(self, backend_server):
        with urllib.request.urlopen(
            f"{backend_server}/healthz", timeout=10
        ) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
        assert "x-content-type-options" in headers, "X-Content-Type-Options header missing"
        assert "x-frame-options" in headers, "X-Frame-Options header missing"
        assert headers["x-frame-options"].upper() == "DENY"
        assert "referrer-policy" in headers, "Referrer-Policy header missing"

    # ── 404 for unknown routes ────────────────────────────────────────────────

    def test_unknown_route_returns_404(self, backend_server):
        try:
            urllib.request.urlopen(
                f"{backend_server}/nonexistent-endpoint-xyz", timeout=10
            )
            pytest.fail("Expected 404, got 200")
        except urllib.error.HTTPError as e:
            assert e.code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Docker-compose integration tests (opt-in, requires DOCKER_INTEGRATION_TESTS=1)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not DOCKER_INTEGRATION_TESTS,
    reason="set DOCKER_INTEGRATION_TESTS=1 to run full docker-compose integration tests",
)
@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker not on PATH")
class TestDockerComposeIntegration:
    """
    Bring up backend + redis via docker compose, run HTTP checks, then tear down.

    Only core, lightweight services are started (backend + redis). GPU-heavy
    services (ollama, llama-server) are intentionally omitted so the test
    completes without a GPU or model downloads.

    Enable with: DOCKER_INTEGRATION_TESTS=1 pytest tests/test_docker_setup.py
    """

    _SERVICES = ["backend", "redis"]
    _COMPOSE_TIMEOUT = 120  # seconds for `docker compose up --wait`

    @pytest.fixture(autouse=True, scope="class")
    def compose_stack(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("integration")
        env = {
            **os.environ,
            "AUTH_SECRET_KEY": _good_secret(),
            "HISTORY_SECRET_KEY": _good_secret(),
            "LAI_DB_PATH": str(tmp / "lai.db"),
            # Backend will WARN about missing upstream services — that's expected
            "OLLAMA_URL": "http://127.0.0.1:19999",
            "LLAMACPP_URL": "http://127.0.0.1:19998/v1",
            "LOG_LEVEL": "WARNING",
        }

        subprocess.run(
            [
                "docker", "compose",
                "up", "-d", "--build", "--wait",
                *self._SERVICES,
            ],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=self._COMPOSE_TIMEOUT,
        )
        yield env

        subprocess.run(
            ["docker", "compose", "down", "--remove-orphans"],
            cwd=ROOT,
            env=env,
            timeout=60,
            check=False,
        )

    def test_backend_healthz_via_docker(self):
        assert _wait_for_http("http://127.0.0.1:8000/healthz", timeout=30), (
            "Backend /healthz did not return 200 within 30 s after docker compose up"
        )
        status, body = _http_get("http://127.0.0.1:8000/healthz")
        assert status == 200
        assert body == {"ok": True}

    def test_backend_models_list_via_docker(self):
        _, body = _http_get("http://127.0.0.1:8000/v1/models")
        ids = {m["id"] for m in body.get("data", [])}
        assert "tier.versatile" in ids

    def test_redis_container_is_running(self):
        result = subprocess.run(
            ["docker", "compose", "ps", "--status", "running", "--services"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert "redis" in result.stdout, (
            f"Redis container not in running state. Output:\n{result.stdout}"
        )

    def test_backend_container_is_running(self):
        result = subprocess.run(
            ["docker", "compose", "ps", "--status", "running", "--services"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert "backend" in result.stdout, (
            f"Backend container not in running state. Output:\n{result.stdout}"
        )
