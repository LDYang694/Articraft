from __future__ import annotations

from typing import Any, Literal, overload

from articraft.agent.tools._core import Reviewer, Tool, ToolContext, ToolResult, result_item
from articraft.agent.tools.compile import TOOL as compile_tool
from articraft.agent.tools.critic import TOOL as critic_tool
from articraft.agent.tools.edit import TOOL as edit_tool
from articraft.agent.tools.exec_command import TOOL as exec_command_tool
from articraft.agent.tools.find_texture import TOOL as find_texture_tool
from articraft.agent.tools.read import TOOL as read_tool
from articraft.agent.tools.sample_color import TOOL as sample_color_tool
from articraft.agent.tools.view_image import TOOL as view_image_tool
from articraft.agent.tools.write import TOOL as write_tool
from articraft.agent.tools.write_stdin import TOOL as write_stdin_tool

TOOLS: dict[str, Tool[Any]] = {
    tool.name: tool
    for tool in (
        read_tool,
        view_image_tool,
        edit_tool,
        write_tool,
        exec_command_tool,
        write_stdin_tool,
        sample_color_tool,
        find_texture_tool,
        critic_tool,
        compile_tool,
    )
}
# All three work from images, so a model that cannot see them cannot use any.
_IMAGE_TOOLS = frozenset({"view_image", "critic", "find_texture"})


def schemas(*, include_images: bool = True) -> list[dict[str, Any]]:
    return [
        tool.schema for tool in TOOLS.values() if include_images or tool.name not in _IMAGE_TOOLS
    ]


@overload
def get(name: Literal["view_image", "critic", "find_texture"]) -> Tool[ToolResult]: ...


@overload
def get(
    name: Literal[
        "read", "edit", "write", "exec_command", "write_stdin", "sample_color", "compile"
    ],
) -> Tool[dict[str, Any]]: ...


@overload
def get(name: str) -> Tool[Any]: ...


def get(name: str) -> Tool[Any]:
    try:
        return TOOLS[name]
    except KeyError as exc:
        raise ValueError(f"unknown tool: {name}") from exc


__all__ = ["Reviewer", "Tool", "ToolContext", "ToolResult", "get", "result_item", "schemas"]
