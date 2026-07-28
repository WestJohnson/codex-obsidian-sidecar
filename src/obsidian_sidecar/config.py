from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "codex-obsidian-sidecar" / "config.json"
DEFAULT_UPDATE_INDEX_URL = (
    "https://ai.westhawaiimarketing.com/charmfile/releases/sidecar/index.json"
)


@dataclass(frozen=True)
class Settings:
    vault_path: Path
    state_dir: Path
    codex_bin: Path
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "low"
    debounce_seconds: int = 180
    curator_timeout_seconds: int = 240
    checkpoint_enabled: bool = True
    checkpoint_max_evidence_chars: int = 20_000
    curator_usage_logging: bool = True
    minimum_confidence: float = 0.65
    basic_memory_project: str = "codex-vault"
    auto_git_backup: bool = True
    git_checkpoint_interval_seconds: int = 3_600
    freshness_project_days: int = 30
    freshness_decision_days: int = 90
    freshness_runbook_days: int = 14
    runtime_role: str = "local"
    syncthing_config_path: Path | None = None
    syncthing_url: str = "http://127.0.0.1:8384"
    syncthing_folder_id: str = "codex-obsidian-vault"
    syncthing_peer_id: str = ""
    cloud_agent_enabled: bool = False
    cloud_agent_model: str = "openai/gpt-5.6-luna"
    cloud_agent_base_url: str = "https://openrouter.ai/api/v1"
    cloud_agent_max_input_chars: int = 60_000
    cloud_agent_max_completion_tokens: int = 3_000
    cloud_agent_daily_cost_limit_usd: float = 0.25
    cloud_agent_monthly_cost_limit_usd: float = 5.0
    cloud_agent_cost_reserve_usd: float = 0.10
    cloud_backup_dir: Path | None = None
    cloud_backup_retention: int = 30
    cloud_lease_ttl_seconds: int = 1_800
    cloud_settle_timeout_seconds: int = 60
    cloud_reconnect_min_interval_seconds: int = 600
    alerts_enabled: bool = False
    alert_cooldown_seconds: int = 86_400
    staged_report_alert_hours: int = 24
    cloud_status_ssh_host: str = ""
    cloud_status_probe_interval_seconds: int = 900
    service_label: str = "io.github.codex-obsidian-sidecar"
    update_checks_enabled: bool = False
    update_check_interval_seconds: int = 86_400
    update_index_url: str = DEFAULT_UPDATE_INDEX_URL

    @property
    def queue_dir(self) -> Path:
        return self.state_dir / "queue"

    @property
    def processed_dir(self) -> Path:
        return self.state_dir / "processed"

    @property
    def failed_dir(self) -> Path:
        return self.state_dir / "failed"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def lock_dir(self) -> Path:
        return self.state_dir / "locks"

    @property
    def checkpoint_dir(self) -> Path:
        return self.state_dir / "checkpoints"

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.queue_dir,
            self.processed_dir,
            self.failed_dir,
            self.log_dir,
            self.lock_dir,
            self.checkpoint_dir,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "vault_path",
            "state_dir",
            "codex_bin",
            "syncthing_config_path",
            "cloud_backup_dir",
        ):
            data[key] = str(data[key]) if data[key] is not None else None
        return data


