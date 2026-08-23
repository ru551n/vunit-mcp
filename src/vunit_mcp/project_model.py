"""In-process VUnit project facade, built from a --export-json file.

This module is the internal scaffold for VUnit functions that the
project's ``run.py`` CLI does not expose (e.g. ``get_implementation_subset``).
It must only ever be used to call VUnit's *internal* API in-process —
NEVER executed through the CLI: the export model is lossy (it lacks the
user's run.py specifics such as custom options, test attributes and
requirements), so a CLI run against it would silently operate on the
wrong project. Anything that compiles or runs goes through the project's
own run.py (see runner).

The server normally never imports vunit (VUnit.main() calls sys.exit(), so
everything else shells out to run.py). This module is the deliberate
exception: vunit_test_dependencies needs the project's dependency graph,
which only an in-process VUnit instance can answer. vunit-hdl is a hard
dependency of this package, so ``vunit`` is always available in the
server's interpreter; it is still imported lazily, here and only here,
so importing ``vunit_mcp`` never pays for it. If the import still fails
(broken install), :meth:`InternalProject.load` raises
:class:`InternalProjectError` with an actionable reinstall hint instead of
breaking the server import.

Verified against VUnit 4.7.1:
- ``VUnitCLI().parse_args(argv=["--output-path", <scratch>])`` works with
  a partial argv.
- ``VUnit.from_args(args, compile_builtins=False)`` + ``add_library`` +
  ``add_source_file`` + ``get_implementation_subset`` work without a
  simulator and without ``VUnit.main()``.
- ``add_source_file`` requires the library to exist first (``library()``
  raises KeyError otherwise).
- The ``VUnit`` constructor wipes ``<output-path>/preprocessed`` and
  writes a pickle ``project_database`` there. The internal project must
  therefore use a dedicated scratch dir, kept per export content at
  ``<project>/.vunit-mcp-cache/model/<key>`` (kept across calls — the
  pickle doubles as a parse cache), never the project's own ``vunit_out``
  (wiping it would force a full recompile of real runs), and never the
  ``.vunit-mcp-cache`` root itself (which holds the export.json cache).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import threading
from pathlib import Path

from .config import Config


class InternalProjectError(RuntimeError):
    """Raised when the in-process project cannot be built or queried."""


_IMPORT_ERROR_HINT = (
    "Failed to import vunit, which is a declared dependency of vunit-mcp. "
    "The installation looks broken — reinstall the package in the "
    "interpreter the server uses (see vunit_status), e.g. "
    "`uv pip install --force-reinstall vunit-mcp`."
)

# Cache of built projects, keyed by content hash of the export data.
# Bounded LRU: a long-lived server that keeps editing the project would
# otherwise hold every stale model (and its parsed sources) in memory.
_MAX_INSTANCES = 4
_instances: dict[str, InternalProject] = {}

# VUnit's UI is not documented as thread-safe; serialize access to the
# in-process instances (the server runs load/queries in worker threads via
# asyncio.to_thread so the event loop stays responsive during parsing).
_model_lock = threading.Lock()


def _export_key(export_data: dict) -> str:
    """Stable key for the parts of the export that define the project."""
    payload = json.dumps(
        {
            "files": export_data.get("files", []),
            "tests": export_data.get("tests", []),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class InternalProject:
    """A queryable in-process VUnit project rebuilt from an export.

    Instances are cached per export content (see :meth:`load`), so
    repeated calls with an unchanged project do not re-parse sources.
    """

    def __init__(
        self,
        vu,
        tests: list[dict],
        file_libraries: dict[str, str],
    ) -> None:
        self._vu = vu
        self._tests = tests
        self._file_libraries = file_libraries

    # -- construction ----------------------------------------------------

    @classmethod
    def load(cls, config: Config, export_data: dict) -> tuple[InternalProject, bool]:
        """Return (project, reused), reusing a cached instance when the
        export content is unchanged. May parse all project sources; run
        off the event loop (see the server's asyncio.to_thread call)."""
        with _model_lock:
            key = _export_key(export_data)
            cached = _instances.get(key)
            if cached is not None:
                # Re-insert to mark most-recently-used (plain dict keeps
                # insertion order, which drives the LRU eviction below).
                _instances[key] = _instances.pop(key)
                return cached, True

            try:
                from vunit import VUnit  # type: ignore[import-untyped]
                from vunit.vunit_cli import VUnitCLI  # type: ignore[import-untyped]
            except ImportError as exc:
                raise InternalProjectError(_IMPORT_ERROR_HINT) from exc

            # Per-key scratch dir: isolates the pickle parse cache from the
            # export.json cache that shares .vunit-mcp-cache (see above).
            scratch = config.project_dir / ".vunit-mcp-cache" / "model" / key
            scratch.mkdir(parents=True, exist_ok=True)
            try:
                args = VUnitCLI().parse_args(argv=["--output-path", str(scratch)])
                vu = VUnit.from_args(args, compile_builtins=False)
                file_libraries: dict[str, str] = {}
                for f in export_data.get("files", []):
                    library_name = f["library_name"]
                    if not any(
                        l.name == library_name
                        for l in vu.get_libraries(allow_empty=True)
                    ):
                        vu.add_library(library_name)
                    vu.add_source_file(f["file_name"], library_name)
                    file_libraries[str(Path(f["file_name"]).resolve())] = library_name
            except InternalProjectError:
                raise
            except Exception as exc:
                raise InternalProjectError(
                    f"Failed to build the in-process project model: {exc}"
                ) from exc

            project = cls(vu, list(export_data.get("tests", [])), file_libraries)
            _instances[key] = project
            while len(_instances) > _MAX_INSTANCES:
                _instances.pop(next(iter(_instances)))
            return project, False

    # -- queries ----------------------------------------------------------

    @property
    def test_names(self) -> list[str]:
        return [t["name"] for t in self._tests]

    def resolve_test(self, pattern: str) -> list[dict]:
        """Export test entries matching an exact name or VUnit-style
        wildcard. Empty list = no match; caller disambiguates >1."""
        return [t for t in self._tests if fnmatch.fnmatchcase(t["name"], pattern)]

    def implementation_subset(self, test: dict) -> list[tuple[str, str]]:
        """(library_name, absolute file_name) pairs needed to elaborate
        the test, in compile order. Runs on the shared VUnit UI (see
        :data:`_model_lock`); run off the event loop."""
        with _model_lock:
            file_name = test["location"]["file_name"]
            library = self._file_libraries.get(str(Path(file_name).resolve()))
            if library is None:
                raise InternalProjectError(
                    f"Test file {file_name!r} not found in the project model"
                )
            source_file = self._vu.get_source_file(file_name, library_name=library)
            subset = self._vu.get_implementation_subset([source_file])
            # UI SourceFile exposes .name and .library.name (not file_name /
            # library_name). .name is a property that re-relativizes against
            # the process cwd on every access (VUnit's simplify_path), so
            # resolve relative names against cwd, not the project dir.
            cwd = Path.cwd()
            def _abs(name: str) -> str:
                p = Path(name)
                return str((p if p.is_absolute() else cwd / p).resolve())

            return [(f.library.name, _abs(f.name)) for f in subset]


def clear_cache() -> None:
    """Drop all cached projects (tests / export changes)."""
    _instances.clear()
