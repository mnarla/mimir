"""Bounded source reads and best-effort secret redaction, with no persistence or logging."""

import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path

from pathspec import GitIgnoreSpec

REDACTED = "[REDACTED]"
# These patterns are deliberately explicit; they cannot detect every possible secret.
_PATTERNS = [
    re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----.*?"
        r"(?:-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----|\Z)",
        re.S,
    ),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{25,}"
        r"|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
]
_HEADERS = re.compile(r"(\b(?:cookie|set-cookie|authorization)[ \t]*:[ \t]*)[^\r\n]+", re.I)
_BEARER = re.compile(r'\bBearer[ \t]+[^\s,;"\']+', re.I)
_USERINFO = re.compile(r"(https?://)[^/\s@]+@", re.I)
_ASSIGNMENT = re.compile(
    r"""(\b(?:[A-Za-z0-9]+_)*(?:api[_-]?key|secret|secret_access_key|password|passwd|"""
    r"""access[_-]?token|refresh[_-]?token|token|cookie|authorization)\b["']?\s*[:=]\s*)"""
    r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&"'<>]+)""",
    re.I,
)


class PrivacyError(ValueError):
    """Source read denied; errors never expose source text or arbitrary paths."""


def scrub_text(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _HEADERS.sub(lambda m: m[1] + REDACTED, text)
    text = _BEARER.sub("Bearer " + REDACTED, text)
    text = _USERINFO.sub(lambda m: m[1] + REDACTED + "@", text)
    return _ASSIGNMENT.sub(lambda m: m[1] + REDACTED, text)


def _read(root: Path, relative: Path, limit: int) -> str:
    """Walk descriptors without following symlinks; ignored paths never reach this function."""
    try:
        with ExitStack() as stack:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            for part in relative.parts[:-1]:
                directory = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                stack.callback(os.close, directory)
            fd = os.open(
                relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            file = stack.enter_context(os.fdopen(fd, "rb"))
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise PrivacyError("Source must be a regular file")
            raw = file.read(limit + 1)
            if len(raw) > limit:
                raise PrivacyError("Source exceeds the read limit")
            return raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise PrivacyError("Cannot safely read source") from None


class SourcePolicy:
    """Policy snapshot for one explicit root; recreate it when discovery/ignore rules change."""

    def __init__(self, root: Path, *, exclusions: tuple[str, ...] = ()) -> None:
        try:
            self.root = root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise PrivacyError("Invalid source root") from None
        if not self.root.is_dir():
            raise PrivacyError("Source root must be a directory")
        self.exclusions: list[Path] = []
        for excluded in exclusions:
            path = Path(excluded)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise PrivacyError("Exclusions must be relative paths within the source root")
            self.exclusions.append(path)
        rules = self.root / ".memoryignore"
        # A dangling symlink must fail too; exists() alone would silently skip it.
        contents = (
            _read(self.root, Path(".memoryignore"), 65536)
            if (rules.exists() or rules.is_symlink())
            else ""
        )
        try:
            self.spec = GitIgnoreSpec.from_lines(
                [*contents.splitlines(), ".git/", ".env", ".env.*", ".memoryignore"]
            )
        except (ValueError, TypeError):
            raise PrivacyError("Invalid ignore rules") from None

    def read_text(self, path: Path, *, max_bytes: int = 262144) -> str:
        if type(max_bytes) is not int or not 1 <= max_bytes <= 1048576:
            raise PrivacyError("Invalid source read limit")
        path = path if path.is_absolute() else self.root / path
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise PrivacyError("Source is outside the authorized root") from None
        if not relative.parts or ".." in relative.parts:
            raise PrivacyError("Invalid source path")
        if any(
            relative == excluded or excluded in relative.parents for excluded in self.exclusions
        ):
            raise PrivacyError("Source is excluded")
        # Check parents too: a negation cannot traverse an ignored directory.
        if self.spec.match_file(relative.as_posix()) or any(
            self.spec.match_file(parent.as_posix() + "/")
            for parent in relative.parents
            if parent.parts
        ):
            raise PrivacyError("Source is ignored")
        return scrub_text(_read(self.root, relative, max_bytes))
