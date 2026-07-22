from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

import yaml

from omnipet import __version__
from omnipet.checkpoint import CheckpointError, export_checkpoint, restore_checkpoint
from omnipet.hatch import HatchExecutionError
from omnipet.package import PackageError, check_package, publish_package, recover_package
from omnipet.project import ProjectValidationError, load_pet_project
from omnipet.release import (
    approve_project_stage,
    clear_project_block,
    hatch_project,
    init_pet_project,
    project_status,
    qa_project_stage,
    reset_failed_job,
)
from omnipet.run import (
    RunPreparationError,
    adopt_canonical,
    load_run_state,
    prepare_run,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omnipet")
    parser.add_argument("--version", action="version", version=f"omnipet {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    pet = commands.add_parser("pet")
    pet_commands = pet.add_subparsers(dest="pet_command", required=True)
    validate = pet_commands.add_parser("validate")
    validate.add_argument("pet_id")
    validate.add_argument("--repo-root", type=Path, default=Path.cwd())
    init = pet_commands.add_parser("init")
    init.add_argument("pet_id")
    init.add_argument("--repo-root", type=Path, default=Path.cwd())
    init.add_argument("--standalone", type=Path)
    hatch = commands.add_parser("hatch")
    hatch.add_argument("pet_id")
    hatch.add_argument("--repo-root", type=Path, default=Path.cwd())
    recovery = hatch.add_mutually_exclusive_group()
    recovery.add_argument("--reset-failed")
    recovery.add_argument("--clear-block", action="store_true")
    approve = commands.add_parser("approve")
    approve.add_argument("pet_id")
    approve.add_argument("--stage", required=True, choices=("base", "standard-rows", "directions", "package"))
    approve.add_argument("--note")
    approve.add_argument("--repo-root", type=Path, default=Path.cwd())
    qa = commands.add_parser("qa")
    qa.add_argument("pet_id")
    qa.add_argument("--stage", required=True, choices=("base", "standard-rows", "directions", "package"))
    qa.add_argument("--verdict-file", type=Path)
    qa.add_argument("--repo-root", type=Path, default=Path.cwd())
    public_status = commands.add_parser("status")
    public_status.add_argument("pet_id")
    public_status.add_argument("--repo-root", type=Path, default=Path.cwd())
    package = commands.add_parser("package")
    package.add_argument("pet_id")
    package_mode = package.add_mutually_exclusive_group()
    package_mode.add_argument("--check", action="store_true")
    package_mode.add_argument("--recover", action="store_true")
    package.add_argument("--repo-root", type=Path, default=Path.cwd())
    run = commands.add_parser("run")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    prepare = run_commands.add_parser("prepare")
    prepare.add_argument("pet_id")
    prepare.add_argument("--repo-root", type=Path, default=Path.cwd())
    status = run_commands.add_parser("status")
    status.add_argument("pet_id")
    status.add_argument("--repo-root", type=Path, default=Path.cwd())
    adopt = run_commands.add_parser("adopt-canonical")
    adopt.add_argument("pet_id")
    adopt.add_argument("--reset-generated-work", action="store_true")
    adopt.add_argument("--repo-root", type=Path, default=Path.cwd())
    checkpoint = commands.add_parser("checkpoint")
    checkpoint_commands = checkpoint.add_subparsers(dest="checkpoint_command", required=True)
    checkpoint_export = checkpoint_commands.add_parser("export")
    checkpoint_export.add_argument("pet_id")
    checkpoint_export.add_argument("--repo-root", type=Path, default=Path.cwd())
    checkpoint_export.add_argument("--force", action="store_true")
    checkpoint_restore = checkpoint_commands.add_parser("restore")
    checkpoint_restore.add_argument("pet_id")
    checkpoint_restore.add_argument("--repo-root", type=Path, default=Path.cwd())
    checkpoint_restore.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "pet" and args.pet_command == "init":
        try:
            destination = init_pet_project(args.repo_root, args.pet_id, standalone=args.standalone)
        except (OSError, ValueError):
            print(json.dumps({"ok": False, "error": "pet initialization failed"}), file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "pet_id": args.pet_id, "project_root": str(destination)}, sort_keys=True))
        return 0

    try:
        project = load_pet_project(args.repo_root, args.pet_id)
    except (OSError, UnicodeError, ProjectValidationError, yaml.YAMLError):
        print(json.dumps({"ok": False, "error": "invalid pet project"}), file=sys.stderr)
        return 1

    if args.command == "package":
        try:
            if args.recover:
                recover_package(project)
                print(json.dumps({"ok": True, "pet_id": project.pet_id, "recovered": True}, sort_keys=True))
                return 0
            manifest = check_package(project)
            outputs = None if args.check else publish_package(project)
            print(json.dumps({
                "ok": True,
                "pet_id": project.pet_id,
                "checked": True,
                "published": outputs is not None,
                "spriteVersionNumber": manifest["spriteVersionNumber"],
                "outputs": [str(path) for path in outputs] if outputs else [],
            }, sort_keys=True))
            return 0
        except (PackageError, OSError, ValueError):
            print(json.dumps({"ok": False, "error": "package failed"}), file=sys.stderr)
            return 1

    if args.command in {"hatch", "approve", "qa", "status"}:
        try:
            if args.command == "hatch":
                state = (
                    reset_failed_job(project, args.reset_failed)
                    if args.reset_failed
                    else clear_project_block(project) if args.clear_block else hatch_project(project)
                )
                payload = project_status(project)
            elif args.command == "approve":
                state = approve_project_stage(project, args.stage, note=args.note)
                payload = project_status(project)
            elif args.command == "qa":
                state = qa_project_stage(project, args.stage, verdict_file=args.verdict_file)
                payload = project_status(project)
            else:
                payload = project_status(project)
                state = None
            if state is not None:
                payload["workflow_state"] = state.state
            print(json.dumps(payload, sort_keys=True))
            return 0 if payload["workflow_state"] != "blocked" else 1
        except Exception:
            print(json.dumps({"ok": False, "error": f"{args.command} failed"}), file=sys.stderr)
            return 1

    if args.command == "checkpoint":
        try:
            if args.checkpoint_command == "export":
                checkpoint_path = export_checkpoint(project, force=args.force)
                manifest = json.loads((checkpoint_path / "checkpoint.json").read_text(encoding="utf-8"))
                print(json.dumps({
                    "ok": True,
                    "pet_id": project.pet_id,
                    "checkpoint": str(checkpoint_path),
                    "completed_jobs": manifest["completed_jobs"],
                }, sort_keys=True))
            else:
                state = restore_checkpoint(project, force=args.force)
                print(json.dumps({
                    "ok": True,
                    "pet_id": state.pet_id,
                    "run_dir": str(state.run_dir),
                    "counts": dict(state.counts),
                }, sort_keys=True))
            return 0
        except (CheckpointError, HatchExecutionError, OSError, ValueError):
            print(json.dumps({
                "ok": False,
                "error": f"checkpoint {args.checkpoint_command} failed",
            }), file=sys.stderr)
            return 1

    if args.command == "run":
        try:
            if args.run_command == "prepare":
                state = prepare_run(project, project.repository_root)
            elif args.run_command == "adopt-canonical":
                state = adopt_canonical(
                    project,
                    project.repository_root,
                    reset_generated_work=args.reset_generated_work,
                )
            else:
                state = load_run_state(
                    project.repository_root,
                    project.pet_id,
                    display_name=project.display_name,
                )
        except (HatchExecutionError, RunPreparationError):
            error = {
                "prepare": "run preparation failed",
                "adopt-canonical": "canonical adoption failed",
            }.get(args.run_command, "invalid run state")
            print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "pet_id": state.pet_id,
                    "run_dir": str(state.run_dir),
                    "counts": dict(state.counts),
                    "stages": [
                        {"name": stage.name, "status": stage.status}
                        for stage in state.stages
                    ],
                },
                sort_keys=True,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "ok": True,
                "pet_id": project.pet_id,
                "project_root": str(project.root),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
