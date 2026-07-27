from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

import yaml

from omnipet import __version__
from omnipet.checkpoint import (
    CheckpointError,
    export_checkpoint,
    restore_checkpoint_for_current_engine,
)
from omnipet.design_pack import (
    approve_design_pack_action,
    reject_design_pack_action,
    revise_design_pack_action,
    submit_design_action,
    submit_design_pack_summary_action,
    submit_intake_action,
    submit_prototype_evidence_action,
)
from omnipet.guides import add_generation_guide
from omnipet.hatch import HatchExecutionError
from omnipet.package import PackageError, check_package, publish_package, recover_package
from omnipet.project import ProjectValidationError, load_pet_project
from omnipet.public_release import (
    PublicReleaseError,
    export_public_release,
    verify_public_release,
)
from omnipet.review_resolution import ResolutionError, create_warning_resolution
from omnipet.release import (
    approve_project_stage,
    clear_project_block,
    hatch_project,
    initialize_design_run,
    init_pet_project,
    project_status,
    qa_project_stage,
    repair_project_job,
    reset_failed_job,
)
from omnipet.run import (
    RunPreparationError,
    adopt_canonical,
    load_run_state,
    prepare_run,
    refresh_prompts,
)


_WORKFLOW_ERRORS = {
    "hatch": "hatch failed",
    "approve": "approve failed",
    "qa": "qa failed",
    "status": "status failed",
}


