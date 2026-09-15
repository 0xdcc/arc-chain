"""arc_readiness.file_hygiene: Metadata permission validation for private files."""

from __future__ import annotations

import os
import stat


class InsecureFileError(ValueError):
    """Raised when private file metadata does not satisfy strict security invariants."""


InsecureKeyFileError = InsecureFileError


def check_private_file_metadata(path: str | os.PathLike[str]) -> bool:
    """Verify that a private file has safe metadata and strict 0600 permissions.

    Security model:
    - Pure POSIX metadata verification via os.lstat on the explicitly provided path.
    - Does NOT open, read, or inspect file contents.
    - Does NOT inspect environment variables, search default key directories, or touch network.
    - Requires an existing POSIX regular file with EXACT mode 0600 (owner read/write only).
    - Strictly rejects symlinks, directories, FIFOs, sockets, device nodes, and missing paths.
    - Uses lstat without opening the path, preventing blocking on FIFOs or named pipes.
    - Note: This is an instantaneous metadata check and does not provide atomic TOCTOU
      guarantees against concurrent external filesystem modifications prior to subsequent I/O.

    Args:
        path: Explicit filesystem path to inspect.

    Returns:
        True if the file exists, is a regular file, and has exact 0600 permissions.

    Raises:
        InsecureFileError: If the path does not exist, is not a regular file (e.g. symlink,
            directory, FIFO), or has insecure permissions (e.g. 0644, 0777).
    """
    normalized_path = os.fspath(path)

    try:
        st = os.lstat(normalized_path)
    except FileNotFoundError as err:
        raise InsecureFileError(f"Private file not found: {normalized_path}") from err
    except OSError as err:
        raise InsecureFileError(f"Failed to stat private file {normalized_path}: {err}") from err

    # Reject symlinks explicitly (os.lstat inspects the symlink itself)
    if stat.S_ISLNK(st.st_mode):
        raise InsecureFileError(f"Symlinks not allowed for private file: {normalized_path}")

    # Reject non-regular files (directories, FIFOs, devices, sockets)
    if not stat.S_ISREG(st.st_mode):
        if stat.S_ISDIR(st.st_mode):
            raise InsecureFileError(f"Directory not allowed for private file: {normalized_path}")
        if stat.S_ISFIFO(st.st_mode):
            raise InsecureFileError(f"FIFO not allowed for private file: {normalized_path}")
        raise InsecureFileError(f"Private file must be a regular file: {normalized_path}")

    # Exact POSIX 0600 permission check
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o600:
        raise InsecureFileError(
            f"File has insecure permissions {oct(mode)} (expected 0600): {normalized_path}"
        )

    return True
