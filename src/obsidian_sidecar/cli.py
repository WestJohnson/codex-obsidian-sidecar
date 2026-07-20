from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import load_settings
from .curator import StaticCurator
from .queueing import capture_hook
from .worker import daemon_once, process_ready, run_maintenance


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obsidian-sidecar")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("--config", type=Path, help="override the sidecar config path")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "preflight", help="report machine-readable installation capabilities"
    )
    setup = subparsers.add_parser(
        "setup", help="plan or apply a portable local installation"
    )
    setup.add_argument("--vault", type=Path, required=True)
    setup.add_argument("--codex-bin", type=Path)
    setup.add_argument("--state-dir", type=Path)
    setup.add_argument("--executable", type=Path)
    setup.add_argument("--basic-memory-project", default="codex-vault")
    setup.add_argument("--model", default="gpt-5.6-luna")
    setup.add_argument("--service-label")
    setup.add_argument("--no-codex-hook", action="store_true")
    setup.add_argument("--no-service", action="store_true")
    setup.add_argument("--no-basic-memory", action="store_true")
    setup.add_argument("--disable-update-checks", action="store_true")
    setup.add_argument(
        "--apply",
        action="store_true",
        help="apply the plan; without this flag setup is read-only",
    )
    subparsers.add_parser(
        "verify-install", help="verify configured hooks, services, and retrieval"
    )
    update_check = subparsers.add_parser(
        "update-check", help="check the configured package index for a release"
    )
    update_check.add_argument("--index-url")
    update = subparsers.add_parser(
        "update", help="install an exact verified package release with rollback"
    )
    update.add_argument("--index-url")
    update.add_argument(
        "--yes", action="store_true", help="required confirmation for mutation"
    )
    subparsers.add_parser("capture-hook", help="read one Codex hook event from stdin")
    process = subparsers.add_parser("process", help="process ready capture events")
    process.add_argument(
        "--force", action="store_true", help="ignore the debounce window"
    )
    process.add_argument(
        "--curation-json", type=Path, help="use a deterministic curation fixture"
    )
    doctor = subparsers.add_parser("doctor", help="inspect and report vault health")
    doctor.add_argument(
        "--backup", action="store_true", help="commit a local Git backup"
    )
    subparsers.add_parser("daemon-once", help="process queue and run due maintenance")
    subparsers.add_parser("benchmark", help="run the weighted end-to-end benchmark")
    subparsers.add_parser(
        "cloud-doctor", help="inspect Syncthing, leases, and conflict state"
    )
    subparsers.add_parser(
        "cloud-benchmark", help="score the deployed cloud system out of 100"
    )
    cloud = subparsers.add_parser(
        "cloud-maintenance", help="run the fenced cloud maintenance transaction"
    )
    cloud.add_argument(
        "--force-agent",
        action="store_true",
        help="run organization analysis even without a changed-source snapshot",
    )
    subparsers.add_parser(
        "cloud-reconcile", help="publish a matching offline stage after reconnection"
    )
    subparsers.add_parser(
        "alert-status", help="report only actionable memory-sidecar alert conditions"
    )
    freshness = subparsers.add_parser(
        "freshness-status", help="report computed freshness without changing notes"
    )
    freshness.add_argument("--path", help="filter to one vault-relative Markdown path")
    impact = subparsers.add_parser(
        "decision-impact", help="preview a decision's read-only blast radius"
    )
    impact.add_argument("decision_id")
    impact.add_argument("--depth", type=int, choices=(1, 2, 3), default=1)
    migration = subparsers.add_parser(
        "knowledge-migrate",
        help="plan or apply freshness and canonical-decision migration",
    )
    migration.add_argument(
        "--apply", action="store_true", help="apply the otherwise read-only plan"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "preflight":
        from .installer import preflight_report

        print(json.dumps(preflight_report(), indent=2))
        return 0
    if args.command == "setup":
        from .config import DEFAULT_CONFIG_PATH
        from .installer import (
            SetupOptions,
            apply_setup,
            setup_plan,
            suggested_codex_bin,
        )

        codex_bin = args.codex_bin or suggested_codex_bin()
        if codex_bin is None:
            print(
                json.dumps(
                    {"status": "error", "error": "Codex CLI not found"}, indent=2
                )
            )
            return 1
        selected_config = args.config or DEFAULT_CONFIG_PATH
        service_label = args.service_label
        if service_label is None and selected_config.exists():
            try:
                existing = json.loads(selected_config.read_text(encoding="utf-8"))
                service_label = existing.get("service_label")
            except (OSError, AttributeError, json.JSONDecodeError):
                pass
        options = SetupOptions(
            vault_path=args.vault,
            codex_bin=codex_bin,
            state_dir=args.state_dir
            or Path.home() / ".local/share/codex-obsidian-sidecar",
            config_path=selected_config,
            executable=args.executable,
            basic_memory_project=args.basic_memory_project,
            model=args.model,
            service_label=service_label or "io.github.codex-obsidian-sidecar",
            install_codex_hook=not args.no_codex_hook,
            install_service=not args.no_service,
            register_basic_memory=not args.no_basic_memory,
            enable_update_checks=not args.disable_update_checks,
        )
        try:
            result = apply_setup(options) if args.apply else setup_plan(options)
        except Exception as error:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": type(error).__name__,
                        "detail": str(error),
                    },
                    indent=2,
                )
            )
            return 1
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "verify-install":
        from .installer import verify_setup

        settings = load_settings(args.config)
        result = verify_setup(settings, config_path=args.config)
        print(json.dumps(result, indent=2))
        return 0 if result["healthy"] else 1
    if args.command in {"update-check", "update"}:
        from .config import config_path
        from .updates import apply_update, check_update

        selected = args.config or config_path()
        index_url = args.index_url
        if index_url is None and selected.exists():
            index_url = load_settings(selected).update_index_url
        index_url = index_url or "https://pypi.org/pypi/codex-obsidian-sidecar/json"
        try:
            result = check_update(index_url)
            if args.command == "update":
                if not args.yes:
                    print(
                        json.dumps(
                            {
                                **result,
                                "status": "confirmation-required",
                                "detail": "Re-run with --yes to apply the exact release.",
                            },
                            indent=2,
                        )
                    )
                    return 2
                result = apply_update(result)
        except Exception as error:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": type(error).__name__,
                        "detail": str(error),
                    },
                    indent=2,
                )
            )
            return 1
        print(json.dumps(result, indent=2))
        return 0
    settings = load_settings(args.config)
    if args.command == "capture-hook":
        return capture_hook(settings)
    if args.command == "process":
        curator = None
        if args.curation_json:
            curator = StaticCurator(
                json.loads(args.curation_json.read_text(encoding="utf-8"))
            )
        result = process_ready(settings, force=args.force, curator=curator)
        print(json.dumps(result.__dict__, indent=2))
        return 0 if result.failed == 0 else 1
    if args.command == "doctor":
        result = run_maintenance(settings, backup=args.backup)
        print(json.dumps(result, indent=2))
        return 0 if result["critical_failures"] == 0 and result["score"] >= 80 else 1
    if args.command == "daemon-once":
        print(json.dumps(daemon_once(settings), indent=2))
        return 0
    if args.command == "benchmark":
        from .benchmark import run_benchmark

        result = run_benchmark(settings)
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    if args.command == "cloud-doctor":
        from .cloud import cloud_doctor

        result = cloud_doctor(settings)
        print(json.dumps(result, indent=2))
        return 0 if result["healthy"] else 1
    if args.command == "cloud-benchmark":
        from .cloud import run_cloud_benchmark

        result = run_cloud_benchmark(settings)
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    if args.command == "cloud-maintenance":
        from .cloud import run_cloud_maintenance

        try:
            result = run_cloud_maintenance(settings, force_agent=bool(args.force_agent))
        except Exception as error:
            print(json.dumps({"status": "error", "error": str(error)}, indent=2))
            return 1
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "cloud-reconcile":
        from .cloud import run_cloud_reconcile

        try:
            result = run_cloud_reconcile(settings)
        except Exception as error:
            print(json.dumps({"status": "error", "error": str(error)}, indent=2))
            return 1
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "alert-status":
        from .alerts import alert_status

        print(json.dumps(alert_status(settings), indent=2))
        return 0
    if args.command == "freshness-status":
        from .knowledge import knowledge_status

        result = knowledge_status(settings)
        if args.path:
            selected = args.path.removeprefix("./")
            notes = [
                item
                for item in result["freshness"]["notes"]
                if item["path"] == selected
            ]
            result["freshness"]["notes"] = notes
            counts: dict[str, int] = {}
            for item in notes:
                counts[item["state"]] = counts.get(item["state"], 0) + 1
            result["freshness"]["counts"] = counts
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "decision-impact":
        from .knowledge import preview_decision_impact

        result = preview_decision_impact(settings, args.decision_id, depth=args.depth)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "ok" else 1
    if args.command == "knowledge-migrate":
        from .coordination import LocalWriterLease, cloud_lease_status
        from .knowledge import (
            migrate_knowledge,
            write_knowledge_report,
        )
        from .maintenance import reindex_basic_memory

        if not args.apply:
            print(json.dumps(migrate_knowledge(settings, apply=False), indent=2))
            return 0
        if settings.runtime_role != "local":
            print(
                json.dumps(
                    {
                        "status": "error",
                        "detail": "knowledge migration may mutate only the authoritative local vault",
                    },
                    indent=2,
                )
            )
            return 1
        active, reason, _ = cloud_lease_status(settings.vault_path)
        if active:
            print(
                json.dumps(
                    {
                        "status": "deferred",
                        "detail": f"cloud maintenance lease is {reason}",
                    },
                    indent=2,
                )
            )
            return 1
        with LocalWriterLease(settings.vault_path, ttl_seconds=1_800):
            result = migrate_knowledge(settings, apply=True)
            write_knowledge_report(settings)
        result["reindex_result"] = reindex_basic_memory(settings)
        print(json.dumps(result, indent=2))
        return 0 if not result["errors"] and result["reindex_result"] == "ok" else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
