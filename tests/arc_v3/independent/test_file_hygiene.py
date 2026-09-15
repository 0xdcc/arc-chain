"""Independent verification test suite for private file metadata hygiene.

Verifies:
1. Exact 0600 permission obligation from legacy tests (adapted with b'fixture' sample).
2. Insecure 0644 permissions rejection with 'insecure permissions' error message.
3. Insecure 0777 permissions rejection.
4. Additional insecure permission masks (e.g. 0660, 0640, 0400).
5. Non-regular file rejection: symlinks, directories, FIFOs (non-blocking).
6. Missing file rejection.
7. Support for PathLike objects.
8. Exception hierarchy: InsecureFileError inherits from ValueError.
"""

from __future__ import annotations

import os
import pathlib
import stat
import tempfile

import pytest

from arc_readiness.file_hygiene import (
    InsecureFileError,
    InsecureKeyFileError,
    check_private_file_metadata,
)

FIXTURE_PAYLOAD = b"fixture"


def test_private_key_file_permissions_600() -> None:
    """Verify that a private file with 0600 permissions passes inspection."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(FIXTURE_PAYLOAD)

    try:
        os.chmod(tmp_path, 0o600)
        assert check_private_file_metadata(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_private_key_file_insecure_permissions_644() -> None:
    """Verify that private files with 0644 permissions are rejected."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(FIXTURE_PAYLOAD)

    try:
        os.chmod(tmp_path, 0o644)
        with pytest.raises(InsecureFileError) as exc_info:
            check_private_file_metadata(tmp_path)
        assert "insecure permissions" in str(exc_info.value)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_private_key_file_insecure_permissions_777() -> None:
    """Verify that private files with 0777 permissions are rejected."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(FIXTURE_PAYLOAD)

    try:
        os.chmod(tmp_path, 0o777)
        with pytest.raises(InsecureFileError) as exc_info:
            check_private_file_metadata(tmp_path)
        assert "insecure permissions" in str(exc_info.value)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_private_file_insecure_permissions_variants(tmp_path: pathlib.Path) -> None:
    """Verify that other non-0600 permission masks fail-closed."""
    file_path = tmp_path / "variant_perm.txt"
    file_path.write_bytes(FIXTURE_PAYLOAD)

    # 0o400 (read-only), 0o660 (group readable), 0o640 (group readable)
    for perm in (0o400, 0o660, 0o640, 0o604):
        os.chmod(file_path, perm)
        with pytest.raises(InsecureFileError) as exc_info:
            check_private_file_metadata(file_path)
        assert "insecure permissions" in str(exc_info.value)


def test_private_file_missing(tmp_path: pathlib.Path) -> None:
    """Verify that a missing file fails-closed."""
    non_existent = tmp_path / "non_existent_key.pem"
    with pytest.raises(InsecureFileError) as exc_info:
        check_private_file_metadata(non_existent)
    assert "not found" in str(exc_info.value).lower()


def test_private_file_directory(tmp_path: pathlib.Path) -> None:
    """Verify that a directory is rejected even with 0700 or 0600 permissions."""
    sub_dir = tmp_path / "key_directory"
    sub_dir.mkdir(mode=0o700)
    with pytest.raises(InsecureFileError):
        check_private_file_metadata(sub_dir)


def test_private_file_symlink(tmp_path: pathlib.Path) -> None:
    """Verify that symlinks pointing to 0600 regular files are strictly rejected."""
    target_file = tmp_path / "target.key"
    target_file.write_bytes(FIXTURE_PAYLOAD)
    os.chmod(target_file, 0o600)

    symlink_file = tmp_path / "link.key"
    os.symlink(target_file, symlink_file)

    with pytest.raises(InsecureFileError) as exc_info:
        check_private_file_metadata(symlink_file)
    assert "symlink" in str(exc_info.value).lower()


def test_private_file_fifo(tmp_path: pathlib.Path) -> None:
    """Verify that named pipes (FIFOs) are rejected without blocking on open."""
    fifo_path = tmp_path / "test_fifo.pipe"
    try:
        os.mkfifo(fifo_path, 0o600)
    except (AttributeError, OSError):
        pytest.skip("mkfifo not supported on this platform/filesystem")

    with pytest.raises(InsecureFileError) as exc_info:
        check_private_file_metadata(fifo_path)
    assert "fifo" in str(exc_info.value).lower()


def test_private_file_pathlib_object(tmp_path: pathlib.Path) -> None:
    """Verify that pathlib.Path instances are accepted and evaluated correctly."""
    p = tmp_path / "pathlib_test.key"
    p.write_bytes(FIXTURE_PAYLOAD)
    os.chmod(p, 0o600)
    assert check_private_file_metadata(p) is True


def test_insecure_file_error_hierarchy() -> None:
    """Verify exception hierarchy and backward-compatible alias."""
    assert issubclass(InsecureFileError, ValueError)
    assert issubclass(InsecureKeyFileError, ValueError)
    assert InsecureKeyFileError is InsecureFileError
