from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_UPDATE_INDEX_URL,
    Settings,
    config_path as resolved_config_path,
    load_settings,
)


DEFAULT_SERVICE_LABEL = "io.github.codex-obsidian-sidecar"
_SERVICE_LABEL = re.compile(r"^[A-Za-z0-9_.@-]+$")


@dataclass(frozen=True)
class SetupOptions:
    vault_path: Path
    codex_bin: Path
    state_dir: Path
    config_path: Path = DEFAULT_CONFIG_PATH
    executable: Path | None = None
    basic_memory_project: str = "codex-vault"
    model: str = "gpt-5.6-luna"
    service_label: str = DEFAULT_SERVICE_LABEL
    install_codex_hook: bool = True
    install_service: bool = True
    register_basic_memory: bool = True
    enable_update_checks: bool = True


def _run(
    command: list[str], *, timeout: int = 20, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _version(command: list[str]) -> dict[str, Any]:
    try:
        result = _run(command, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": False, "detail": type(error).__name__}
    output = (result.stdout or result.stderr).strip().splitlines()
    return {
        "available": result.returncode == 0,
        "detail": output[0][:300] if output else f"exit {result.returncode}",
    }


def _codex_candidates() -> list[Path]:
    candidates: list[Path] = []
    found = shutil.which("codex")
    if found:
        candidates.append(Path(found))
    if sys.platform == "darwin":
        candidates.append(Path("/Applications/ChatGPT.app/Contents/Resources/codex"))
    candidates.extend(
        [
            Path.home() / ".local/bin/codex",
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
        ]
    )
    unique: list[Path] = []
    for candidate in candidates:
        expanded = candidate.expanduser()
        if expanded.is_file() and expanded not in unique:
            unique.append(expanded)
    return unique


def suggested_codex_bin() -> Path | None:
    candidates = _codex_candidates()
    return candidates[0] if candidates else None


def _obsidian_config_paths() -> list[Path]:
    if sys.platform == "darwin":
        return [Path.home() / "Library/Application Support/obsidian/obsidian.json"]
    if sys.platform.startswith("linux"):
        return [Path.home() / ".config/obsidian/obsidian.json"]
    appdata = os.environ.get("APPDATA")
    return [Path(appdata) / "obsidian/obsidian.json"] if appdata else []


def _obsidian_vaults() -> list[str]:
    vaults: list[str] = []
    for config in _obsidian_config_paths():
        if not config.is_file():
            continue
        try:
            value = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries = value.get("vaults", {}) if isinstance(value, dict) else {}
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            candidate = str(Path(str(entry["path"])).expanduser())
            if candidate not in vaults:
                vaults.append(candidate)
    return vaults


def preflight_report() -> dict[str, Any]:
    codex = _codex_candidates()
    executable = shutil.which("obsidian-sidecar")
    uv = shutil.which("uv")
    bm = shutil.which("bm") or shutil.which("basic-memory")
    obsidian = shutil.which("obsidian")
    system = platform.system().lower()
    return {
        "schema": 1,
        "platform": {
            "system": system,
            "release": platform.release(),
            "machine": platform.machine(),
            "home": str(Path.home()),
            "service_manager": (
                "launchd"
                if system == "darwin"
                else "systemd-user"
                if system == "linux"
                else "manual"
            ),
        },
        "tools": {
            "sidecar": _version([executable, "--version"])
            if executable
            else {"available": False, "detail": "not on PATH"},
            "codex": _version([str(codex[0]), "--version"])
            if codex
            else {"available": False, "detail": "not found"},
            "uv": _version([uv, "--version"])
            if uv
            else {"available": False, "detail": "not found"},
            "obsidian": _version([obsidian, "version"])
            if obsidian
            else {"available": False, "detail": "not found"},
            "basic_memory": _version([bm, "--version"])
            if bm
            else {"available": False, "detail": "not found"},
        },
        "codex_candidates": [str(path) for path in codex],
        "vault_candidates": _obsidian_vaults(),
        "existing": {
            "config": str(DEFAULT_CONFIG_PATH)
            if DEFAULT_CONFIG_PATH.is_file()
            else None,
            "codex_hooks": str(Path.home() / ".codex/hooks.json")
            if (Path.home() / ".codex/hooks.json").is_file()
            else None,
        },
        "requirements": {
            "python": ">=3.11",
            "codex_cli": True,
            "existing_obsidian_vault": True,
            "basic_memory": "recommended",
            "background_service": system in {"darwin", "linux"},
        },
    }


def _resolved_executable(options: SetupOptions) -> Path:
    if options.executable:
        return options.executable.expanduser().absolute()
    discovered = shutil.which("obsidian-sidecar")
    if not discovered:
        raise FileNotFoundError("obsidian-sidecar is not installed on PATH")
    return Path(discovered).absolute()


def _service_paths(label: str) -> list[Path]:
    if sys.platform == "darwin":
        return [Path.home() / "Library/LaunchAgents" / f"{label}.plist"]
    if sys.platform.startswith("linux"):
        base = Path.home() / ".config/systemd/user"
        return [base / f"{label}.service", base / f"{label}.timer"]
    return []


def _validate_options(options: SetupOptions) -> None:
    vault = options.vault_path.expanduser()
    codex = options.codex_bin.expanduser()
    if not vault.is_dir():
        raise FileNotFoundError(f"Obsidian vault does not exist: {vault}")
    if not codex.is_file() or not os.access(codex, os.X_OK):
        raise FileNotFoundError(f"Executable Codex CLI not found: {codex}")
    if not options.basic_memory_project.strip():
        raise ValueError("Basic Memory project name must not be empty")
    if not _SERVICE_LABEL.fullmatch(options.service_label):
        raise ValueError("Service label contains unsupported characters")
    if options.install_service and not _service_paths(options.service_label):
        raise RuntimeError(
            "Automatic background service setup supports macOS and Linux only"
        )


def setup_plan(options: SetupOptions) -> dict[str, Any]:
    _validate_options(options)
    executable = _resolved_executable(options)
    actions: list[dict[str, Any]] = [
        {
            "action": "write-config",
            "path": str(options.config_path.expanduser()),
            "mode": "0600",
        }
    ]
    if options.install_codex_hook:
        actions.append(
            {
                "action": "merge-codex-stop-hook",
                "path": str(Path.home() / ".codex/hooks.json"),
                "command": f"{shlex.quote(str(executable))} capture-hook",
            }
        )
    if options.register_basic_memory:
        actions.append(
            {
                "action": "register-basic-memory-project",
                "project": options.basic_memory_project,
                "path": str(options.vault_path.expanduser().absolute()),
            }
        )
    if options.install_service:
        actions.append(
            {
                "action": "install-background-service",
                "manager": "launchd" if sys.platform == "darwin" else "systemd-user",
                "paths": [str(path) for path in _service_paths(options.service_label)],
                "label": options.service_label,
            }
        )
    return {
        "schema": 1,
        "status": "planned",
        "options": {
            "vault_path": str(options.vault_path.expanduser().absolute()),
            "codex_bin": str(options.codex_bin.expanduser().absolute()),
            "sidecar_executable": str(_resolved_executable(options)),
            "state_dir": str(options.state_dir.expanduser().absolute()),
            "config_path": str(options.config_path.expanduser().absolute()),
            "executable": str(executable),
            "basic_memory_project": options.basic_memory_project,
            "model": options.model,
            "service_label": options.service_label,
            "update_checks_enabled": options.enable_update_checks,
        },
        "actions": actions,
        "manual_approvals": [
            "Review and trust the installed Codex Stop hook in a fresh Codex session.",
            "Enable Obsidian CLI in Obsidian Settings > General > Advanced if unavailable.",
        ],
        "security": {
            "requires_root": False,
            "stores_secrets": False,
            "remote_script_execution": False,
            "existing_files_backed_up": True,
            "automatic_update_mutation": False,
        },
    }


def _backup_path(path: Path, timestamp: str) -> Path:
    return path.with_name(f"{path.name}.backup-{timestamp}")


def _write_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot(path: Path) -> tuple[bytes | None, int]:
    if not path.exists():
        return None, 0o600
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _merge_hook(path: Path, command: str) -> bytes:
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Codex hooks file must contain a JSON object")
    else:
        value = {}
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Codex hooks field must contain a JSON object")
    stop = hooks.setdefault("Stop", [])
    if not isinstance(stop, list):
        raise ValueError("Codex Stop hooks field must contain a list")
    found = False
    for group in stop:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks", [])
        if not isinstance(entries, list):
            continue
        retained: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                retained.append(entry)
                continue
            existing = entry.get("command")
            try:
                parts = shlex.split(existing) if isinstance(existing, str) else []
            except ValueError:
                parts = []
            is_sidecar = (
                len(parts) == 2
                and Path(parts[0]).name == "obsidian-sidecar"
                and parts[1] == "capture-hook"
            )
            if not is_sidecar:
                retained.append(entry)
                continue
            if not found:
                retained.append(
                    {
                        **entry,
                        "type": "command",
                        "command": command,
                        "timeout": 5,
                        "statusMessage": "Queuing durable session memory",
                    }
                )
                found = True
        group["hooks"] = retained
    if found:
        return (json.dumps(value, indent=2) + "\n").encode()
    stop.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 5,
                    "statusMessage": "Queuing durable session memory",
                }
            ]
        }
    )
    return (json.dumps(value, indent=2) + "\n").encode()


