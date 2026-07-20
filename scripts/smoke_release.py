from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(
    command: list[str], *, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout: {result.stdout[-1000:]}\nstderr: {result.stderr[-1000:]}"
        )
    return result


def smoke(wheel: Path, bundle: Path) -> dict[str, object]:
    if not wheel.is_file() or not bundle.is_file():
        raise FileNotFoundError("release wheel and agent bundle are required")
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        required_suffixes = {
            "/AGENTS.md",
            "/.agents/skills/obsidian-sidecar-setup/SKILL.md",
            f"/artifacts/{wheel.name}",
        }
        missing = [
            suffix
            for suffix in required_suffixes
            if not any(name.endswith(suffix) for name in names)
        ]
        if missing:
            raise RuntimeError(f"agent bundle is incomplete: {missing}")

    with tempfile.TemporaryDirectory(prefix="sidecar-release-smoke-") as temp_name:
        base = Path(temp_name)
        home = base / "home"
        home.mkdir()
        vault = home / "vault"
        vault.mkdir()
        venv = base / "venv"
        env = {
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
        }
        _run(["uv", "venv", "--python", "3.11", str(venv)], env=env)
        python = venv / "bin/python"
        _run(["uv", "pip", "install", "--python", str(python), str(wheel)], env=env)
        sidecar = venv / "bin/obsidian-sidecar"
        codex = home / "bin/codex"
        codex.parent.mkdir()
        codex.write_text("#!/bin/sh\necho 'codex-cli smoke'\n", encoding="utf-8")
        codex.chmod(0o700)
        env["PATH"] = f"{venv / 'bin'}:{codex.parent}:{env.get('PATH', '')}"

        version = _run([str(sidecar), "--version"], env=env).stdout.strip()
        preflight = json.loads(_run([str(sidecar), "preflight"], env=env).stdout)
        base_setup = [
            str(sidecar),
            "setup",
            "--vault",
            str(vault),
            "--codex-bin",
            str(codex),
            "--executable",
            str(sidecar),
            "--no-service",
            "--no-basic-memory",
            "--disable-update-checks",
        ]
        plan = json.loads(_run(base_setup, env=env).stdout)
        if plan.get("status") != "planned":
            raise RuntimeError("setup did not default to a read-only plan")
        config = home / ".config/codex-obsidian-sidecar/config.json"
        if config.exists():
            raise RuntimeError("read-only setup plan wrote a config file")
        applied = json.loads(_run([*base_setup, "--apply"], env=env).stdout)
        verified = json.loads(_run([str(sidecar), "verify-install"], env=env).stdout)
        mode = stat.S_IMODE(config.stat().st_mode)
        if not applied.get("verification", {}).get("healthy") or not verified.get(
            "healthy"
        ):
            raise RuntimeError("clean installation verification failed")
        if mode != 0o600:
            raise RuntimeError(f"config permissions are {mode:04o}, expected 0600")
        return {
            "schema": 1,
            "passed": True,
            "version": version,
            "python": "3.11",
            "preflight_sidecar": preflight["tools"]["sidecar"],
            "plan_actions": [item["action"] for item in plan["actions"]],
            "verification_checks": verified["checks"],
            "config_mode": f"{mode:04o}",
            "agent_bundle": "complete",
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    wheel = args.wheel or next(
        iter(sorted((ROOT / "release/artifacts").glob("*.whl"))), None
    )
    bundle = args.bundle or next(
        iter(sorted((ROOT / "release/artifacts").glob("*-agent-bundle.zip"))), None
    )
    if wheel is None or bundle is None:
        raise SystemExit("build the release before running the smoke test")
    print(json.dumps(smoke(wheel, bundle), indent=2))


if __name__ == "__main__":
    main()
