"""Structured exporter and directory safety guards for atomic execution outputs.

Enforces C22 (Directory Traversal, Symlink, and Hard Link Defense):
- Prohibits directory traversal outside authorized base roots;
- Prohibits symlinks in destination paths and parent hierarchies;
- Prohibits hard links (st_nlink > 1) to prevent shared inode modification;
- Employs O_NOFOLLOW file descriptor opening;
- Provides manifest validation to prevent unmanifested files from contaminating deliveries.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO


class ExportError(Exception):
    """Base exception for export and serialization errors."""


class ExportPathSecurityError(ExportError):
    """Raised when an export destination violates path security (symlink, hard link, traversal)."""


def validate_safe_export_path(
    target_path: str | Path,
    *,
    allow_create_parent: bool = False,
    base_dir: Path | None = None,
) -> Path:
    """Validate export destination path against C22 security invariants.

    Ensures:
    - Path is not empty or blank;
    - Path does not escape base_dir if base_dir is supplied;
    - Target path and none of its parent directories are symlinks;
    - Target path, if existing, is a regular file with st_nlink == 1 (no hard links).
    """
    if target_path is None:
        raise ExportPathSecurityError("Target path cannot be None")

    if isinstance(target_path, str):
        cleaned = target_path.strip()
        if not cleaned:
            raise ExportPathSecurityError("Target path cannot be empty or blank")
        path_obj = Path(cleaned)
    elif isinstance(target_path, Path):
        path_obj = target_path
    else:
        raise ExportPathSecurityError(f"Unsupported path type: {type(target_path).__name__}")

    resolved_path = path_obj.resolve()

    if base_dir is not None:
        resolved_base = base_dir.resolve()
        try:
            resolved_path.relative_to(resolved_base)
        except ValueError as exc:
            raise ExportPathSecurityError(
                f"Path {path_obj} escapes base directory {base_dir}"
            ) from exc

    current = path_obj
    parts_to_check: list[Path] = [current]
    for parent in current.parents:
        parts_to_check.append(parent)

    for component in reversed(parts_to_check):
        if component.is_symlink() or os.path.islink(component):
            raise ExportPathSecurityError(
                f"Symlink detected in path component under C22: {component}"
            )

    parent_dir = path_obj.parent
    if not parent_dir.exists():
        if allow_create_parent:
            parent_dir.mkdir(parents=True, exist_ok=True)
        else:
            raise ExportPathSecurityError(f"Parent directory does not exist: {parent_dir}")

    if path_obj.is_symlink() or os.path.islink(path_obj):
        raise ExportPathSecurityError(f"Target path is a symlink under C22: {path_obj}")

    if path_obj.exists():
        if path_obj.is_dir():
            raise ExportPathSecurityError(f"Target path is a directory, expected file: {path_obj}")

        metadata = path_obj.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ExportPathSecurityError(
                f"Target path is not a regular file under C22: {path_obj}"
            )
        if metadata.st_nlink > 1:
            raise ExportPathSecurityError(
                f"Target path has hard link count {metadata.st_nlink} > 1 under C22: {path_obj}"
            )

    return path_obj


def safe_open_for_write(
    target_path: str | Path,
    *,
    allow_create_parent: bool = False,
    base_dir: Path | None = None,
) -> TextIO:
    """Open target path for writing using O_NOFOLLOW and strict C22 invariant assertions."""
    validated = validate_safe_export_path(
        target_path,
        allow_create_parent=allow_create_parent,
        base_dir=base_dir,
    )

    open_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    open_flags |= nofollow_flag

    try:
        descriptor = os.open(str(validated), open_flags, 0o644)
    except OSError as exc:
        raise ExportPathSecurityError(f"Failed to open {validated} with O_NOFOLLOW: {exc}") from exc

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ExportPathSecurityError(
                f"Opened file descriptor for {validated} is not a regular file"
            )
        if file_stat.st_nlink > 1:
            raise ExportPathSecurityError(
                f"Opened file descriptor for {validated} has hard link count {file_stat.st_nlink} > 1"
            )
        return open(descriptor, "w", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def export_records_to_jsonl(
    records: Iterable[Mapping[str, Any] | Any],
    destination: str | Path,
    *,
    allow_create_parent: bool = False,
    base_dir: Path | None = None,
) -> int:
    """Safely serialize and write records line-by-line as canonical JSONL."""
    written_count = 0
    with safe_open_for_write(
        destination,
        allow_create_parent=allow_create_parent,
        base_dir=base_dir,
    ) as output_stream:
        for item in records:
            if hasattr(item, "to_dict") and callable(item.to_dict):
                serialized_dict = item.to_dict()
            elif isinstance(item, Mapping):
                serialized_dict = dict(item)
            else:
                raise ExportError(f"Cannot serialize item of type {type(item).__name__} to dict")

            line = json.dumps(
                serialized_dict,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            output_stream.write(line + "\n")
            written_count += 1

    return written_count


def export_summary_to_json(
    summary: Mapping[str, Any] | Any,
    destination: str | Path,
    *,
    allow_create_parent: bool = False,
    base_dir: Path | None = None,
) -> None:
    """Safely serialize and write pipeline summary as canonical JSON."""
    if hasattr(summary, "to_dict") and callable(summary.to_dict):
        payload = summary.to_dict()
    elif isinstance(summary, Mapping):
        payload = dict(summary)
    else:
        raise ExportError(f"Cannot serialize summary of type {type(summary).__name__}")

    with safe_open_for_write(
        destination,
        allow_create_parent=allow_create_parent,
        base_dir=base_dir,
    ) as output_stream:
        output_stream.write(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )


def verify_directory_manifest(
    directory: str | Path,
    allowed_filenames: set[str] | Sequence[str],
) -> bool:
    """Verify directory contains no symlinks and no unmanifested files under C22."""
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise ExportPathSecurityError(f"Directory does not exist or is not a directory: {dir_path}")

    if dir_path.is_symlink() or os.path.islink(dir_path):
        raise ExportPathSecurityError(f"Directory is a symlink under C22: {dir_path}")

    allowed_set = set(allowed_filenames)
    for entry in dir_path.iterdir():
        if entry.is_symlink() or os.path.islink(entry):
            raise ExportPathSecurityError(f"Symlink found inside directory under C22: {entry}")

        if entry.name not in allowed_set:
            raise ExportPathSecurityError(
                f"Unmanifested file found inside directory under C22: {entry.name}"
            )

    return True