def _config_bytes(options: SetupOptions) -> bytes:
    path = options.config_path.expanduser()
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Existing sidecar config must contain a JSON object")
        raw.update(loaded)
    raw.update(
        {
            "schema_version": 1,
            "vault_path": str(options.vault_path.expanduser().absolute()),
            "state_dir": str(options.state_dir.expanduser().absolute()),
            "codex_bin": str(options.codex_bin.expanduser().absolute()),
            "sidecar_executable": str(_resolved_executable(options)),
            "model": options.model,
            "basic_memory_project": options.basic_memory_project,
            "freshness_project_days": 30,
            "freshness_decision_days": 90,
            "freshness_runbook_days": 14,
            "runtime_role": "local",
            "service_label": options.service_label,
            "update_checks_enabled": options.enable_update_checks,
            "update_check_interval_seconds": 86_400,
            "update_index_url": DEFAULT_UPDATE_INDEX_URL,
            "integrations": {
                "codex_hook": options.install_codex_hook,
                "background_service": options.install_service,
                "basic_memory": options.register_basic_memory,
            },
        }
    )
    raw.setdefault("checkpoint_enabled", True)
    raw.setdefault("checkpoint_max_evidence_chars", 20_000)
    raw.setdefault("curator_usage_logging", True)
    return (json.dumps(raw, indent=2) + "\n").encode()


