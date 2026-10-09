"""Accessibility-tree snapshots: a compact, LLM-friendly view of the page with
stable element references (``[ref=e12]``) that map back to DOM nodes.

Built from ``Accessibility.getFullAXTree`` — read-only, no page-visible side
effects, and far cheaper for an agent to consume than raw HTML or pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

INTERACTIVE_ROLES = frozenset(
    {
        "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
        "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab",
        "switch", "slider", "spinbutton", "scrollbar", "treeitem", "gridcell",
    }
)  # fmt: skip
LANDMARK_ROLES = frozenset(
    {
        "navigation", "main", "banner", "contentinfo", "complementary", "form", "search",
        "region", "dialog", "alertdialog", "alert", "article", "section", "heading",
        "list", "listitem", "table", "row", "cell", "columnheader", "rowheader", "img",
        "figure", "menu", "menubar", "tablist", "tabpanel", "tree", "grid", "group",
        "paragraph", "blockquote", "code", "separator", "status", "progressbar", "log",
    }
)  # fmt: skip
_SKIP_ROLES = frozenset({"InlineTextBox", "LineBreak"})
_ROOT_ROLES = frozenset({"RootWebArea", "WebArea"})
_TEXT_ROLES = frozenset({"StaticText", "text"})
_SHOWN_PROPERTIES = (
    "checked", "disabled", "expanded", "focused", "pressed", "required", "selected",
    "readonly", "invalid", "level", "url", "placeholder", "hasPopup",
)  # fmt: skip


@dataclass
class Snapshot:
    text: str
    refs: dict[str, int] = field(default_factory=dict)
    """ref (``e12``) → ``backendDOMNodeId``."""
    truncated: bool = False


def format_ax_tree(nodes: list[dict[str, Any]], *, max_lines: int = 400) -> Snapshot:
    by_id = {node["nodeId"]: node for node in nodes}
    referenced = {child for node in nodes for child in node.get("childIds", [])}
    roots = [node for node in nodes if node["nodeId"] not in referenced]
    lines: list[str] = []
    refs: dict[str, int] = {}
    truncated = False

    def walk(node: dict[str, Any], depth: int) -> None:  # noqa: PLR0912 — one tree walk
        nonlocal truncated
        if truncated:
            return
        role = _value(node.get("role"))
        name = _value(node.get("name")) or ""
        children = [by_id[c] for c in node.get("childIds", []) if c in by_id]
        ignored = node.get("ignored", False)
        if role in _SKIP_ROLES:
            return
        emitted = False
        if not ignored and role in _TEXT_ROLES:
            if name.strip():
                lines.append(f"{'  ' * depth}- text: {_quote(name.strip())}")
                emitted = True
        elif not ignored and (role in INTERACTIVE_ROLES or role in LANDMARK_ROLES or name.strip()):
            if (role in ("none", "generic") and not name.strip()) or role in _ROOT_ROLES:
                emitted = False  # structural wrappers: descend without a line of their own
            else:
                line = f"{'  ' * depth}- {role}"
                if name.strip():
                    line += f" {_quote(name.strip())}"
                props = _properties(node)
                if props:
                    line += " " + " ".join(props)
                backend = node.get("backendDOMNodeId")
                if backend is not None and (role in INTERACTIVE_ROLES or role in LANDMARK_ROLES):
                    ref = f"e{len(refs) + 1}"
                    refs[ref] = int(backend)
                    line += f" [ref={ref}]"
                lines.append(line)
                emitted = True
        if len(lines) >= max_lines:
            truncated = True
            return
        # Buttons/links already carry their text in `name`; skip duplicate text children.
        if (
            emitted
            and role in INTERACTIVE_ROLES
            and all(_value(c.get("role")) in _TEXT_ROLES | _SKIP_ROLES for c in children)
        ):
            return
        for child in children:
            walk(child, depth + (1 if emitted else 0))

    for root in roots:
        walk(root, 0)
    if truncated:
        lines.append(f"- … truncated at {max_lines} lines; narrow down with a selector")
    return Snapshot("\n".join(lines), refs, truncated)


def _value(field_value: Any) -> str:
    if isinstance(field_value, dict):
        field_value = field_value.get("value")
    if field_value is None:
        return ""
    if isinstance(field_value, bool):
        return "true" if field_value else "false"
    return str(field_value)


def _quote(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > 120:
        text = text[:117] + "…"
    return '"' + text.replace('"', '\\"') + '"'


def _properties(node: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for prop in node.get("properties", []) or []:
        name = prop.get("name")
        if name not in _SHOWN_PROPERTIES:
            continue
        value = _value(prop.get("value"))
        if value in ("false", "", "none"):
            continue
        out.append(f"[{name}]" if value == "true" else f"[{name}={_quote(value)}]")
    value = _value(node.get("value"))
    if value:
        out.append(f"[value={_quote(value)}]")
    return out
