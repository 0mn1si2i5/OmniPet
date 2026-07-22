from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Canvas:
    aspect_ratio: str
    image_size: str


_JOB_KINDS = {
    "base": "base-pet",
    "idle": "row-strip",
    "running-right": "row-strip",
    "running-left": "row-strip",
    "waving": "row-strip",
    "jumping": "row-strip",
    "failed": "row-strip",
    "waiting": "row-strip",
    "running": "row-strip",
    "review": "row-strip",
    "look-cardinals": "look-cardinals",
    "look-row-9": "look-row-strip",
    "look-row-10": "look-row-strip",
}
_BASE_CANVAS = Canvas("1:1", "1K")
_ROW_CANVAS = Canvas("21:9", "2K")
_CARDINAL_DEGREES = {"000", "090", "180", "270"}


def canvas_for_job(job_id: str, kind: str) -> Canvas:
    if not isinstance(job_id, str) or not isinstance(kind, str):
        raise ValueError("invalid visual job")
    expected_kind = _JOB_KINDS.get(job_id)
    if kind != expected_kind:
        raise ValueError("invalid visual job kind")
    return _BASE_CANVAS if job_id == "base" else _ROW_CANVAS


def validate_job_canvas(job: Mapping[str, object]) -> Canvas:
    if not isinstance(job, Mapping):
        raise ValueError("invalid visual job")
    expected = canvas_for_job(job.get("id"), job.get("kind"))
    canvas = job.get("canvas")
    if not isinstance(canvas, Mapping) or set(canvas) != {"aspect_ratio", "image_size"}:
        raise ValueError("invalid visual job canvas")
    aspect_ratio = canvas.get("aspect_ratio")
    image_size = canvas.get("image_size")
    if (
        not isinstance(aspect_ratio, str)
        or not isinstance(image_size, str)
        or Canvas(aspect_ratio, image_size) != expected
    ):
        raise ValueError("invalid visual job canvas")
    return expected


def canvas_for_cardinal_repair(job_id: str, kind: str, degree: str) -> Canvas:
    canvas_for_job(job_id, kind)
    if job_id != "look-cardinals" or degree not in _CARDINAL_DEGREES:
        raise ValueError("invalid cardinal repair")
    return _BASE_CANVAS


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) == 5 and arguments[0] == "validate":
            _, job_id, kind, canvas_json, degree = arguments
            canvas = json.loads(canvas_json)
            expected = validate_job_canvas({"id": job_id, "kind": kind, "canvas": canvas})
            if degree != "-":
                expected = canvas_for_cardinal_repair(job_id, kind, degree)
        elif len(arguments) == 4 and arguments[0] == "validate":
            _, job_id, kind, canvas_json = arguments
            expected = validate_job_canvas(
                {"id": job_id, "kind": kind, "canvas": json.loads(canvas_json)}
            )
        else:
            raise ValueError("invalid canvas command")
    except (json.JSONDecodeError, TypeError, ValueError):
        return 1
    print(json.dumps({"aspect_ratio": expected.aspect_ratio, "image_size": expected.image_size}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