def _macos_plist(options: SetupOptions, executable: Path) -> bytes:
    state = options.state_dir.expanduser().absolute()
    path_entries = [
        str(executable.parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    payload = {
        "Label": options.service_label,
        "ProgramArguments": [str(executable), "daemon-once"],
        "RunAtLoad": True,
        "StartInterval": 60,
        "LowPriorityIO": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": ":".join(dict.fromkeys(path_entries)),
            "OBSIDIAN_SIDECAR_CONFIG": str(options.config_path.expanduser().absolute()),
        },
        "StandardOutPath": str(state / "logs/launchd.out.log"),
        "StandardErrorPath": str(state / "logs/launchd.err.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _linux_units(options: SetupOptions, executable: Path) -> tuple[bytes, bytes]:
    config = options.config_path.expanduser().absolute()
    service = f"""[Unit]
Description=Codex Obsidian Sidecar worker

[Service]
Type=oneshot
Environment=OBSIDIAN_SIDECAR_CONFIG={_systemd_quote(str(config))}
ExecStart={_systemd_quote(str(executable))} daemon-once
Nice=10
"""
    timer = f"""[Unit]
Description=Run Codex Obsidian Sidecar every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=5s
Persistent=true
Unit={options.service_label}.service

[Install]
WantedBy=timers.target
"""
    return service.encode(), timer.encode()


def _basic_memory_registration(project: str, vault: Path) -> dict[str, Any]:
    bm = shutil.which("bm") or shutil.which("basic-memory")
    if not bm:
        raise FileNotFoundError("Basic Memory CLI was requested but is not installed")
    listed = _run([bm, "project", "list", "--json"], timeout=30)
    if listed.returncode != 0:
        raise RuntimeError("Basic Memory project listing failed")
    value = json.loads(listed.stdout)
    projects = value.get("projects", []) if isinstance(value, dict) else []
    for entry in projects:
        if not isinstance(entry, dict) or entry.get("name") != project:
            continue
        existing = Path(str(entry.get("local_path", ""))).expanduser().absolute()
        if existing != vault.absolute():
            raise RuntimeError(
                f"Basic Memory project {project!r} already points to {existing}"
            )
        return {"status": "existing", "project": project, "path": str(existing)}
    added = _run(
        [bm, "project", "add", project, str(vault.absolute()), "--local"],
        timeout=60,
    )
    if added.returncode != 0:
        raise RuntimeError("Basic Memory project registration failed")
    return {"status": "created", "project": project, "path": str(vault.absolute())}


def _reload_service(options: SetupOptions) -> dict[str, Any]:
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}"
        _run(
            ["launchctl", "bootout", f"{target}/{options.service_label}"],
            timeout=20,
        )
        plist = _service_paths(options.service_label)[0]
        loaded = _run(["launchctl", "bootstrap", target, str(plist)], timeout=20)
        if loaded.returncode != 0:
            raise RuntimeError(
                f"launchd bootstrap failed: {loaded.stderr.strip()[:500]}"
            )
        started = _run(
            ["launchctl", "kickstart", "-k", f"{target}/{options.service_label}"],
            timeout=20,
        )
        if started.returncode != 0:
            raise RuntimeError(
                f"launchd kickstart failed: {started.stderr.strip()[:500]}"
            )
        return {"manager": "launchd", "status": "loaded"}
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise FileNotFoundError("systemctl is required for Linux background setup")
    for command in (
        [systemctl, "--user", "daemon-reload"],
        [systemctl, "--user", "enable", "--now", f"{options.service_label}.timer"],
        [systemctl, "--user", "start", f"{options.service_label}.service"],
    ):
        result = _run(command, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(
                f"systemd user setup failed: {result.stderr.strip()[:500]}"
            )
    return {"manager": "systemd-user", "status": "loaded"}


def apply_setup(options: SetupOptions) -> dict[str, Any]:
    plan = setup_plan(options)
    executable = _resolved_executable(options)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    config = options.config_path.expanduser().absolute()
    hooks = Path.home() / ".codex/hooks.json"
    service_paths = (
        _service_paths(options.service_label) if options.install_service else []
    )
    touched = [config]
    if options.install_codex_hook:
        touched.append(hooks)
    touched.extend(service_paths)
    snapshots = {path: _snapshot(path) for path in touched}
    backups: list[str] = []
    for path, (content, mode) in snapshots.items():
        if content is None:
            continue
        backup = _backup_path(path, timestamp)
        _write_bytes(backup, content, mode)
        backups.append(str(backup))
    basic_memory: dict[str, Any] | None = None
    service: dict[str, Any] | None = None
    try:
        options.state_dir.expanduser().mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_bytes(config, _config_bytes(options), 0o600)
        if options.install_codex_hook:
            command = f"{shlex.quote(str(executable))} capture-hook"
            _write_bytes(hooks, _merge_hook(hooks, command), 0o600)
        if options.install_service:
            if sys.platform == "darwin":
                _write_bytes(service_paths[0], _macos_plist(options, executable), 0o600)
            else:
                service_data, timer_data = _linux_units(options, executable)
                _write_bytes(service_paths[0], service_data, 0o600)
                _write_bytes(service_paths[1], timer_data, 0o600)
        if options.register_basic_memory:
            basic_memory = _basic_memory_registration(
                options.basic_memory_project,
                options.vault_path.expanduser().absolute(),
            )
        if options.install_service:
            service = _reload_service(options)
    except Exception:
        for path, (content, mode) in snapshots.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _write_bytes(path, content, mode)
        raise
    verification = verify_setup(load_settings(config), config_path=config)
    return {
        **plan,
        "status": "applied",
        "backups": backups,
        "basic_memory": basic_memory,
        "service": service,
        "verification": verification,
    }


def _hook_present(command: str) -> bool:
    path = Path.home() / ".codex/hooks.json"
    if not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        stop = value.get("hooks", {}).get("Stop", [])
    except (OSError, AttributeError, json.JSONDecodeError):
        return False
    return any(
        isinstance(entry, dict) and entry.get("command") == command
        for group in stop
        if isinstance(group, dict)
        for entry in group.get("hooks", [])
    )


def verify_setup(
    settings: Settings, *, config_path: Path | None = None
) -> dict[str, Any]:
    selected = (config_path or resolved_config_path()).expanduser()
    try:
        raw = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    integrations = raw.get("integrations", {}) if isinstance(raw, dict) else {}
    executable = raw.get("sidecar_executable") or shutil.which("obsidian-sidecar")
    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str, required: bool = True) -> None:
        checks.append(
            {"name": name, "passed": passed, "required": required, "detail": detail}
        )

    mode = stat.S_IMODE(selected.stat().st_mode) if selected.exists() else 0
    record("config-private", mode & 0o077 == 0, f"mode={mode:04o}")
    record("vault", settings.vault_path.is_dir(), str(settings.vault_path))
    record(
        "codex-cli",
        settings.codex_bin.is_file() and os.access(settings.codex_bin, os.X_OK),
        str(settings.codex_bin),
    )
    if integrations.get("codex_hook"):
        command = (
            f"{shlex.quote(str(Path(str(executable)).expanduser().absolute()))} capture-hook"
            if executable
            else ""
        )
        record(
            "codex-hook-structure",
            bool(command and _hook_present(command)),
            "Hook trust must also be confirmed in a fresh Codex session.",
        )
    if integrations.get("background_service"):
        if sys.platform == "darwin":
            result = _run(
                ["launchctl", "print", f"gui/{os.getuid()}/{settings.service_label}"],
                timeout=20,
            )
            record("background-service", result.returncode == 0, settings.service_label)
        elif sys.platform.startswith("linux") and shutil.which("systemctl"):
            result = _run(
                [
                    "systemctl",
                    "--user",
                    "is-enabled",
                    f"{settings.service_label}.timer",
                ],
                timeout=20,
            )
            record("background-service", result.returncode == 0, settings.service_label)
    if integrations.get("basic_memory"):
        bm = shutil.which("bm") or shutil.which("basic-memory")
        result = _run([bm, "project", "list", "--json"], timeout=30) if bm else None
        present = False
        if result and result.returncode == 0:
            try:
                value = json.loads(result.stdout)
                present = any(
                    item.get("name") == settings.basic_memory_project
                    for item in value.get("projects", [])
                    if isinstance(item, dict)
                )
            except (AttributeError, json.JSONDecodeError):
                pass
        record("basic-memory-project", present, settings.basic_memory_project)
    failures = [
        item["name"] for item in checks if item["required"] and not item["passed"]
    ]
    return {
        "schema": 1,
        "healthy": not failures,
        "failures": failures,
        "checks": checks,
        "manual": ["Confirm the Codex Stop hook is trusted before relying on capture."],
    }


def options_dict(options: SetupOptions) -> dict[str, Any]:
    value = asdict(options)
    return {
        key: str(item) if isinstance(item, Path) else item
        for key, item in value.items()
    }
