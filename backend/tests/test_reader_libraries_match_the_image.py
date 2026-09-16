"""The reader's libraries are the same four, at the same versions, in both places that name them.

The script runs inside the sandbox image; its tests run here. Two lists, two files, one set of
behaviours being asserted — and nothing but this test can tell that they still agree.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _ROOT / "backend" / "pyproject.toml"
_DOCKERFILE = _ROOT / "sandbox" / "Dockerfile.sandbox"
_PIN = re.compile(r'"([A-Za-z0-9_.-]+)==([0-9][^"]*)"')


def _group() -> dict[str, str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    pins: dict[str, str] = {}
    for entry in data["dependency-groups"]["reader"]:
        name, _, version = entry.partition("==")
        pins[name.lower()] = version
    return pins


def _image() -> dict[str, str]:
    """Every pin on the image's reader install line, transitive ones included."""
    source = _DOCKERFILE.read_text(encoding="utf-8")
    start = source.index('"openpyxl==')
    end = source.index("\n\n", start)
    return {name.lower(): version for name, version in _PIN.findall(source[start:end])}


def test_the_reader_tests_run_against_the_versions_the_container_runs() -> None:
    """★ A SUITE GREEN ABOUT THE WRONG LIBRARY IS WORSE THAN NO SUITE.

    The reader's whole test suite skipped itself into silence for want of these four libraries:
    four module-level `importorskip`s, nothing installing them, and a measured "1 skipped" over
    the 450-line script that replaced the deleted extractor. They are a dependency group now — so
    the moment they are here to run at all, they have to be the versions the container has.

    Mutation receipt: move either file's openpyxl a patch version and this fails naming both.
    """
    group, image = _group(), _image()

    assert group, "the reader dependency group is empty"
    assert {name: image[name] for name in group} == group


def test_every_library_the_reader_imports_is_pinned_in_both() -> None:
    """The four are named rather than derived, so a fifth import is a deliberate act. It has to be
    added in both places, and this is what says so."""
    reader = (_ROOT / "sandbox" / "scripts" / "read_attachment.py").read_text(encoding="utf-8")
    imported = {
        module
        for module in ("openpyxl", "docx", "pptx", "polars")
        if re.search(rf"^\s+import {module}\b", reader, re.MULTILINE)
    }
    named = {
        "openpyxl": "openpyxl",
        "docx": "python-docx",
        "pptx": "python-pptx",
        "polars": "polars",
    }

    assert {named[module] for module in imported} <= set(_group())