def _build_parser() -> argparse.ArgumentParser:
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
    hatch.add_argument("--action-id")
    hatch.add_argument("--run-revision")
    recovery = hatch.add_mutually_exclusive_group()
    recovery.add_argument("--reset-failed")
    recovery.add_argument("--clear-block", action="store_true")
    approve = commands.add_parser("approve")
    approve.add_argument("pet_id")
    approve.add_argument("--stage", required=True, choices=("base", "standard-rows", "directions", "package", "design-pack"))
    approve.add_argument("--principal")
    approve.add_argument("--action-id")
    approve.add_argument("--run-revision")
    approve.add_argument("--note")
    approve.add_argument("--repo-root", type=Path, default=Path.cwd())
    qa = commands.add_parser("qa")
    qa_resolve = commands.add_parser("qa-resolve")
    qa_resolve.add_argument("pet_id")
    qa_resolve.add_argument("--report", required=True)
    qa_resolve.add_argument("--verdict-file", required=True, type=Path)
    qa_resolve.add_argument("--repo-root", type=Path, default=Path.cwd())
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
    public_release = commands.add_parser("release")
    release_commands = public_release.add_subparsers(
        dest="release_command", required=True
    )
    release_export = release_commands.add_parser("export")
    release_export.add_argument("pet_id")
    release_export.add_argument("--output", required=True, type=Path)
    release_export.add_argument("--repo-root", type=Path, default=Path.cwd())
    release_verify = release_commands.add_parser("verify")
    release_verify.add_argument("bundle_directory", type=Path)
    repair = commands.add_parser("repair")
    repair.add_argument("pet_id")
    repair.add_argument("--job", required=True)
    repair.add_argument("--reason", required=True)
    repair.add_argument("--repo-root", type=Path, default=Path.cwd())
    guide = commands.add_parser("guide")
    guide_commands = guide.add_subparsers(dest="guide_command", required=True)
    guide_add = guide_commands.add_parser("add")
    guide_add.add_argument("pet_id")
    guide_add.add_argument("--job", required=True)
    guide_add.add_argument("--file", required=True, type=Path)
    guide_add.add_argument("--role", required=True)
    guide_add.add_argument(
        "--authority",
        required=True,
        choices=("identity", "pose-only", "layout-only"),
    )
    guide_add.add_argument("--repo-root", type=Path, default=Path.cwd())
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
    refresh = run_commands.add_parser("refresh-prompts")
    refresh.add_argument("pet_id")
    refresh.add_argument("--repo-root", type=Path, default=Path.cwd())
    design = commands.add_parser("design")
    design_commands = design.add_subparsers(dest="design_command", required=True)
    design_init = design_commands.add_parser("init")
    design_init.add_argument("pet_id")
    design_init.add_argument("--repo-root", type=Path, default=Path.cwd())
    design_intake = design_commands.add_parser("intake")
    design_intake.add_argument("pet_id")
    design_intake.add_argument("--file", required=True, type=Path)
    design_submit = design_commands.add_parser("submit")
    design_submit.add_argument("pet_id")
    for flag in ("contract", "rationale", "storyboard", "prototype-plan", "look-mechanics"):
        design_submit.add_argument(f"--{flag}", required=True, type=Path)
    design_prototype = design_commands.add_parser("prototype")
    design_prototype.add_argument("pet_id")
    design_prototype.add_argument("--file", required=True, type=Path)
    design_pack = design_commands.add_parser("pack")
    design_pack.add_argument("pet_id")
    design_pack.add_argument("--contact-sheet", required=True, type=Path)
    design_pack.add_argument("--review", required=True, type=Path)
    for command in (design_intake, design_submit, design_prototype, design_pack):
        command.add_argument("--action-id", required=True)
        command.add_argument("--run-revision", required=True)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
    for name in ("revise", "reject"):
        decision = design_commands.add_parser(name)
        decision.add_argument("pet_id")
        decision.add_argument("--action-id", required=True)
        decision.add_argument("--run-revision", required=True)
        decision.add_argument("--reason")
        decision.add_argument("--repo-root", type=Path, default=Path.cwd())
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] == ["qa", "resolve"]:
        argv[:2] = ["qa-resolve"]
    args = _build_parser().parse_args(argv)

    if args.command == "pet" and args.pet_command == "init":
        try:
            destination = init_pet_project(args.repo_root, args.pet_id, standalone=args.standalone)
        except (OSError, ValueError):
            print(json.dumps({"ok": False, "error": "pet initialization failed"}), file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "pet_id": args.pet_id, "project_root": str(destination)}, sort_keys=True))
        return 0

    if args.command == "release" and args.release_command == "verify":
        try:
            release = verify_public_release(args.bundle_directory)
        except (OSError, PublicReleaseError, ValueError):
            print(
                json.dumps({"ok": False, "error": "release verify failed"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps({
            "ok": True,
            "pet_id": release["petId"],
            "version": release["version"],
            "verified": True,
        }, sort_keys=True))
        return 0

    try:
        project = load_pet_project(args.repo_root, args.pet_id)
    except (OSError, UnicodeError, ProjectValidationError, yaml.YAMLError):
        print(json.dumps({"ok": False, "error": "invalid pet project"}), file=sys.stderr)
        return 1

    run_dir = project.repository_root / ".omnipet/runs" / project.pet_id
    if args.command == "design":
        try:
            if args.design_command == "init":
                run_dir.parent.mkdir(parents=True, exist_ok=True)
                state = initialize_design_run(run_dir, project.pet_id, project.references)
            elif args.design_command == "intake":
                state = submit_intake_action(
                    run_dir, _load_json_file(args.file), action_id=args.action_id,
                    run_revision=args.run_revision,
                )
            elif args.design_command == "submit":
                state = submit_design_action(
                    run_dir, contract=_load_json_file(args.contract),
                    rationale=_load_text_file(args.rationale),
                    storyboard=_load_json_file(args.storyboard),
                    prototype_plan=_load_json_file(args.prototype_plan),
                    look_mechanics=_load_json_file(args.look_mechanics),
                    action_id=args.action_id, run_revision=args.run_revision,
                )
            elif args.design_command == "prototype":
                state = submit_prototype_evidence_action(
                    run_dir, _load_json_file(args.file), action_id=args.action_id,
                    run_revision=args.run_revision,
                )
            elif args.design_command == "pack":
                state = submit_design_pack_summary_action(
                    run_dir, args.contact_sheet, _load_json_file(args.review),
                    action_id=args.action_id, run_revision=args.run_revision,
                )
            elif args.design_command == "revise":
                state = revise_design_pack_action(
                    run_dir, args.reason or "revision requested",
                    action_id=args.action_id, run_revision=args.run_revision,
                )
            else:
                state = reject_design_pack_action(
                    run_dir, args.reason or "rejected",
                    action_id=args.action_id, run_revision=args.run_revision,
                )
            payload = project_status(project)
            payload["workflow_state"] = state.state
            print(json.dumps(payload, sort_keys=True))
            return 0
        except Exception:
            print(json.dumps({
                "ok": False, "error": f"design {args.design_command} failed",
            }), file=sys.stderr)
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

    if args.command == "release":
        try:
            destination = export_public_release(project, args.output)
            release = json.loads(
                (destination / "release.json").read_text(encoding="utf-8")
            )
            print(json.dumps({
                "ok": True,
                "pet_id": project.pet_id,
                "version": release["version"],
                "output": str(destination),
            }, sort_keys=True))
            return 0
        except (OSError, PublicReleaseError, ValueError):
            print(
                json.dumps({"ok": False, "error": "release export failed"}),
                file=sys.stderr,
            )
            return 1

    if args.command == "qa-resolve":
        try:
            run_dir = (
                project.repository_root / ".omnipet/runs" / project.pet_id
            )
            resolution = create_warning_resolution(
                run_dir, args.report, args.verdict_file
            )
            print(json.dumps({
                "ok": True,
                "pet_id": project.pet_id,
                "report": args.report,
                "resolution": resolution.relative_to(run_dir).as_posix(),
            }, sort_keys=True))
            return 0
        except (OSError, ResolutionError, ValueError):
            print(
                json.dumps({"ok": False, "error": "qa resolve failed"}),
                file=sys.stderr,
            )
            return 1

    if args.command == "repair":
        try:
            result = repair_project_job(
                project, args.job, reason=args.reason
            )
            print(json.dumps({
                "ok": True,
                "pet_id": project.pet_id,
                "repaired_job": result.repaired_job,
                "invalidated_jobs": list(result.invalidated_jobs),
                "invalidated_stages": list(result.invalidated_stages),
                "archive": result.archive_path,
            }, sort_keys=True))
            return 0
        except Exception:
            print(json.dumps({"ok": False, "error": "repair failed"}), file=sys.stderr)
            return 1

    if args.command == "guide":
        try:
            record = add_generation_guide(
                project,
                args.job,
                args.file,
                role=args.role,
                authority=args.authority,
            )
            print(json.dumps({
                "ok": True,
                "pet_id": project.pet_id,
                "guide": record,
            }, sort_keys=True))
            return 0
        except (OSError, UnicodeError, ValueError):
            print(json.dumps({"ok": False, "error": "guide add failed"}), file=sys.stderr)
            return 1

    if args.command in {"hatch", "approve", "qa", "status"}:
        try:
            if args.command == "hatch":
                state = (
                    reset_failed_job(project, args.reset_failed)
                    if args.reset_failed
                    else clear_project_block(project) if args.clear_block else hatch_project(
                        project, action_id=args.action_id,
                        run_revision=args.run_revision,
                    )
                )
                payload = project_status(project)
            elif args.command == "approve":
                if args.stage == "design-pack":
                    if not args.principal or not args.action_id or not args.run_revision:
                        raise ValueError("design pack approval arguments are required")
                    state = approve_design_pack_action(
                        project, args.principal, args.note, action_id=args.action_id,
                        run_revision=args.run_revision,
                    )
                else:
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
                if payload.get("blocked") is not None:
                    payload["blocked"] = dict(payload["blocked"])
                    payload["blocked"].pop("diagnostic", None)
            print(json.dumps(payload, sort_keys=True))
            return 0 if payload["workflow_state"] != "blocked" else 1
        except Exception:
            print(
                json.dumps({"ok": False, "error": _WORKFLOW_ERRORS[args.command]}),
                file=sys.stderr,
            )
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
                state = restore_checkpoint_for_current_engine(project, force=args.force)
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
            elif args.run_command == "refresh-prompts":
                state = refresh_prompts(project, project.repository_root)
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
                "refresh-prompts": "refresh prompts failed",
            }.get(args.run_command, "invalid run state")
            print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
            return 1
        if hasattr(state, "run_dir"):
            payload = {
                "ok": True,
                "pet_id": state.pet_id,
                "run_dir": str(state.run_dir),
                "counts": dict(state.counts),
                "stages": [
                    {"name": stage.name, "status": stage.status}
                    for stage in state.stages
                ],
            }
        else:
            payload = project_status(project)
        print(json.dumps(payload, sort_keys=True))
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


def _load_json_file(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def _load_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
