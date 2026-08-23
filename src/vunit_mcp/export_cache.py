"""Disk cache for the ``--export-json`` project model.

Exporting runs the project's own run.py in a subprocess, which parses every
registered source file — expensive on real projects. The result depends
only on the project's sources (plus the config that shapes run.py's
invocation), so it is cached on disk at
``<project>/.vunit-mcp-cache/export.json`` together with a fingerprint of
those inputs.

Invalidation is deliberately make-style (mtime_ns + size, not content
hashes — source files can be large):

- any registered file's mtime or size changed, or it disappeared;
- run.py itself changed (covers adding/removing/relocating files);
- the config that feeds run.py changed (interpreter, simulator, extra args).

Files matching a ``VUNIT_MCP_FINGERPRINT_EXCLUDE`` pattern are an exception
to the first rule: their mtime/size are not fingerprinted (for generated or
otherwise volatile files whose rewrites would churn the cache), but their
name and existence still are, so adding/removing them still invalidates.

Anything else (e.g. VUnit's own output dir, this cache) is irrelevant.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .runner import RunTimeoutError, run_vunit

CACHE_DIRNAME = ".vunit-mcp-cache"
CACHE_FILENAME = "export.json"


def cache_path(config: Config) -> Path:
    """Stable cache location for the project's exported model."""
    return config.project_dir / CACHE_DIRNAME / CACHE_FILENAME


def _file_entry(path: Path) -> list | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [str(path), st.st_mtime_ns, st.st_size]


def _excluded(rel: str, name: str, patterns: list[str]) -> bool:
    """Whether a registered file matches a VUNIT_MCP_FINGERPRINT_EXCLUDE
    pattern. A pattern is a fnmatch glob matched against the file's name
    (``*.hex``) or its project-relative path (``mem/*``, ``tb/t_*.vhd``),
    or a directory name/prefix (``mem``) matching everything under it."""
    for pat in patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
            return True
        if rel.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def fingerprint(config: Config, files: list[dict]) -> str:
    """Fingerprint of everything the export depends on.

    ``files`` is the *previous* export's file list: the export tells us
    which files the project registers, and any change to the registration
    itself is captured because run.py is always part of the fingerprint.
    Files matching ``config.fingerprint_exclude`` skip mtime/size tracking
    but still track name and existence.
    """
    entries: list = [
        "config",
        config.python,
        config.simulator,
        list(config.extra_args),
        _file_entry(config.run_script),
    ]
    seen: set[str] = set()
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict):
            continue
        name = f.get("file_name")
        if not name:
            continue
        p = Path(name)
        if not p.is_absolute():
            p = config.project_dir / p
        p = p.resolve()
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if config.fingerprint_exclude:
            try:
                rel = p.relative_to(config.project_dir).as_posix()
            except ValueError:
                rel = key
            if _excluded(rel, p.name, config.fingerprint_exclude):
                entries.append(["excluded", key, p.exists()])
                continue
        entries.append(_file_entry(p))
    payload = json.dumps(entries)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_cached(config: Config) -> tuple[dict, str] | None:
    """Return (export_data, fingerprint) from the cache, or None if the
    cache is missing or corrupt (a corrupt cache is simply re-exported)."""
    path = cache_path(config)
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(wrapper, dict):  # corrupt root (e.g. a JSON list)
        return None
    data, fp = wrapper.get("export"), wrapper.get("fingerprint")
    if not isinstance(data, dict) or not isinstance(fp, str):
        return None
    return data, fp


def save_cached(config: Config, data: dict, fingerprint: str) -> Path:
    """Atomically write the export wrapper (temp file + rename)."""
    path = cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = {
        "fingerprint": fingerprint,
        "exported_at": time.time(),
        "export": data,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(wrapper, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


@dataclass(frozen=True)
class ExportOutcome:
    """Result of :func:`get_export_json`; exactly one of data/error set."""

    data: dict | None
    error: str | None
    reused: bool
    path: Path


async def get_export_json(config: Config) -> ExportOutcome:
    """Return the project's ``--export-json`` model, from the disk cache
    while the project is unchanged, else by running ``run.py
    --export-json`` and refreshing the cache. Run off the event loop by
    the caller if the subprocess is expected to be long."""
    path = cache_path(config)
    cached = load_cached(config)
    if cached is not None:
        data, fp = cached
        if fingerprint(config, data.get("files", [])) == fp:
            return ExportOutcome(data, None, True, path)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        try:
            result = await run_vunit(config, ["--export-json", tmp_path])
        except RunTimeoutError as exc:
            return ExportOutcome(None, f"Error: {exc}", False, path)
        if not result.ok:
            return ExportOutcome(
                None,
                f"run.py --export-json failed (exit {result.returncode}):\n"
                + result.summary(),
                False,
                path,
            )
        try:
            data = json.loads(Path(tmp_path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return ExportOutcome(
                None,
                f"--export-json produced invalid JSON: {exc}\n"
                + result.summary(),
                False,
                path,
            )
        if not isinstance(data, dict):
            return ExportOutcome(
                None,
                "--export-json produced a non-object JSON root\n"
                + result.summary(),
                False,
                path,
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    save_cached(config, data, fingerprint(config, data.get("files", [])))
    return ExportOutcome(data, None, False, path)
