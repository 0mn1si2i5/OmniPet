"""Typed in-process APIs for OmniPet's built-in deterministic hatch runtime."""


class HatchExecutionError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"built-in hatch execution error: {code}")


__all__ = ["HatchExecutionError"]
