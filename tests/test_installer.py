from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from obsidian_sidecar import installer
from obsidian_sidecar.installer import SetupOptions, apply_setup, setup_plan


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SetupOptions:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    vault = home / "vault"
    vault.mkdir()
    return SetupOptions(
        vault_path=vault,
        codex_bin=_executable(home / "codex"),
        state_dir=home / ".local/share/codex-obsidian-sidecar",
        config_path=home / ".config/codex-obsidian-sidecar/config.json",
        executable=_executable(home / ".local/bin/obsidian-sidecar"),
        install_service=False,
        register_basic_memory=False,
    )


def test_setup_defaults_to_a_read_only_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _options(tmp_path, monkeypatch)
    result = setup_plan(options)

    assert result["status"] == "planned"
    assert result["security"] == {
        "requires_root": False,
        "stores_secrets": False,
        "remote_script_execution": False,
        "existing_files_backed_up": True,
        "automatic_update_mutation": False,
    }
    assert not options.config_path.exists()


def test_setup_preserves_hooks_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _options(tmp_path, monkeypatch)
    hooks = Path.home() / ".codex/hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/usr/bin/true",
                                    "timeout": 5,
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    first = apply_setup(options)
    second = apply_setup(options)

    value = json.loads(hooks.read_text(encoding="utf-8"))
    commands = [
        entry["command"] for group in value["hooks"]["Stop"] for entry in group["hooks"]
    ]
    assert commands.count("/usr/bin/true") == 1
    assert commands.count(f"{options.executable} capture-hook") == 1
    assert first["verification"]["healthy"] is True, first["verification"]
    assert second["verification"]["healthy"] is True, second["verification"]
    assert os.stat(options.config_path).st_mode & 0o077 == 0
    config = json.loads(options.config_path.read_text(encoding="utf-8"))
    assert config["sidecar_executable"] == str(options.executable)
    assert config["update_checks_enabled"] is True
    assert "api_key" not in config


def test_setup_preserves_existing_freshness_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _options(tmp_path, monkeypatch)
    options.config_path.parent.mkdir(parents=True)
    options.config_path.write_text(
        json.dumps(
            {
                "freshness_project_days": 90,
                "freshness_decision_days": 120,
                "freshness_runbook_days": 21,
            }
        ),
        encoding="utf-8",
    )

    apply_setup(options)

    config = json.loads(options.config_path.read_text(encoding="utf-8"))
    assert config["freshness_project_days"] == 90
    assert config["freshness_decision_days"] == 120
    assert config["freshness_runbook_days"] == 21


def test_setup_migrates_an_existing_sidecar_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _options(tmp_path, monkeypatch)
    hooks = Path.home() / ".codex/hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/old/location/obsidian-sidecar capture-hook",
                                },
                                {
                                    "type": "command",
                                    "command": "/another/location/obsidian-sidecar capture-hook",
                                },
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    apply_setup(options)

    value = json.loads(hooks.read_text(encoding="utf-8"))
    commands = [
        entry["command"] for group in value["hooks"]["Stop"] for entry in group["hooks"]
    ]
    assert commands == [f"{options.executable} capture-hook"]


def test_setup_restores_files_when_an_integration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _options(tmp_path, monkeypatch)
    options = SetupOptions(
        **{
            **options.__dict__,
            "register_basic_memory": True,
        }
    )
    options.config_path.parent.mkdir(parents=True)
    original = b'{"original": true}\n'
    options.config_path.write_bytes(original)

    def fail_registration(project: str, vault: Path) -> dict[str, str]:
        del project, vault
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(installer, "_basic_memory_registration", fail_registration)

    with pytest.raises(RuntimeError, match="fixture failure"):
        apply_setup(options)

    assert options.config_path.read_bytes() == original
    assert not (Path.home() / ".codex/hooks.json").exists()


def test_setup_rejects_missing_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _options(tmp_path, monkeypatch)
    missing = SetupOptions(**{**options.__dict__, "vault_path": tmp_path / "missing"})

    with pytest.raises(FileNotFoundError, match="vault does not exist"):
        setup_plan(missing)


