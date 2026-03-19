from typing import Any

import yaml

from core import CommandType, NodeCategory, RouteConfig, WorkflowConfig, WorkflowNode


YAML_TYPE_TO_COMMAND = {
    "left_click": CommandType.LEFT_CLICK,
    "left_double_click": CommandType.LEFT_DOUBLE_CLICK,
    "right_click": CommandType.RIGHT_CLICK,
    "input_text": CommandType.INPUT_TEXT,
    "wait": CommandType.WAIT,
    "scroll": CommandType.SCROLL,
    "hotkey": CommandType.HOTKEY,
    "hover": CommandType.HOVER,
    "screenshot": CommandType.SCREENSHOT,
    "route": CommandType.ROUTE,
}


COMMAND_TO_YAML_TYPE = {value: key for key, value in YAML_TYPE_TO_COMMAND.items()}


def _to_command_type(raw_type: Any):
    if isinstance(raw_type, CommandType):
        return raw_type

    text = str(raw_type or "").strip().lower()
    if text in YAML_TYPE_TO_COMMAND:
        return YAML_TYPE_TO_COMMAND[text]

    return CommandType.from_raw(raw_type)


def _to_yaml_type(raw_type: Any):
    cmd = CommandType.from_raw(raw_type)
    return COMMAND_TO_YAML_TYPE.get(cmd, cmd.name.lower())


def _load_route_payload(node_data: dict[str, Any]):
    route_payload = node_data.get("route", node_data.get("routes", None))
    route_config = RouteConfig.from_raw(route_payload)

    # 兼容旧 YAML 的 next 字段
    legacy_next = str(node_data.get("next", "")).strip()
    if not route_config.default and legacy_next:
        route_config.default = legacy_next

    return route_config


def workflow_from_yaml_data(data: Any):
    if not isinstance(data, dict):
        raise ValueError("YAML 根节点必须是对象")

    raw_nodes = data.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise ValueError("YAML nodes 必须是列表")

    nodes: list[WorkflowNode] = []
    for idx, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            raise ValueError(f"YAML 节点第 {idx + 1} 项必须是对象")

        node_id = str(item.get("id", item.get("node_id", idx + 1))).strip() or str(idx + 1)
        node_type = _to_command_type(item.get("type", "left_click"))
        routes_value = _load_route_payload(item)
        value = str(item.get("value", ""))
        timeout_second = float(item.get("timeout_sec", item.get("timeout_second", item.get("retry", 0))))

        nodes.append(
            WorkflowNode(
                node_id=node_id,
                category=NodeCategory.from_raw(item.get("category", NodeCategory.MIDDLE.name)),
                type=node_type,
                value=value,
                timeout_second=timeout_second,
                routes=routes_value,
            )
        )

    start_node = str(data.get("start", data.get("start_node", ""))).strip()
    return WorkflowConfig.from_raw({"start_node": start_node, "nodes": nodes})


def workflow_to_yaml_data(workflow: WorkflowConfig):
    result: dict[str, Any] = {
        "version": 1,
        "start": workflow.start_node,
        "nodes": [],
    }

    for node in workflow.nodes:
        item = {
            "id": node.node_id,
            "category": NodeCategory.from_raw(getattr(node, "category", NodeCategory.MIDDLE)).name,
            "type": _to_yaml_type(node.type),
            "value": node.value,
        }

        if float(node.timeout_second) > 0:
            item["timeout_sec"] = float(node.timeout_second)

        route_payload = RouteConfig.from_raw(getattr(node, "routes", None)).to_dict()
        if route_payload["routes"] or route_payload["default"]:
            item["route"] = route_payload

        if CommandType.from_raw(node.type) == CommandType.ROUTE and "route" in item:
            item.pop("value", None)

        result["nodes"].append(item)

    return result


def load_workflow_from_yaml_file(file_path: str):
    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return workflow_from_yaml_data(data)


def dump_workflow_to_yaml_file(workflow: WorkflowConfig, file_path: str):
    payload = workflow_to_yaml_data(workflow)
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
