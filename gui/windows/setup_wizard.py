"""First-run setup wizard (PySide6 QWizard).

Spawned by LocalAIStack.ps1 when .env is missing or no admin user exists.
Pages:
  1  Welcome / prerequisite check
  2  Admin account (email + password)
  3  Secrets (auto-generated, no user input)
  4  Public access / Cloudflare Tunnel (skippable)
  5  SMTP (skippable)
  6  Models (skippable)
  7  Finish — writes .env, seeds admin user

No web browser is embedded. The Cloudflare step opens the *system* browser
exactly once (via cloudflared, unavoidable for OAuth); everything else is
native Qt.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QThread, Signal, QFileSystemWatcher, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]
_VENV_BACKEND = _REPO / "vendor" / "venv-backend" / "Scripts" / "python.exe"
_CF_DIR = pathlib.Path(os.environ.get("USERPROFILE", pathlib.Path.home())) / ".cloudflared"
_CERT_PEM = _CF_DIR / "cert.pem"


# ---------------------------------------------------------------------------
# Field keys (shared between pages via QWizard::field)
# ---------------------------------------------------------------------------
FIELD_EMAIL = "admin_email"
FIELD_PASSWORD = "admin_password"
FIELD_AUTH_KEY = "auth_secret_key"
FIELD_HISTORY_KEY = "history_secret_key"
FIELD_PUBLIC_ACCESS = "public_access"   # "local" or "tunnel"
FIELD_DOMAIN = "domain"
FIELD_CHAT_HOSTNAME = "chat_hostname"
FIELD_TUNNEL_UUID = "tunnel_uuid"
FIELD_TUNNEL_NAME = "tunnel_name"
FIELD_SMTP_ENABLED = "smtp_enabled"
FIELD_SMTP_HOST = "smtp_host"
FIELD_SMTP_PORT = "smtp_port"
FIELD_SMTP_USER = "smtp_user"
FIELD_SMTP_PASS = "smtp_pass"
FIELD_SMTP_FROM = "smtp_from"


# ---------------------------------------------------------------------------
# Wizard state persistence
# ---------------------------------------------------------------------------
#
# If the wizard crashes, is killed, or the user closes it before the
# Finish page commits the .env, we save what they've already typed to a
# JSON file and pre-populate fields on the next launch. Removed when
# Finish completes successfully.
#
# This includes the admin password — it's the most painful field to
# re-enter and the user explicitly asked for it to persist. The file
# lives at `data/.wizard_state.json` (or `%LOCALAPPDATA%\LocalAIStack\
# .wizard_state.json` in installed mode), is mode 0600 on POSIX, and is
# always under .gitignore (`data/*`).
#
# Field allow-list — only fields that are actually registered with the
# QWizard (so `wiz.field` succeeds). Auto-generated secrets are
# deliberately excluded (they regenerate on Page 3 each launch; saving
# them would defeat their freshness guarantee). Tunnel UUID / name are
# also excluded because they live as page instance attrs rather than
# registered fields, AND the new idempotent `create_tunnel` makes
# re-provisioning safe — restored UUID isn't needed.
_PERSISTED_FIELDS = (
    FIELD_EMAIL,
    FIELD_PASSWORD,
    FIELD_DOMAIN,
    FIELD_CHAT_HOSTNAME,
    FIELD_SMTP_ENABLED,
    FIELD_SMTP_HOST,
    FIELD_SMTP_PORT,
    FIELD_SMTP_USER,
    FIELD_SMTP_PASS,
    FIELD_SMTP_FROM,
)


def _wizard_state_path() -> pathlib.Path:
    if os.environ.get("LAI_INSTALLED") == "1":
        local_appdata = os.environ.get("LOCALAPPDATA")
        root = (
            pathlib.Path(local_appdata) / "LocalAIStack"
            if local_appdata else _REPO / "data"
        )
    else:
        root = _REPO / "data"
    return root / ".wizard_state.json"


def _save_wizard_state(wiz: "QWizard") -> None:
    """Snapshot every persisted field to disk. Called on every page change.

    Best-effort: any I/O failure is logged and swallowed — the wizard
    must keep running even if the disk is read-only.
    """
    state: dict[str, object] = {}
    for key in _PERSISTED_FIELDS:
        try:
            val = wiz.field(key)
        except Exception:
            continue
        # `wiz.field` returns the underlying widget value: str, int, bool…
        if val is None:
            continue
        # QSpinBox returns int; tunnel UUID can be empty string before
        # provisioning. Skip empty strings to keep the file clean.
        if isinstance(val, str) and not val:
            continue
        state[key] = val
    if not state:
        return
    path = _wizard_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.warning("Could not persist wizard state to %s: %s", path, exc)


def _load_wizard_state() -> dict[str, object]:
    """Read prior wizard state, returning {} if absent or corrupt."""
    path = _wizard_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not load wizard state from %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # Filter unknown keys defensively.
    return {k: v for k, v in data.items() if k in _PERSISTED_FIELDS}


def _clear_wizard_state() -> None:
    """Wipe the saved state. Called from Finish on success."""
    path = _wizard_state_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove wizard state %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Worker thread for long-running setup steps
# ---------------------------------------------------------------------------

_NO_WINDOW_FLAG = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
)


class _WorkerThread(QThread):
    line_emitted = Signal(str)
    finished = Signal(int, str)  # exit_code, last_line

    def __init__(self, cmd: list[str], cwd: str | None = None):
        super().__init__()
        self._cmd = cmd
        self._cwd = cwd or str(_REPO)

    def run(self) -> None:
        try:
            proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=self._cwd,
                creationflags=_NO_WINDOW_FLAG,
            )
            last = ""
            for line in proc.stdout:
                line = line.rstrip()
                last = line
                self.line_emitted.emit(line)
            proc.wait()
            self.finished.emit(proc.returncode, last)
        except Exception as e:
            self.finished.emit(1, str(e))


class _ProvisionTunnelWorker(QThread):
    """Runs the synchronous cloudflared tunnel-create / route-dns / write-config
    flow off the Qt UI thread. Each step that takes >1s on the main thread
    would otherwise freeze the wizard window for the duration."""

    progress = Signal(str)
    # tunnel_id, tunnel_name, error_message — error empty on success
    finished_with_result = Signal(str, str, str)

    def __init__(self, hostname: str, domain: str, cloudflared_path):
        super().__init__()
        self._hostname = hostname
        self._domain = domain
        self._cf = cloudflared_path

    def run(self) -> None:
        from gui.cloudflare_setup import (
            create_tunnel, route_dns, write_config_yml, CloudflareSetupError,
        )
        name = (
            f"local-ai-stack-{self._domain.replace('.', '-')}"
            if self._domain else "local-ai-stack"
        )
        try:
            self.progress.emit(f"Creating tunnel '{name}'…")
            tunnel_id = create_tunnel(name, self._cf)
            self.progress.emit(
                f"✓ Tunnel {tunnel_id[:8]}… ready. Routing DNS for {self._hostname}…"
            )
            # overwrite=True replaces any stale CNAME from a deleted tunnel.
            route_dns(tunnel_id, self._hostname, self._cf, overwrite=True)
            self.progress.emit("✓ DNS routed. Writing config.yml…")
            write_config_yml(tunnel_id, self._hostname)
            self.finished_with_result.emit(tunnel_id, name, "")
        except CloudflareSetupError as exc:
            self.finished_with_result.emit("", "", str(exc))
        except Exception as exc:  # pragma: no cover — defensive
            self.finished_with_result.emit("", "", f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Page 1 — Welcome / prerequisites
# ---------------------------------------------------------------------------

class _WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Welcome to Local AI Stack")
        self.setSubTitle(
            "This wizard configures your installation. "
            "It checks prerequisites and walks you through the setup."
        )
        layout = QVBoxLayout(self)

        self._status_box = QPlainTextEdit()
        self._status_box.setReadOnly(True)
        self._status_box.setMaximumHeight(200)
        layout.addWidget(QLabel("Prerequisite check:"))
        layout.addWidget(self._status_box)

        self._fix_btn = QPushButton("Install missing prerequisites…")
        self._fix_btn.clicked.connect(self._run_setup)
        layout.addWidget(self._fix_btn)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._worker: _WorkerThread | None = None

    def initializePage(self) -> None:
        self._check_prereqs()

    def _check_prereqs(self) -> None:
        self._status_box.clear()

        def check(label: str, cmd: list[str]) -> bool:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                ok = r.returncode == 0
            except Exception:
                ok = False
            icon = "✓" if ok else "✗"
            self._status_box.appendPlainText(f"  [{icon}] {label}")
            return ok

        all_ok = True
        all_ok &= check("Python 3.12+", ["python", "--version"])
        all_ok &= check("cloudflared", ["cloudflared", "--version"])
        # llama-server is vendored; check the binary directly rather than PATH.
        llama_bin = _REPO / "vendor" / "llama-server"
        if (llama_bin / "llama-server.exe").exists() or (llama_bin / "llama-server").exists():
            self._status_box.appendPlainText("  [✓] llama-server (vendored)")
        else:
            self._status_box.appendPlainText("  [✗] llama-server (vendored)")
            all_ok = False
        try:
            r = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10)
            all_ok &= r.returncode == 0
            self._status_box.appendPlainText(
                f"  [{'✓' if r.returncode == 0 else '!'  }] NVIDIA driver (optional)"
            )
        except Exception:
            self._status_box.appendPlainText("  [!] NVIDIA driver not detected (CPU-only mode)")

        self._fix_btn.setEnabled(not all_ok)

    def _run_setup(self) -> None:
        ps1 = _REPO / "LocalAIStack.ps1"
        if not ps1.exists():
            QMessageBox.warning(self, "Not found", "LocalAIStack.ps1 not found.")
            return
        self._progress.setVisible(True)
        self._fix_btn.setEnabled(False)
        self._worker = _WorkerThread(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
             "-Setup", "-SkipModels"]
        )
        self._worker.line_emitted.connect(self._status_box.appendPlainText)
        self._worker.finished.connect(self._on_setup_done)
        self._worker.start()

    def _on_setup_done(self, code: int, _last: str) -> None:
        self._progress.setVisible(False)
        self._check_prereqs()

    def isComplete(self) -> bool:
        return True  # always allow advancing; missing prereqs show as warnings


# ---------------------------------------------------------------------------
# Page 2 — Admin account
# ---------------------------------------------------------------------------

class _AdminAccountPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Admin Account")
        self.setSubTitle("Create the administrator account for this installation.")

        form = QFormLayout()
        self.setLayout(form)

        self._email = QLineEdit()
        self._email.setPlaceholderText("admin@example.com")
        self._email.textChanged.connect(self.completeChanged)
        form.addRow("Email:", self._email)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.Password)
        self._password.setPlaceholderText("Min. 12 characters")
        self._password.textChanged.connect(self.completeChanged)
        form.addRow("Password:", self._password)

        self._confirm = QLineEdit()
        self._confirm.setEchoMode(QLineEdit.Password)
        self._confirm.textChanged.connect(self.completeChanged)
        form.addRow("Confirm:", self._confirm)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color: #e74c3c;")
        form.addRow("", self._hint)

        self.registerField(FIELD_EMAIL + "*", self._email)
        self.registerField(FIELD_PASSWORD + "*", self._password)

    def isComplete(self) -> bool:
        email = self._email.text().strip()
        pw = self._password.text()
        confirm = self._confirm.text()

        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            self._hint.setText("Enter a valid email address.")
            return False
        if len(pw) < 12:
            self._hint.setText("Password must be at least 12 characters.")
            return False
        if pw != confirm:
            self._hint.setText("Passwords do not match.")
            return False
        self._hint.setText("")
        return True


# ---------------------------------------------------------------------------
# Page 3 — Secrets (auto-generated)
# ---------------------------------------------------------------------------

class _SecretsPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Cryptographic Secrets")
        self.setSubTitle("Generating secure keys for your installation. No input required.")

        layout = QVBoxLayout(self)
        self._label = QLabel("Generating…")
        layout.addWidget(self._label)

        self._auth_display = QLineEdit()
        self._auth_display.setReadOnly(True)
        layout.addWidget(QLabel("AUTH_SECRET_KEY:"))
        layout.addWidget(self._auth_display)

        self._hist_display = QLineEdit()
        self._hist_display.setReadOnly(True)
        layout.addWidget(QLabel("HISTORY_SECRET_KEY:"))
        layout.addWidget(self._hist_display)

        self.registerField(FIELD_AUTH_KEY, self._auth_display)
        self.registerField(FIELD_HISTORY_KEY, self._hist_display)

    def initializePage(self) -> None:
        self._label.setText("Generating…")
        QTimer.singleShot(200, self._generate)

    def _generate(self) -> None:
        auth_key = secrets.token_urlsafe(48)
        hist_key = secrets.token_urlsafe(48)
        self._auth_display.setText(auth_key)
        self._hist_display.setText(hist_key)
        self._label.setText("✓ Keys generated successfully.")


# ---------------------------------------------------------------------------
# Page 4 — Public access / Cloudflare Tunnel
# ---------------------------------------------------------------------------

class _TunnelPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Public Access")
        self.setSubTitle(
            "Expose the chat interface over the internet via a Cloudflare Tunnel, "
            "or keep it local-only."
        )
        layout = QVBoxLayout(self)

        self._local_radio = QRadioButton("Local-only (127.0.0.1:18000 only)")
        self._local_radio.setChecked(True)
        self._local_radio.toggled.connect(self._toggle_tunnel_fields)
        layout.addWidget(self._local_radio)

        self._tunnel_radio = QRadioButton("Public via Cloudflare Tunnel")
        self._tunnel_radio.toggled.connect(self._toggle_tunnel_fields)
        layout.addWidget(self._tunnel_radio)

        self._tunnel_group = QGroupBox("Cloudflare Tunnel")
        tg_layout = QFormLayout(self._tunnel_group)

        self._domain_edit = QLineEdit()
        self._domain_edit.setPlaceholderText("example.com")
        self._domain_edit.textChanged.connect(self._on_domain_changed)
        tg_layout.addRow("Domain:", self._domain_edit)

        self._hostname_edit = QLineEdit()
        self._hostname_edit.setPlaceholderText("chat.example.com")
        tg_layout.addRow("Chat hostname:", self._hostname_edit)

        self._cf_btn = QPushButton("Connect to Cloudflare…")
        self._cf_btn.clicked.connect(self._run_cloudflare_setup)
        tg_layout.addRow("", self._cf_btn)

        self._cf_status = QLabel("")
        self._cf_status.setWordWrap(True)
        tg_layout.addRow("Status:", self._cf_status)

        self._tunnel_group.setEnabled(False)
        layout.addWidget(self._tunnel_group)

        self._tunnel_uuid: str = ""
        self._tunnel_name: str = ""
        self._cert_watcher: QFileSystemWatcher | None = None
        self._login_proc: subprocess.Popen | None = None
        self._cf_worker: _WorkerThread | None = None

        self.registerField(FIELD_DOMAIN, self._domain_edit)
        self.registerField(FIELD_CHAT_HOSTNAME, self._hostname_edit)

    def _toggle_tunnel_fields(self) -> None:
        self._tunnel_group.setEnabled(self._tunnel_radio.isChecked())
        self.completeChanged.emit()

    def _on_domain_changed(self, text: str) -> None:
        if text and not self._hostname_edit.text():
            self._hostname_edit.setText(f"chat.{text.strip()}")

    def _run_cloudflare_setup(self) -> None:
        from gui.cloudflare_setup import (
            find_cloudflared, run_login, _needs_login, find_existing_tunnel,
            CloudflareSetupError,
        )

        hostname = self._hostname_edit.text().strip()
        if not hostname:
            QMessageBox.warning(self, "Missing hostname", "Enter the chat hostname first.")
            return

        # Local config.yml-managed existing tunnel? Offer to adopt it.
        existing = find_existing_tunnel()
        if existing:
            reply = QMessageBox.question(
                self, "Existing tunnel",
                f"Found existing tunnel config at:\n{existing['config_path']}\n\n"
                "Adopt this tunnel?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._tunnel_uuid = existing["tunnel_id"]
                self._cf_status.setText(f"✓ Adopted existing tunnel {self._tunnel_uuid[:8]}…")
                self.completeChanged.emit()
                return

        self._cf_btn.setEnabled(False)

        try:
            cf = find_cloudflared()
        except FileNotFoundError as e:
            QMessageBox.warning(self, "cloudflared not found", str(e))
            self._cf_btn.setEnabled(True)
            return

        if _needs_login():
            # Make sure the directory exists BEFORE we tell QFileSystemWatcher
            # to watch it — addPath silently no-ops on missing paths.
            _CF_DIR.mkdir(parents=True, exist_ok=True)
            self._cf_status.setText("Opening browser for Cloudflare login…")
            try:
                self._login_proc = run_login(cf)
            except OSError as exc:
                self._cf_status.setText(f"✗ Could not start cloudflared: {exc}")
                self._cf_btn.setEnabled(True)
                return
            self._cert_watcher = QFileSystemWatcher([str(_CF_DIR)])
            self._cert_watcher.directoryChanged.connect(self._on_cf_dir_changed)
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(2000)
            self._poll_timer.timeout.connect(self._poll_cert)
            self._poll_timer.start()
        else:
            self._cf_status.setText("cert.pem is fresh — skipping login.")
            self._provision_tunnel(cf, hostname)

    def _poll_cert(self) -> None:
        if _CERT_PEM.exists():
            self._poll_timer.stop()
            self._on_cf_dir_changed("")

    def _on_cf_dir_changed(self, _path: str) -> None:
        if not _CERT_PEM.exists():
            return
        if self._poll_timer and self._poll_timer.isActive():
            self._poll_timer.stop()
        if self._login_proc:
            try:
                self._login_proc.terminate()
            except Exception:
                pass
            self._login_proc = None

        self._cf_status.setText("✓ Logged in. Creating tunnel…")
        try:
            from gui.cloudflare_setup import find_cloudflared
            cf = find_cloudflared()
        except FileNotFoundError as e:
            self._cf_status.setText(str(e))
            self._cf_btn.setEnabled(True)
            return
        hostname = self._hostname_edit.text().strip()
        self._provision_tunnel(cf, hostname)

    def _provision_tunnel(self, cf, hostname: str) -> None:
        # Run synchronous cloudflared CLI off the Qt UI thread. Each
        # cloudflared call can take 5–60 s; doing them inline here would
        # freeze the wizard window for the entire duration.
        domain = self._domain_edit.text().strip()
        self._provision_worker = _ProvisionTunnelWorker(hostname, domain, cf)
        self._provision_worker.progress.connect(self._cf_status.setText)
        self._provision_worker.finished_with_result.connect(self._on_provision_done)
        # Ensure the worker survives until it finishes — Qt would otherwise
        # GC it once `_run_cloudflare_setup` returns.
        self._provision_worker.finished.connect(self._provision_worker.deleteLater)
        self._provision_worker.start()

    def _on_provision_done(self, tunnel_id: str, name: str, error: str) -> None:
        if error:
            self._cf_status.setText(f"✗ {error}")
            self._cf_btn.setEnabled(True)
            return
        hostname = self._hostname_edit.text().strip()
        self._tunnel_uuid = tunnel_id
        self._tunnel_name = name
        self._cf_status.setText(
            f"✓ Tunnel ready. DNS routed to {hostname}.\n"
            "(DNS propagation may take up to 5 minutes.)"
        )
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        if self._local_radio.isChecked():
            return True
        # Tunnel mode: need a UUID
        return bool(self._tunnel_uuid and self._hostname_edit.text().strip())

    def cleanupPage(self) -> None:
        if self._login_proc:
            try:
                self._login_proc.terminate()
            except Exception:
                pass

    def get_access_mode(self) -> str:
        return "tunnel" if self._tunnel_radio.isChecked() else "local"


# ---------------------------------------------------------------------------
# Page 5 — SMTP (skippable)
# ---------------------------------------------------------------------------

class _SmtpPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Email / Magic Links")
        self.setSubTitle(
            "Configure SMTP to send magic-link login emails, "
            "or skip to log them to the console only."
        )
        layout = QVBoxLayout(self)

        self._skip_radio = QRadioButton("Skip (log magic links to console)")
        self._skip_radio.setChecked(True)
        self._skip_radio.toggled.connect(self._toggle_smtp)
        layout.addWidget(self._skip_radio)

        self._smtp_radio = QRadioButton("Configure SMTP")
        self._smtp_radio.toggled.connect(self._toggle_smtp)
        layout.addWidget(self._smtp_radio)

        self._smtp_group = QGroupBox("SMTP settings")
        self._smtp_group.setEnabled(False)
        form = QFormLayout(self._smtp_group)

        self._host = QLineEdit()
        self._host.setPlaceholderText("smtp.example.com")
        form.addRow("Host:", self._host)

        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(587)
        form.addRow("Port:", self._port)

        self._user = QLineEdit()
        form.addRow("Username:", self._user)

        self._pass = QLineEdit()
        self._pass.setEchoMode(QLineEdit.Password)
        form.addRow("Password:", self._pass)

        self._from = QLineEdit()
        self._from.setPlaceholderText("noreply@example.com")
        form.addRow("From:", self._from)

        self._starttls = QCheckBox("Use STARTTLS")
        self._starttls.setChecked(True)
        form.addRow("", self._starttls)

        layout.addWidget(self._smtp_group)

        self.registerField(FIELD_SMTP_ENABLED, self._smtp_radio)
        self.registerField(FIELD_SMTP_HOST, self._host)
        self.registerField(FIELD_SMTP_PORT, self._port, "value")
        self.registerField(FIELD_SMTP_USER, self._user)
        self.registerField(FIELD_SMTP_PASS, self._pass)
        self.registerField(FIELD_SMTP_FROM, self._from)

    def _toggle_smtp(self) -> None:
        self._smtp_group.setEnabled(self._smtp_radio.isChecked())

    def isComplete(self) -> bool:
        return True  # SMTP is optional


# ---------------------------------------------------------------------------
# (Models page removed — auto-pull on first -Start instead.)
# The launcher invokes `model_resolver resolve --pull` whenever any
# tier's GGUF is missing on disk, so the user never has to make a
# checkbox decision they can't easily undo. See Invoke-Start in
# LocalAIStack.ps1.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Page 6 — Finish
# ---------------------------------------------------------------------------

class _FinishPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Writing Configuration")
        self.setSubTitle("Saving your settings and creating the admin account.")
        self._written = False

        layout = QVBoxLayout(self)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        layout.addWidget(self._log)

    def initializePage(self) -> None:
        self._written = False
        self._log.clear()
        self._write_config()

    def _write_config(self) -> None:
        wiz = self.wizard()

        auth_key = wiz.field(FIELD_AUTH_KEY)
        hist_key = wiz.field(FIELD_HISTORY_KEY)
        email = wiz.field(FIELD_EMAIL)
        password = wiz.field(FIELD_PASSWORD)
        chat_hostname = wiz.field(FIELD_CHAT_HOSTNAME) or ""
        domain = wiz.field(FIELD_DOMAIN) or ""
        smtp_enabled = wiz.field(FIELD_SMTP_ENABLED)
        smtp_host = wiz.field(FIELD_SMTP_HOST) or ""
        smtp_port = wiz.field(FIELD_SMTP_PORT) or 587
        smtp_user = wiz.field(FIELD_SMTP_USER) or ""
        smtp_pass = wiz.field(FIELD_SMTP_PASS) or ""
        smtp_from = wiz.field(FIELD_SMTP_FROM) or ""

        # Determine access mode from page 4
        tunnel_page: _TunnelPage = wiz.page(3)  # 0-indexed
        access_mode = tunnel_page.get_access_mode() if tunnel_page else "local"
        tunnel_uuid = tunnel_page._tunnel_uuid if tunnel_page else ""
        tunnel_name = tunnel_page._tunnel_name if tunnel_page else ""

        if access_mode == "tunnel" and chat_hostname:
            public_base_url = f"https://{chat_hostname}"
        else:
            public_base_url = "http://localhost:18000"
            chat_hostname = "localhost"

        # Build .env content
        env_lines = [
            f"AUTH_SECRET_KEY={auth_key}",
            f"HISTORY_SECRET_KEY={hist_key}",
            f"PUBLIC_BASE_URL={public_base_url}",
            f"CHAT_HOSTNAME={chat_hostname}",
            "ADMIN_API_ALLOWED_HOSTS=127.0.0.1,localhost",
            "WEB_SEARCH_PROVIDER=ddg",
            "BRAVE_API_KEY=",
            f"SMTP_HOST={smtp_host if smtp_enabled else ''}",
            f"SMTP_PORT={smtp_port if smtp_enabled else '587'}",
            f"SMTP_USER={smtp_user if smtp_enabled else ''}",
            f"SMTP_PASS={smtp_pass if smtp_enabled else ''}",
            f"SMTP_FROM={smtp_from if smtp_enabled else ''}",
            f"CLOUDFLARE_TUNNEL_ID={tunnel_uuid}",
            f"CLOUDFLARE_TUNNEL_NAME={tunnel_name}",
            "MODEL_UPDATE_POLICY=prompt",
        ]

        # The launcher reads .env from the repo root. Installed-mode
        # (LAI_INSTALLED=1, Inno Setup layout) splits code from per-user
        # data, but until that path lands the launcher still owns the
        # repo-root file.
        if os.environ.get("LAI_INSTALLED") == "1":
            local_appdata = os.environ.get("LOCALAPPDATA")
            data_root = (
                pathlib.Path(local_appdata) / "LocalAIStack"
                if local_appdata else _REPO
            )
        else:
            data_root = _REPO

        data_root.mkdir(parents=True, exist_ok=True)
        env_path = data_root / ".env"
        tmp_path = data_root / ".env.tmp"

        try:
            tmp_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
            tmp_path.replace(env_path)
            self._log.appendPlainText(f"✓ .env written to {env_path}")
        except Exception as e:
            self._log.appendPlainText(f"✗ Failed to write .env: {e}")
            return

        # Seed admin user
        python = str(_VENV_BACKEND) if _VENV_BACKEND.exists() else sys.executable
        try:
            result = subprocess.run(
                [python, "-m", "backend.seed_admin",
                 "--email", email,
                 "--password", password,
                 "--admin",
                 "--if-no-admins"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(_REPO),
                env={**os.environ, "LAI_DATA_ROOT": str(data_root)},
            )
            if result.returncode == 0:
                username = email.split("@", 1)[0]
                self._log.appendPlainText(
                    f"✓ Admin user created.\n"
                    f"   Log in with username '{username}' (NOT email) and the password you set."
                )
            else:
                self._log.appendPlainText(f"! seed_admin: {result.stdout.strip() or result.stderr.strip()}")
        except Exception as e:
            self._log.appendPlainText(f"✗ seed_admin failed: {e}")

        self._written = True
        # Setup committed successfully — discard the partial-progress
        # snapshot so the next launch starts clean.
        _clear_wizard_state()
        self._log.appendPlainText("\nSetup complete. Click Finish to launch the stack.")

    def isComplete(self) -> bool:
        return self._written


# ---------------------------------------------------------------------------
# Main wizard class
# ---------------------------------------------------------------------------

class SetupWizard(QWizard):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Local AI Stack — Setup Wizard")
        self.setWizardStyle(QWizard.ModernStyle)
        self.resize(700, 520)

        # Model selection deliberately omitted: the launcher (-Start) auto-
        # pulls every tier in config/model-sources.yaml the first time it
        # finds a GGUF missing on disk. The previous _ModelsPage exposed
        # only 4 of the 5 chat tiers (missing coding + highest_quality)
        # AND passed CLI flags the resolver didn't accept — net effect was
        # bugs without giving users meaningful control.
        self.addPage(_WelcomePage())        # page id 0
        self.addPage(_AdminAccountPage())   # page id 1
        self.addPage(_SecretsPage())        # page id 2
        self.addPage(_TunnelPage())         # page id 3
        self.addPage(_SmtpPage())           # page id 4
        self.addPage(_FinishPage())         # page id 5

        # ── Progress persistence ────────────────────────────────────────
        # If a previous run was killed or crashed, restore everything the
        # user already typed. The fields are registered by their owning
        # pages during addPage(); setField after addPage works because
        # QWizard wires fields to widgets on registration.
        prior = _load_wizard_state()
        for key, val in prior.items():
            try:
                self.setField(key, val)
            except Exception as exc:
                logger.debug("Could not restore wizard field %r: %s", key, exc)

        # The "Confirm" widget on the Admin page is UI-only (not a
        # registered wizard field), so setField doesn't reach it. Mirror
        # the restored password into it so the page validates without
        # forcing a retype.
        if FIELD_PASSWORD in prior:
            try:
                admin_page = self.page(1)
                if admin_page is not None and hasattr(admin_page, "_confirm"):
                    admin_page._confirm.setText(prior[FIELD_PASSWORD])
            except Exception as exc:
                logger.debug("Could not mirror restored password to confirm: %s", exc)

        # Save after each Next/Back so a forced-quit always leaves a
        # current snapshot. _FinishPage wipes the snapshot on success.
        self.currentIdChanged.connect(lambda _id: _save_wizard_state(self))


# ---------------------------------------------------------------------------
# Entry point (standalone test)
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Local AI Stack Setup")
    wiz = SetupWizard()
    wiz.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