def test_macos_service_reload_clears_a_stale_disabled_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _options(tmp_path, monkeypatch)
    options = SetupOptions(
        **{
            **options.__dict__,
            "install_service": True,
            "service_label": "com.example.sidecar",
        }
    )
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], *, timeout: int = 20, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        del timeout, check
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer.os, "getuid", lambda: 501)
    monkeypatch.setattr(installer, "_run", fake_run)

    result = installer._reload_service(options)

    assert result == {"manager": "launchd", "status": "loaded"}
    assert calls[0] == [
        "launchctl",
        "enable",
        "gui/501/com.example.sidecar",
    ]
    assert calls[1][0:2] == ["launchctl", "bootout"]
    assert calls[2][0:2] == ["launchctl", "bootstrap"]
    assert len(calls) == 3  # RunAtLoad starts once, without killing the new worker.


@pytest.mark.parametrize(
    ("token", "previous_disabled"),
    [
        ("true", True),
        ("false", False),
        ("disabled", True),
        ("enabled", False),
        (None, None),
    ],
)
def test_failed_macos_setup_restores_disabled_state_and_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    previous_disabled: bool | None,
) -> None:
    options = _options(tmp_path, monkeypatch)
    options = SetupOptions(
        **{
            **options.__dict__,
            "install_service": True,
            "service_label": "com.example.sidecar",
        }
    )
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer.os, "getuid", lambda: 501)
    plist = installer._service_paths(options.service_label)[0]
    plist.parent.mkdir(parents=True)
    original_plist = b"original service definition"
    plist.write_bytes(original_plist)
    options.config_path.parent.mkdir(parents=True)
    original_config = b'{"original": true}\n'
    options.config_path.write_bytes(original_config)
    disabled = {"com.example.unrelated": True}
    if previous_disabled is not None:
        disabled[options.service_label] = previous_disabled
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        action = command[1]
        if action == "print-disabled":
            assert command[2] == "gui/501"
            records = "\n".join(
                f'"{label}" => {token if label == options.service_label else str(value).lower()}'
                for label, value in disabled.items()
            )
            return subprocess.CompletedProcess(
                command, 0, f"disabled services = {{\n{records}\n}}", ""
            )
        if action in {"enable", "disable"}:
            assert command[2] == "gui/501/com.example.sidecar"
            disabled[options.service_label] = action == "disable"
        elif action == "bootstrap":
            assert disabled[options.service_label] is False
            return subprocess.CompletedProcess(command, 5, "", "fixture failure")
        else:
            assert action == "bootout"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(installer, "_run", fake_run)
    with pytest.raises(RuntimeError, match="launchd bootstrap failed"):
        apply_setup(options)

    assert disabled[options.service_label] is (previous_disabled is True)
    assert disabled["com.example.unrelated"] is True
    assert plist.read_bytes() == original_plist
    assert options.config_path.read_bytes() == original_config
    assert not (Path.home() / ".codex/hooks.json").exists()
    assert calls[0][1] == "print-disabled"
    assert calls[-1][1] == ("disable" if previous_disabled else "enable")


@pytest.mark.parametrize(
    ("returncode", "output"),
    [(1, ""), (0, "invalid output")]
    + [
        (
            0,
            f'disabled services = {{\n"{installer.DEFAULT_SERVICE_LABEL}" => {token}\n}}',
        )
        for token in ("unknown", "true-ish", "disabled-ish", "")
    ],
)
def test_macos_setup_requires_readable_disabled_state_before_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, output: str
) -> None:
    options = _options(tmp_path, monkeypatch)
    options = SetupOptions(**{**options.__dict__, "install_service": True})
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, output, "")

    monkeypatch.setattr(installer, "_run", fake_run)
    with pytest.raises(RuntimeError, match="launchd disabled state"):
        apply_setup(options)
    assert len(calls) == 1
    assert calls[0][1] == "print-disabled"
    assert not options.config_path.exists()
    assert not (Path.home() / ".codex/hooks.json").exists()
    assert not installer._service_paths(options.service_label)[0].exists()
