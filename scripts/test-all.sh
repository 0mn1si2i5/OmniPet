#!/bin/sh
set -eu

if [ -z "${PYTHON:-}" ] && [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON=${PYTHON:-python3}
fi

if ! "$PYTHON" -c '
from importlib.metadata import version
import sys

try:
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    try:
        setuptools_version = Version(version("setuptools"))
    except InvalidVersion:
        sys.exit(1)
    if not SpecifierSet(">=75").contains(setuptools_version, prereleases=False):
        raise ValueError
    import setuptools.build_meta
except Exception:
    sys.exit(1)
' >/dev/null 2>&1; then
  printf '%s\n' \
    'Missing development/build dependencies.' \
    "Install them with: .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON" -m unittest discover -s tests
