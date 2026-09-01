from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from articraft import package_dir
from articraft.agent.tools._exec import ExecSessions
from articraft.agent.tools._paths import scoped_path
from articraft.compiler.result import CompilePayload

SDK_DOCS_ROOT = package_dir / "sdk" / "docs"
WORKSPACE_SDK_DOCS_ROOT = Path("docs") / "sdk"
REVIEW_DIR = Path("review")
"""Everything `critic` writes, kept out of the digest below."""
IGNORED_WORKSPACE_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    "__pycache__",
    "docs",
}
IGNORED_WORKSPACE_SUFFIXES = {".pyc", ".pyo", ".swp", ".swo"}


class Compiler(Protocol):
    """The one thing tools need from a Workspace.

    Tools never create runs, so they are given the narrower capability rather
    than the whole protocol. A ``Workspace`` satisfies this structurally.
    """

    def compile_path(self, run_dir: Path | str) -> CompilePayload: ...


class Reviewer(Protocol):
    """The one thing the critic tool needs from a model: one answer.

    A ``Model`` satisfies this structurally. The critic builds one of these per
    review and closes it, because a provider that chains its requests keeps
    conversation state: a shared client would splice reviews into the run's own
    conversation, and successive reviews would see each other's verdicts and
    repeat them instead of judging what is in front of them.
    """

    async def query(self, messages: list[dict[str, Any]]) -> dict[str, Any]: ...
    async def close(self) -> None: ...


@dataclass
class ToolContext:
    compiler: Compiler
    run_dir: Path
    workspace: Path
    """The directory the agent edits: ``run_dir / "workspace"``."""
    new_reviewer: Callable[[], Reviewer] | None = None
    """Builds the critic's model. A factory, so each review starts clean."""
    blender: str | None = None
    """Blender's executable, for the renders the critic grades."""
    textures: bool = False
    """Whether this run exports texture maps, which decides what a review asks."""
    compile_result: CompilePayload | None = None
    successful_compile_result: CompilePayload | None = None
    successful_compile_digest: str | None = None
    last_compile_failure_signature: str | None = None
    consecutive_compile_failures: int = 0
    review_cost: float = 0.0
    review_token_usage: dict[str, int] = field(default_factory=dict)
    review_scores: list[float] = field(default_factory=list)
    """One score per finished review, so the critic can see it stalling."""
    exec_sessions: ExecSessions = field(default_factory=ExecSessions)

    def refresh_compile_freshness(self) -> bool:
        return (
            self.successful_compile_result is not None
            and self.successful_compile_digest is not None
            and workspace_digest(self.workspace) == self.successful_compile_digest
        )

    def add_review_usage(self, response: Mapping[str, Any]) -> None:
        """Charge a review to the run, so a record shows what the run cost."""

        with contextlib.suppress(TypeError, ValueError):
            self.review_cost += float(response.get("cost") or 0.0)
        usage = response.get("token_usage")
        if isinstance(usage, Mapping):
            for key, value in usage.items():
                try:
                    self.review_token_usage[str(key)] = self.review_token_usage.get(
                        str(key), 0
                    ) + int(value)
                except (TypeError, ValueError):
                    continue


@dataclass(frozen=True)
class ToolResult:
    output: dict[str, Any]
    content_items: list[dict[str, Any]]


ToolOutput = TypeVar("ToolOutput", covariant=True)


@dataclass(frozen=True)
class Tool(Generic[ToolOutput]):
    name: str
    schema: dict[str, Any]
    run: Callable[[ToolContext, dict[str, Any]], Awaitable[ToolOutput]]
    supports_parallel: bool = False


def schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "strict": False,
    }


def reference_image(workspace: Path) -> Path | None:
    """The photograph the run was given, when it was given one."""

    for suffix in (".png", ".jpg", ".jpeg", ".webp"):
        candidate = workspace / f"reference{suffix}"
        if candidate.is_file():
            return candidate
    return None


