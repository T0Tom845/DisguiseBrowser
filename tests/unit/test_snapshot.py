from disguisebrowser.snapshot import format_ax_tree


def _node(node_id: str, role: str, name: str = "", **extra: object) -> dict[str, object]:
    node: dict[str, object] = {
        "nodeId": node_id,
        "role": {"type": "role", "value": role},
        "name": {"type": "computedString", "value": name},
        "childIds": [],
    }
    node.update(extra)
    return node


def test_format_ax_tree_emits_refs_for_interactive_and_landmark_nodes() -> None:
    nodes = [
        _node("1", "RootWebArea", "Shop", childIds=["2", "3", "6"]),
        _node(
            "2",
            "heading",
            "Shop",
            backendDOMNodeId=10,
            properties=[{"name": "level", "value": {"value": 1}}],
        ),
        _node("3", "generic", childIds=["4", "5"], backendDOMNodeId=11),
        _node("4", "link", "Cart", backendDOMNodeId=12, childIds=["7"]),
        _node("5", "StaticText", "3 items"),
        _node(
            "6",
            "textbox",
            "Search",
            backendDOMNodeId=13,
            value={"type": "string", "value": "shoes"},
            properties=[
                {"name": "focused", "value": {"value": True}},
                {"name": "required", "value": {"value": False}},
            ],
        ),
        _node("7", "StaticText", "Cart"),
    ]
    snap = format_ax_tree(nodes)
    assert snap.text.splitlines() == [
        '- heading "Shop" [level="1"] [ref=e1]',
        '- link "Cart" [ref=e2]',
        '- text: "3 items"',
        '- textbox "Search" [focused] [value="shoes"] [ref=e3]',
    ]
    assert snap.refs == {"e1": 10, "e2": 12, "e3": 13}
    assert not snap.truncated


def test_format_ax_tree_skips_ignored_and_truncates() -> None:
    nodes = [_node("root", "RootWebArea", childIds=[str(i) for i in range(1, 11)])]
    nodes += [_node(str(i), "button", f"b{i}", backendDOMNodeId=i) for i in range(1, 11)]
    nodes[3]["ignored"] = True
    snap = format_ax_tree(nodes, max_lines=5)
    assert snap.truncated
    lines = snap.text.splitlines()
    assert len(lines) == 6 and lines[-1].startswith("- … truncated")
    assert 'button "b3"' not in snap.text
