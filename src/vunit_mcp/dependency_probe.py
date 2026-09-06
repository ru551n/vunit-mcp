"""Answer "which files implement this test?" using the *project's* VUnit.

Run as a script by the project's own interpreter, never imported by the
server::

    <config.python> <this file>            # request JSON on stdin

This file is deliberately self-contained: the project's virtualenv has
vunit-mcp installed nowhere, so it must not import ``vunit_mcp`` (or
anything outside the standard library and ``vunit``). Keep it that way.

Why a subprocess at all: ``get_implementation_subset`` is VUnit internal
API with no CLI equivalent, so it can only be reached by importing VUnit
-- and the VUnit that must answer is the project's, not the server's.
The server used to bundle its own pinned vunit-hdl and import it in
process, which meant the dependency graph could disagree with what the
project's run.py actually compiles.

The request is JSON on stdin::

    {"project_dir": str, "scratch": str, "files": [{"library_name": str,
     "file_name": str}, ...], "test_file": str}

The reply is JSON written to ``<scratch>/result.json`` rather than
stdout, because VUnit logs to stdout and stderr and there is no contract
that it stays quiet. Exit code 0 with that file present means success;
anything else is a failure whose stderr is the diagnostic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _abs(name: str, base: Path) -> str:
    """Absolute path for a possibly-relative file name from the export.

    VUnit resolves relative names against the *process* cwd. This process
    is started with cwd set to the project dir precisely so that matches
    the export's own frame of reference, but resolve explicitly rather
    than relying on it.
    """
    path = Path(name)
    return str((path if path.is_absolute() else base / path).resolve())


def _build(request: dict[str, Any]) -> list[list[str]]:
    from vunit import VUnit
    from vunit.vunit_cli import VUnitCLI

    project_dir = Path(request["project_dir"])
    scratch = Path(request["scratch"])

    # A partial argv is enough; going through VUnitCLI (rather than
    # hand-building a Namespace) means VUnit fills in every attribute it
    # expects, so this keeps working when it grows new options.
    args = VUnitCLI().parse_args(argv=["--output-path", str(scratch)])
    vu = VUnit.from_args(args)
    # VUnit 5 dropped the implicit compile_builtins, so vunit_lib has to be
    # registered explicitly or get_implementation_subset cannot walk into
    # it. Nothing here ever compiles; the graph is only queried.
    vu.add_vhdl_builtins()

    file_libraries: dict[str, str] = {}
    for entry in request["files"]:
        library_name = entry["library_name"]
        if not any(
            lib.name == library_name for lib in vu.get_libraries(allow_empty=True)
        ):
            vu.add_library(library_name)
        abs_name = _abs(entry["file_name"], project_dir)
        vu.add_source_file(abs_name, library_name)
        file_libraries[abs_name] = library_name

    test_file = _abs(request["test_file"], project_dir)
    library = file_libraries.get(test_file)
    if library is None:
        raise RuntimeError(
            f"Test file {request['test_file']!r} is not part of the project"
        )

    source_file = vu.get_source_file(test_file, library_name=library)
    subset = vu.get_implementation_subset([source_file])
    # UI SourceFile exposes .name/.library.name (not file_name/library_name),
    # and .name re-relativizes against the process cwd on every access via
    # VUnit's simplify_path -- hence resolving against cwd here.
    cwd = Path.cwd()
    return [[f.library.name, _abs(f.name, cwd)] for f in subset]


def main() -> int:
    request = json.load(sys.stdin)
    result_path = Path(request["scratch"]) / "result.json"
    subset = _build(request)
    result_path.write_text(json.dumps({"subset": subset}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
