"""Project facade over a ``--export-json`` file, for the one question the
project's ``run.py`` CLI cannot answer: which files implement a test.

Two very different kinds of query live here:

- ``test_names`` / ``resolve_test`` are pure export bookkeeping and are
  answered in process, with no VUnit anywhere.
- ``implementation_subset`` needs VUnit's internal
  ``get_implementation_subset``, which has no CLI equivalent. It runs
  ``dependency_probe.py`` as a subprocess under the *project's*
  interpreter (see runner.run_env), so the answer comes from the VUnit
  the project actually compiles with. The server itself never imports
  vunit and does not depend on vunit-hdl.

The export model is lossy -- it lacks the user's run.py specifics such as
custom options, test attributes and requirements -- so it must never be
turned back into a run.py-equivalent CLI invocation. Anything that
compiles or runs goes through the project's own run.py (see runner).

Verified against VUnit 5.0.0.dev (the ru551n fork) and 4.7.1: see
``dependency_probe.py`` for the API details this relies on.

The probe writes into a scratch dir kept per export content at
``<project>/.vunit-mcp-cache/model/<key>``. VUnit stores a pickled
``project_database`` there and loads it back on the next run
(``pickle.loads`` on its entries), which makes it a parse cache across
calls -- parsing every source file is the expensive part, and only an
export change invalidates it. The scratch path is predictable, though: the
key is a sha256 of the export content, which a hostile project can compute
itself, so a database planted there would be code execution. The first
probe of each key therefore wipes any database it did not write; later
probes in the same server process reuse the one they created, which is what
keeps the cache worth having. It is never the project's own ``vunit_out``
(wiping that would force real runs to recompile) and never the
``.vunit-mcp-cache`` root (which holds the export.json cache).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from .config import Config
from .runner import run_env


class InternalProjectError(RuntimeError):
    """Raised when the project model cannot be built or queried."""


_PROBE = Path(__file__).with_name("dependency_probe.py")

# How long the probe may take. It parses every source file in the project
# on a cold scratch dir, which is minutes on a large one; a warm
# project_database turns that into seconds.
_PROBE_TIMEOUT = 900.0

# Cache of answered subsets, keyed by (export content hash, test name).
# Bounded LRU: a long-lived server that keeps editing the project would
# otherwise accumulate an entry per test per edit.
_MAX_RESULTS = 64
_results: dict[tuple[str, str], list[tuple[str, str]]] = {}

# Export keys whose project_database this process wrote itself, and may
# therefore unpickle. See the module docstring.
_trusted_scratch: set[str] = set()

_results_lock = threading.Lock()


def _export_key(export_data: dict[str, Any]) -> str:
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
    """A queryable view of one ``--export-json`` payload."""

    def __init__(self, config: Config, export_data: dict[str, Any]) -> None:
        self._config = config
        self._files = list(export_data.get("files", []))
        self._tests = list(export_data.get("tests", []))
        self._key = _export_key(export_data)

    @classmethod
    def load(cls, config: Config, export_data: dict[str, Any]) -> InternalProject:
        """Build the view. Cheap -- no sources are parsed until
        :meth:`implementation_subset` is called."""
        return cls(config, export_data)

    # -- queries ----------------------------------------------------------

    @property
    def test_names(self) -> list[str]:
        return [t["name"] for t in self._tests]

    def resolve_test(self, pattern: str) -> list[dict[str, Any]]:
        """Export test entries matching an exact name or VUnit-style
        wildcard. Empty list = no match; caller disambiguates >1."""
        return [t for t in self._tests if fnmatch.fnmatchcase(t["name"], pattern)]

    def implementation_subset(
        self, test: dict[str, Any]
    ) -> tuple[list[tuple[str, str]], bool]:
        """``(pairs, from_cache)`` where pairs is ``(library_name, absolute
        file_name)`` in compile order.

        Blocking: shells out to the project's interpreter and may parse
        every source file. Run off the event loop.
        """
        name = test["name"]
        cache_key = (self._key, name)
        with _results_lock:
            cached = _results.get(cache_key)
            if cached is not None:
                # Re-insert to mark most-recently-used (plain dict keeps
                # insertion order, which drives the LRU eviction below).
                _results[cache_key] = _results.pop(cache_key)
                return list(cached), True

        subset = self._probe(test)

        with _results_lock:
            _results[cache_key] = subset
            while len(_results) > _MAX_RESULTS:
                _results.pop(next(iter(_results)))
        return list(subset), False

    # -- the subprocess ---------------------------------------------------

    def _probe(self, test: dict[str, Any]) -> list[tuple[str, str]]:
        scratch = self._config.project_dir / ".vunit-mcp-cache" / "model" / self._key
        try:
            scratch.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise InternalProjectError(
                f"Cannot create the model scratch directory {scratch}: {exc}"
            ) from exc
        self._wipe_untrusted_database(scratch)
        result_path = scratch / "result.json"
        result_path.unlink(missing_ok=True)

        request = json.dumps(
            {
                "project_dir": str(self._config.project_dir),
                "scratch": str(scratch),
                "files": self._files,
                "test_file": test["location"]["file_name"],
            }
        )
        argv = [self._config.python, str(_PROBE)]
        try:
            proc = subprocess.run(
                argv,
                input=request,
                capture_output=True,
                text=True,
                # The project dir is the export's frame of reference, and
                # the activated venv is what makes `import vunit` find the
                # project's VUnit rather than nothing at all.
                cwd=str(self._config.project_dir),
                env=run_env(self._config),
                timeout=_PROBE_TIMEOUT,
                check=False,
            )
        except FileNotFoundError as exc:
            raise InternalProjectError(
                f"Cannot run the project interpreter {self._config.python!r}: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise InternalProjectError(
                f"Resolving dependencies timed out after {_PROBE_TIMEOUT:.0f}s"
            ) from exc

        if proc.returncode != 0 or not result_path.is_file():
            raise InternalProjectError(_failure_message(proc, self._config))
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InternalProjectError(
                f"The dependency probe wrote an unreadable result: {exc}"
            ) from exc
        return [(library, path) for library, path in data["subset"]]

    def _wipe_untrusted_database(self, scratch: Path) -> None:
        """Drop a ``project_database`` this process did not write.

        VUnit unpickles the database it finds at the (predictable) scratch
        path, so one planted by the project would run its code. Wiping only
        on the first probe per export key keeps the database we then write
        ourselves usable as a parse cache for later probes.
        """
        with _results_lock:
            if self._key in _trusted_scratch:
                return
            _trusted_scratch.add(self._key)
        database = scratch / "project_database"
        if database.exists():
            shutil.rmtree(database, ignore_errors=True)


def _failure_message(proc: subprocess.CompletedProcess[str], config: Config) -> str:
    """Turn a probe failure into something the caller can act on."""
    detail = (proc.stderr or proc.stdout or "").strip()
    if "ModuleNotFoundError" in detail and "vunit" in detail:
        return (
            "VUnit is not installed in the project's virtualenv "
            f"({config.venv or config.python}), so its dependency graph "
            "cannot be read. Install the project's dependencies there (see "
            "vunit_status)."
        )
    tail = "\n".join(detail.splitlines()[-15:])
    return f"Failed to resolve dependencies with {config.python}:\n{tail}"


def clear_cache() -> None:
    """Drop all cached answers (tests / export changes)."""
    with _results_lock:
        _results.clear()
        _trusted_scratch.clear()
