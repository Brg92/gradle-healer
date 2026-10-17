"""Pure helpers for redacting runner-specific project paths from recorded Gradle logs."""

import ctypes
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

POSIX_PROJECT_ROOT = "<PROJECT_ROOT>"
WINDOWS_PROJECT_ROOT_BACKSLASH = r"C:\PROJECT_ROOT"
WINDOWS_PROJECT_ROOT_SLASH = "C:/PROJECT_ROOT"


def windows_short_path(path: str | Path) -> str | None:
    """Return the Windows 8.3 short path for path, or None when unavailable."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path = kernel32.GetShortPathNameW
    get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short_path.restype = ctypes.c_uint32
    raw = str(path)
    needed: int = int(get_short_path(raw, None, 0))
    if needed == 0:
        return None
    buf = ctypes.create_unicode_buffer(needed)
    written: int = int(get_short_path(raw, buf, needed))
    if written == 0 or written >= needed:
        return None
    return buf.value


def _candidate_roots(
    project: Path,
    *,
    platform: str,
    extra_roots: Sequence[str] = (),
) -> list[str]:
    raw = str(project)
    candidates: list[str] = [raw]
    resolved = os.path.realpath(raw)
    if resolved != raw:
        candidates.append(resolved)
    candidates.extend(extra_roots)

    if platform == "win32":
        short = windows_short_path(project)
        if short is not None:
            candidates.append(short)
    elif platform == "darwin":
        expanded: list[str] = []
        for candidate in candidates:
            posix = candidate.replace("\\", "/")
            if posix.startswith("/private/"):
                expanded.append(posix.removeprefix("/private"))
            elif posix.startswith("/var/") or posix.startswith("/tmp/"):
                expanded.append(f"/private{posix}")
        candidates.extend(expanded)

    # Longer paths first so a prefix never partially replaces a longer spelling
    # (e.g. "/private/var/..." before "/var/...").
    unique = list(dict.fromkeys(c.rstrip("/\\") for c in candidates if c.rstrip("/\\")))
    unique.sort(key=len, reverse=True)
    return unique


def redact_project_root(
    text: str,
    project: Path,
    *,
    platform: str,
    extra_roots: Sequence[str] = (),
) -> str:
    """Replace every spelling of project in text with the platform's placeholder.

    On Linux and macOS (`platform` in `{"linux", "darwin"}`), every occurrence
    is replaced with `<PROJECT_ROOT>`.
    On Windows (`platform == "win32"`), backslash occurrences become
    `C:\\PROJECT_ROOT` and forward-slash occurrences (such as `file:///C:/...`)
    become `C:/PROJECT_ROOT`, matching drive letters and path components
    case-insensitively.
    """
    candidates = _candidate_roots(project, platform=platform, extra_roots=extra_roots)
    redacted = text
    if platform == "win32":
        for candidate in candidates:
            posix_form = candidate.replace("\\", "/").rstrip("/")
            backslash_form = posix_form.replace("/", "\\")
            redacted = re.sub(
                re.escape(posix_form),
                lambda _m: WINDOWS_PROJECT_ROOT_SLASH,
                redacted,
                flags=re.IGNORECASE,
            )
            redacted = re.sub(
                re.escape(backslash_form),
                lambda _m: WINDOWS_PROJECT_ROOT_BACKSLASH,
                redacted,
                flags=re.IGNORECASE,
            )
        return redacted

    for candidate in candidates:
        posix_form = candidate.replace("\\", "/").rstrip("/")
        redacted = redacted.replace(posix_form, POSIX_PROJECT_ROOT)
        if candidate != posix_form:
            redacted = redacted.replace(candidate, POSIX_PROJECT_ROOT)
    return redacted


def assert_no_leak(
    text: str,
    project: Path,
    *,
    platform: str = sys.platform,
    extra_roots: Sequence[str] = (),
) -> None:
    """Raise AssertionError if any spelling of project or pytest temp dir remains in text."""
    candidates = _candidate_roots(project, platform=platform, extra_roots=extra_roots)
    lowered = text.lower()
    for candidate in candidates:
        for variant in {
            candidate,
            candidate.replace("\\", "/"),
            candidate.replace("/", "\\"),
        }:
            trimmed = variant.rstrip("/\\")
            if trimmed and trimmed.lower() in lowered:
                raise AssertionError(f"unredacted project path {trimmed!r} leaked in recorded log")
    if "pytest-of-" in lowered:
        raise AssertionError("unredacted 'pytest-of-' temp directory leaked in recorded log")
