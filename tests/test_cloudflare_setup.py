"""
Cloudflare setup tests — CI area C.

Unit tests for gui/cloudflare_setup.py with the subprocess layer mocked.
Runs on Linux CI (no cloudflared binary or Cloudflare credentials required).
All subprocess calls are intercepted; we only test the logic layer.
"""

import os
import sys
import pathlib
import textwrap
import pytest


def _module_available():
    try:
        import gui.cloudflare_setup  # noqa: F401
        return True
    except Exception:
        return False


skip_if_unavailable = pytest.mark.skipif(
    not _module_available(),
    reason="gui.cloudflare_setup not yet implemented (pre-Phase-3)"
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_cloudflared_dir(tmp_path, monkeypatch):
    """Point cloudflare_setup at a temp directory instead of %USERPROFILE%."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cf_dir = tmp_path / ".cloudflared"
    cf_dir.mkdir()
    return cf_dir


# ---------------------------------------------------------------------------
# _needs_login
# ---------------------------------------------------------------------------

@skip_if_unavailable
def test_needs_login_true_when_cert_missing(tmp_cloudflared_dir):
    from gui.cloudflare_setup import _needs_login
    assert _needs_login() is True


@skip_if_unavailable
def test_needs_login_false_when_cert_fresh(tmp_cloudflared_dir):
    import time
    from gui.cloudflare_setup import _needs_login

    cert = tmp_cloudflared_dir / "cert.pem"
    cert.write_text("FAKE CERT")
    # mtime = now → well within 90-day window
    assert _needs_login() is False


@skip_if_unavailable
def test_needs_login_true_when_cert_expired(tmp_cloudflared_dir, monkeypatch):
    import time
    from gui.cloudflare_setup import _needs_login, CERT_MAX_AGE_DAYS

    cert = tmp_cloudflared_dir / "cert.pem"
    cert.write_text("OLD CERT")
    # backdate mtime beyond the expiry threshold
    old_mtime = time.time() - (CERT_MAX_AGE_DAYS + 1) * 86400
    os.utime(cert, (old_mtime, old_mtime))
    assert _needs_login() is True


# ---------------------------------------------------------------------------
# _parse_tunnel_uuid
# ---------------------------------------------------------------------------

@skip_if_unavailable
def test_parse_tunnel_uuid_extracts_uuid_from_stdout():
    from gui.cloudflare_setup import _parse_tunnel_uuid

    sample_output = textwrap.dedent("""\
        Created tunnel local-ai-stack with id 550e8400-e29b-41d4-a716-446655440000
        Credentials written to /Users/x/.cloudflared/550e8400-e29b-41d4-a716-446655440000.json
    """)
    uuid = _parse_tunnel_uuid(sample_output)
    assert uuid == "550e8400-e29b-41d4-a716-446655440000"


@skip_if_unavailable
def test_parse_tunnel_uuid_raises_on_bad_output():
    from gui.cloudflare_setup import _parse_tunnel_uuid, CloudflareSetupError

    with pytest.raises(CloudflareSetupError):
        _parse_tunnel_uuid("something went wrong, no UUID here")


# ---------------------------------------------------------------------------
# _write_config_yml — ingress order
# ---------------------------------------------------------------------------

@skip_if_unavailable
def test_write_config_yml_ingress_order(tmp_cloudflared_dir):
    """
    The chat hostname must appear BEFORE the http_status:404 fallback.
    cloudflared evaluates ingress rules top-to-bottom; a wildcard catch-all
    before the chat rule would intercept all traffic.
    """
    import yaml
    from gui.cloudflare_setup import _write_config_yml

    _write_config_yml(
        tunnel_id="550e8400-e29b-41d4-a716-446655440000",
        hostname="chat.example.com",
        backend_url="http://localhost:18000",
        cloudflared_dir=tmp_cloudflared_dir,
    )

    config_path = tmp_cloudflared_dir / "config.yml"
    assert config_path.exists(), "config.yml was not written"

    config = yaml.safe_load(config_path.read_text())
    ingress = config.get("ingress", [])
    assert len(ingress) >= 2, f"Expected at least 2 ingress rules, got {ingress}"

    # First rule must be the chat hostname
    assert ingress[0].get("hostname") == "chat.example.com", (
        f"First ingress rule is not the chat hostname: {ingress[0]}"
    )
    # Last rule must be the 404 fallback (no hostname key)
    last = ingress[-1]
    assert "hostname" not in last, (
        f"Last ingress rule should be the fallback (no hostname), got: {last}"
    )
    assert "404" in str(last.get("service", "")), (
        f"Last ingress rule should be http_status:404, got: {last}"
    )


# ---------------------------------------------------------------------------
# _find_existing_tunnel (adoption path)
# ---------------------------------------------------------------------------

@skip_if_unavailable
def test_find_existing_tunnel_reads_config(tmp_cloudflared_dir):
    import yaml
    from gui.cloudflare_setup import _find_existing_tunnel

    existing_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    config = {
        "tunnel": existing_id,
        "credentials-file": str(tmp_cloudflared_dir / f"{existing_id}.json"),
        "ingress": [
            {"hostname": "chat.example.com", "service": "http://localhost:18000"},
            {"service": "http_status:404"},
        ],
    }
    (tmp_cloudflared_dir / "config.yml").write_text(yaml.dump(config))

    result = _find_existing_tunnel(cloudflared_dir=tmp_cloudflared_dir)
    assert result is not None
    assert result["tunnel_id"] == existing_id


@skip_if_unavailable
def test_find_existing_tunnel_returns_none_when_absent(tmp_cloudflared_dir):
    from gui.cloudflare_setup import _find_existing_tunnel

    result = _find_existing_tunnel(cloudflared_dir=tmp_cloudflared_dir)
    assert result is None


# ---------------------------------------------------------------------------
# Subprocess error propagation
# ---------------------------------------------------------------------------

@skip_if_unavailable
def test_cloudflared_nonzero_exit_raises(monkeypatch, tmp_cloudflared_dir):
    """When cloudflared exits non-zero, _run_cloudflared must raise CloudflareSetupError."""
    import subprocess
    from gui.cloudflare_setup import _run_cloudflared, CloudflareSetupError

    def _fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1,
            stdout="", stderr="Error: authentication failed"
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # Pass a fake cloudflared path so find_cloudflared() is not called
    fake_cf = pathlib.Path("cloudflared")
    with pytest.raises(CloudflareSetupError) as exc_info:
        _run_cloudflared(["tunnel", "create", "test"], cloudflared=fake_cf)

    assert "authentication failed" in str(exc_info.value).lower() or \
           exc_info.value.stderr, "CloudflareSetupError must carry the stderr"
