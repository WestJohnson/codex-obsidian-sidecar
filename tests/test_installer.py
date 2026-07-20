from __future__ import annotations

import json
import os
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