def workspace_path(workspace: Path, raw: str) -> Path:
    return scoped_path(workspace, raw, "run workspace")


def readable_path(workspace: Path, raw: str) -> Path:
    try:
        return workspace_path(workspace, raw)
    except ValueError as workspace_error:
        target = Path(raw)
        if target.is_absolute():
            raise workspace_error from None

        parts = target.parts
        if len(parts) < 2 or parts[:2] != WORKSPACE_SDK_DOCS_ROOT.parts:
            raise workspace_error from None
        if any(part in {"", ".", ".."} for part in parts):
            raise workspace_error from None

        docs_root = SDK_DOCS_ROOT.resolve()
        docs_target = docs_root.joinpath(*parts[2:]).resolve()
        try:
            docs_target.relative_to(docs_root)
        except ValueError:
            raise workspace_error from None
        return docs_target


def display_path(workspace: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        docs_root = SDK_DOCS_ROOT.resolve()
        return (WORKSPACE_SDK_DOCS_ROOT / path.resolve().relative_to(docs_root)).as_posix()


def result_item(
    call_id: str,
    payload: dict[str, Any],
    *,
    content_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    text = json.dumps(payload)
    output: str | list[dict[str, Any]] = text
    if content_items is not None:
        output = [{"type": "input_text", "text": text}, *content_items]
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def workspace_digest(workspace: Path) -> str:
    """Return a stable digest of workspace paths, links, and file contents.

    The digest decides whether a compile still describes the workspace, so it
    covers everything a compile reads. `review/` is the one directory left out:
    a review's renders come from the USDZ a compile already wrote, and its copy
    of the best script is a backup, so counting either would make every review
    stale the compile it graded.
    """

    root = workspace.resolve()
    digest = hashlib.sha256()

    def add_directory(directory: Path) -> None:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
        for entry in ordered:
            path = Path(entry.path)
            relative_path = path.relative_to(root)
            if _ignore_workspace_path(
                relative_path,
                is_directory=entry.is_dir(follow_symlinks=False),
                is_symlink=entry.is_symlink(),
            ):
                continue
            relative = relative_path.as_posix().encode("utf-8", errors="surrogateescape")
            if entry.is_symlink():
                digest.update(b"L\0" + relative + b"\0")
                digest.update(str(path.readlink()).encode("utf-8", errors="surrogateescape"))
                digest.update(b"\0")
                target = path.resolve(strict=True)
                if not target.is_file():
                    raise ValueError(
                        "workspace directory symlinks outside docs are not supported: "
                        f"{relative_path.as_posix()}"
                    )
                with target.open("rb") as file:
                    while chunk := file.read(1024 * 1024):
                        digest.update(chunk)
                digest.update(b"\0")
            elif entry.is_dir(follow_symlinks=False):
                digest.update(b"D\0" + relative + b"\0")
                add_directory(path)
            elif entry.is_file(follow_symlinks=False):
                digest.update(b"F\0" + relative + b"\0")
                with path.open("rb") as file:
                    while chunk := file.read(1024 * 1024):
                        digest.update(chunk)
                digest.update(b"\0")
            else:
                digest.update(b"O\0" + relative + b"\0")

    add_directory(root)
    return digest.hexdigest()


def _ignore_workspace_path(path: Path, *, is_directory: bool, is_symlink: bool) -> bool:
    name = path.name
    if path.parts[0] == "docs":
        return True
    if is_directory and path == REVIEW_DIR:
        return True
    if (is_directory or is_symlink) and (
        name in IGNORED_WORKSPACE_DIRECTORIES or name.endswith((".egg-info", ".dist-info"))
    ):
        return True
    if name in {".DS_Store", ".coverage"}:
        return True
    if name.endswith("~") or (name.startswith("#") and name.endswith("#")):
        return True
    if name.startswith((".#", ".~lock.")):
        return True
    return path.suffix.lower() in IGNORED_WORKSPACE_SUFFIXES
