"""Local Git identity resolution. Never fetches remotes or opens source transcripts."""

import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


class ProjectError(ValueError):
    """Bounded error; never include raw Git output or remote credentials."""


class ProjectConflict(ProjectError):
    """Aliases disagree or an established remote changed; explicit review required."""


@dataclass(frozen=True)
class ProjectResolution:
    project_id: str
    resolution: str
    needs_review: bool = False


def normalize_remote(remote: str) -> str:
    """Normalize known hosting equivalences; reject uncertain/local remote formats."""
    if not remote or len(remote) > 2048 or any(c.isspace() or ord(c) < 32 for c in remote):
        raise ProjectError("Unsupported remote identity; explicit project mapping required")
    scp = re.fullmatch(r"(?:([^/@:]+)@)?([A-Za-z0-9.-]+):([^/].*)", remote)
    scp_style = "://" not in remote
    if scp_style:
        if scp is None:
            raise ProjectError("Unsupported remote identity; explicit project mapping required")
        user, host, path = scp.groups()
        remote = f"ssh://{user or 'git'}@{host}/{path}"
    try:
        parsed = urlsplit(remote)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ProjectError("Invalid remote identity") from None
    if (
        parsed.scheme not in ("https", "ssh")
        or not host
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in parsed.path
    ):
        raise ProjectError("Unsupported remote identity; explicit project mapping required")
    path = parsed.path.removeprefix("/")
    if not path or any(p in ("", ".", "..") for p in path.split("/")):
        raise ProjectError("Invalid remote repository path")
    known = host in ("github.com", "gitlab.com", "bitbucket.org")
    default_port = port is None or port == (443 if parsed.scheme == "https" else 22)
    if known and path.endswith(".git"):
        path = path[:-4]
    if not path:
        raise ProjectError("Invalid remote repository path")
    # Only known hosts and conventional SSH account/path semantics are interchangeable.
    if known and default_port and (parsed.scheme == "https" or parsed.username in (None, "git")):
        return f"repo://{host}/{path}"
    if scp_style:
        # Unknown-host scp paths are relative to the login's home; do not equate to /paths.
        raise ProjectError("Uncertain SSH remote identity; explicit project mapping required")
    if parsed.scheme == "ssh" and parsed.username not in (None, "git"):
        raise ProjectError("Uncertain SSH account identity; explicit project mapping required")
    host_port = f"[{host}]" if ":" in host else host
    if not default_port:
        host_port += f":{port}"
    return f"{parsed.scheme}://{host_port}/{path}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    if executable is None:
        raise ProjectError("Git is unavailable")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", LC_ALL="C"
    )
    try:
        # Fixed metadata commands only, absolute executable, no shell or network operation.
        return subprocess.run(  # nosec B603
            [executable, "-C", str(cwd), "-c", "core.fsmonitor=false", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise ProjectError("Cannot read local Git metadata") from None


def _discover(cwd: Path) -> tuple[str, str | None] | None:
    if not cwd.is_dir():
        raise ProjectError("Project directory does not exist")
    inside = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if inside.returncode:
        if "not a git repository" in inside.stderr:
            if any(
                (parent / ".git").exists() or (parent / ".git").is_symlink()
                for parent in (cwd, *cwd.parents)
            ):
                raise ProjectError("Broken Git metadata requires review")
            return None
        raise ProjectError("Cannot determine repository identity")
    if inside.stdout.strip() != "true":
        raise ProjectError("Bare repositories require explicit project mapping")
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode or not common.stdout.strip():
        raise ProjectError("Cannot determine repository common directory")
    common_dir = str(Path(common.stdout.strip()).resolve(strict=True))
    origin = _git(cwd, "config", "--local", "--no-includes", "--get-all", "remote.origin.url")
    if origin.returncode not in (0, 1):
        raise ProjectError("Cannot determine local origin")
    values = origin.stdout.splitlines()
    if len(values) > 1:
        raise ProjectConflict("Multiple origin identities require review")
    remote = normalize_remote(values[0]) if values else None
    return common_dir, remote


def resolve_project(connection: sqlite3.Connection, cwd: Path) -> ProjectResolution:
    """Resolve and persist aliases atomically; refuses to own a caller's active transaction."""
    if connection.in_transaction:
        raise ProjectError("Project resolution requires an idle connection")
    try:
        discovered = _discover(cwd.expanduser().resolve(strict=True))
    except (OSError, RuntimeError):
        raise ProjectError("Cannot read the project directory") from None
    timestamp = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if discovered is None:
            if not connection.execute("SELECT 1 FROM project WHERE id='global'").fetchone():
                connection.execute(
                    "INSERT INTO project VALUES(?,?,?)", ("global", timestamp, "Global")
                )
            result = ProjectResolution("global", "global")
        else:
            common, remote = discovered
            common_row = connection.execute(
                "SELECT project_id FROM project_alias WHERE kind='common_dir' AND identity=?",
                (common,),
            ).fetchone()
            remote_row = (
                connection.execute(
                    "SELECT project_id FROM project_alias WHERE kind='remote' AND identity=?",
                    (remote,),
                ).fetchone()
                if remote
                else None
            )
            if common_row and remote_row and common_row[0] != remote_row[0]:
                raise ProjectConflict("Common directory and remote identify different projects")
            if common_row and remote:
                previous = connection.execute(
                    "SELECT identity FROM project_alias WHERE kind='remote' AND project_id=?",
                    (common_row[0],),
                ).fetchall()
                if previous and (remote,) not in previous:
                    raise ProjectConflict("Established origin changed; explicit review required")
            existing = common_row or remote_row
            project_id = str(existing[0]) if existing else f"p_{uuid4().hex}"
            if existing is None:
                label = Path(common).parent.name[:160] or "Repository"
                connection.execute(
                    "INSERT INTO project VALUES(?,?,?)", (project_id, timestamp, label)
                )
            for kind, identity in [("common_dir", common), ("remote", remote)]:
                if identity is not None:
                    connection.execute(
                        "INSERT INTO project_alias VALUES(?,?,?,?) ON CONFLICT(kind,identity) "
                        "DO NOTHING",
                        (kind, identity, project_id, timestamp),
                    )
            result = ProjectResolution(
                project_id, "common_dir" if common_row else "remote" if remote_row else "created"
            )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