def config_path() -> Path:
    override = os.environ.get("OBSIDIAN_SIDECAR_CONFIG")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load_settings(path: Path | None = None) -> Settings:
    selected = (path or config_path()).expanduser()
    if not selected.exists():
        raise FileNotFoundError(f"Sidecar config not found: {selected}")
    raw = json.loads(selected.read_text(encoding="utf-8"))
    required = ("vault_path", "state_dir", "codex_bin")
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ValueError(f"Missing sidecar config keys: {', '.join(missing)}")
    settings = Settings(
        vault_path=Path(raw["vault_path"]).expanduser(),
        state_dir=Path(raw["state_dir"]).expanduser(),
        codex_bin=Path(raw["codex_bin"]).expanduser(),
        model=str(raw.get("model", "gpt-5.6-luna")),
        reasoning_effort=str(raw.get("reasoning_effort", "low")),
        debounce_seconds=max(0, int(raw.get("debounce_seconds", 180))),
        curator_timeout_seconds=max(30, int(raw.get("curator_timeout_seconds", 240))),
        checkpoint_enabled=bool(raw.get("checkpoint_enabled", True)),
        checkpoint_max_evidence_chars=max(
            4_000, int(raw.get("checkpoint_max_evidence_chars", 20_000))
        ),
        curator_usage_logging=bool(raw.get("curator_usage_logging", True)),
        minimum_confidence=float(raw.get("minimum_confidence", 0.65)),
        basic_memory_project=str(raw.get("basic_memory_project", "codex-vault")),
        auto_git_backup=bool(raw.get("auto_git_backup", True)),
        git_checkpoint_interval_seconds=max(
            0, int(raw.get("git_checkpoint_interval_seconds", 3_600))
        ),
        freshness_project_days=max(1, int(raw.get("freshness_project_days", 30))),
        freshness_decision_days=max(1, int(raw.get("freshness_decision_days", 90))),
        freshness_runbook_days=max(1, int(raw.get("freshness_runbook_days", 14))),
        runtime_role=str(raw.get("runtime_role", "local")),
        syncthing_config_path=(
            Path(raw["syncthing_config_path"]).expanduser()
            if raw.get("syncthing_config_path")
            else None
        ),
        syncthing_url=str(raw.get("syncthing_url", "http://127.0.0.1:8384")),
        syncthing_folder_id=str(raw.get("syncthing_folder_id", "codex-obsidian-vault")),
        syncthing_peer_id=str(raw.get("syncthing_peer_id", "")),
        cloud_agent_enabled=bool(raw.get("cloud_agent_enabled", False)),
        cloud_agent_model=str(raw.get("cloud_agent_model", "openai/gpt-5.6-luna")),
        cloud_agent_base_url=str(
            raw.get("cloud_agent_base_url", "https://openrouter.ai/api/v1")
        ).rstrip("/"),
        cloud_agent_max_input_chars=max(
            4_000, int(raw.get("cloud_agent_max_input_chars", 60_000))
        ),
        cloud_agent_max_completion_tokens=max(
            500, int(raw.get("cloud_agent_max_completion_tokens", 3_000))
        ),
        cloud_agent_daily_cost_limit_usd=max(
            0.0, float(raw.get("cloud_agent_daily_cost_limit_usd", 0.25))
        ),
        cloud_agent_monthly_cost_limit_usd=max(
            0.0, float(raw.get("cloud_agent_monthly_cost_limit_usd", 5.0))
        ),
        cloud_agent_cost_reserve_usd=max(
            0.0, float(raw.get("cloud_agent_cost_reserve_usd", 0.10))
        ),
        cloud_backup_dir=(
            Path(raw["cloud_backup_dir"]).expanduser()
            if raw.get("cloud_backup_dir")
            else None
        ),
        cloud_backup_retention=max(1, int(raw.get("cloud_backup_retention", 30))),
        cloud_lease_ttl_seconds=max(60, int(raw.get("cloud_lease_ttl_seconds", 1_800))),
        cloud_settle_timeout_seconds=max(
            5, int(raw.get("cloud_settle_timeout_seconds", 60))
        ),
        cloud_reconnect_min_interval_seconds=max(
            60, int(raw.get("cloud_reconnect_min_interval_seconds", 600))
        ),
        alerts_enabled=bool(raw.get("alerts_enabled", False)),
        alert_cooldown_seconds=max(300, int(raw.get("alert_cooldown_seconds", 86_400))),
        staged_report_alert_hours=max(1, int(raw.get("staged_report_alert_hours", 24))),
        cloud_status_ssh_host=str(raw.get("cloud_status_ssh_host", "")).strip(),
        cloud_status_probe_interval_seconds=max(
            60, int(raw.get("cloud_status_probe_interval_seconds", 900))
        ),
        service_label=str(
            raw.get("service_label", "io.github.codex-obsidian-sidecar")
        ).strip(),
        update_checks_enabled=bool(raw.get("update_checks_enabled", False)),
        update_check_interval_seconds=max(
            3_600, int(raw.get("update_check_interval_seconds", 86_400))
        ),
        update_index_url=str(
            raw.get(
                "update_index_url",
                DEFAULT_UPDATE_INDEX_URL,
            )
        ).strip(),
    )
    if not 0 <= settings.minimum_confidence <= 1:
        raise ValueError("minimum_confidence must be between 0 and 1")
    if settings.runtime_role not in {"local", "cloud"}:
        raise ValueError("runtime_role must be local or cloud")
    if not settings.service_label:
        raise ValueError("service_label must not be empty")
    settings.ensure_runtime_dirs()
    return settings
