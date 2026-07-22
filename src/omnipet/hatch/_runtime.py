from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Callable, ParamSpec, TypeVar

from omnipet.hatch import HatchExecutionError

P = ParamSpec("P")
R = TypeVar("R")


def hatch_operation(function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except (TypeError, ValueError, FileNotFoundError, FileExistsError, HatchExecutionError):
            raise
        except SystemExit as exc:
            raise HatchExecutionError(str(exc) or "vendor rejected input") from None
        except Exception as exc:
            raise HatchExecutionError(f"{type(exc).__name__}: {exc}") from exc

    return wrapped


def safe_output(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a path")
    expanded = value.expanduser()
    if ".." in expanded.parts:
        raise ValueError(f"{name} must not contain parent traversal")
    path = expanded.absolute()
    if path.is_symlink():
        raise ValueError(f"{name} must not be a symlink")
    current = path.parent
    while True:
        if current.is_symlink():
            raise ValueError(f"{name} parent must not contain symlinks")
        if current.exists() and not current.is_dir():
            raise ValueError(f"{name} parent must contain only directories")
        if current == current.parent:
            break
        current = current.parent
    return path
