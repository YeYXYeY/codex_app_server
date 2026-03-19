import enum
import json
from dataclasses import dataclass, field
from typing import Any


class NodeCategory(enum.Enum):
    START = "开始节点"
    END = "结束节点"
    MIDDLE = "中间节点"

    @classmethod
    def from_raw(cls, raw_value: Any):
        if isinstance(raw_value, cls):
            return raw_value

        text = str(raw_value or "").strip()
        if not text:
            return cls.MIDDLE

        upper_text = text.upper()
        if upper_text in cls.__members__:
            return cls[upper_text]

        for category in cls:
            if text == category.value:
                return category

        return cls.MIDDLE


class CommandType(enum.Enum):
    LEFT_CLICK = 1.0
    LEFT_DOUBLE_CLICK = 2.0
    RIGHT_CLICK = 3.0
    INPUT_TEXT = 4.0
    WAIT = 5.0
    SCROLL = 6.0
    HOTKEY = 7.0
    HOVER = 8.0
    SCREENSHOT = 9.0
    ROUTE = 10.0

    @classmethod
    def ordered(cls):
        return [
            cls.LEFT_CLICK,
            cls.LEFT_DOUBLE_CLICK,
            cls.RIGHT_CLICK,
            cls.INPUT_TEXT,
            cls.WAIT,
            cls.SCROLL,
            cls.HOTKEY,
            cls.HOVER,
            cls.SCREENSHOT,
            cls.ROUTE,
        ]

    @property
    def label(self):
        return {
            CommandType.LEFT_CLICK: "左键单击",
            CommandType.LEFT_DOUBLE_CLICK: "左键双击",
            CommandType.RIGHT_CLICK: "右键单击",
            CommandType.INPUT_TEXT: "输入文本",
            CommandType.WAIT: "等待(秒)",
            CommandType.SCROLL: "滚轮滚动",
            CommandType.HOTKEY: "系统按键",
            CommandType.HOVER: "鼠标悬停",
            CommandType.SCREENSHOT: "截图保存",
            CommandType.ROUTE: "路由判断",
        }[self]

    @classmethod
    def from_label(cls, label: str):
        normalized = (label or "").strip()
        for cmd in cls.ordered():
            if cmd.label == normalized:
                return cmd
        raise ValueError(f"未知操作类型文本: {label}")

    @classmethod
    def from_raw(cls, raw_type: Any):
        if isinstance(raw_type, cls):
            return raw_type

        if isinstance(raw_type, str):
            normalized = raw_type.strip()
            if normalized:
                try:
                    return cls.from_label(normalized)
                except ValueError:
                    pass

        try:
            return cls(float(raw_type))
        except Exception as e:
            raise ValueError(f"未知操作类型值: {raw_type}") from e


@dataclass
class RouteConfig:
    # pattern -> target node_id
    routes: dict[str, str] = field(default_factory=dict)
    default: str = ""
    confidence: float = 0.8

    @classmethod
    def from_raw(cls, raw_value: Any):
        if isinstance(raw_value, cls):
            return cls(
                routes=dict(raw_value.routes),
                default=str(raw_value.default).strip(),
                confidence=float(raw_value.confidence),
            )

        if raw_value is None:
            return cls()

        if isinstance(raw_value, str):
            text = raw_value.strip()
            if not text:
                return cls()
            try:
                payload = json.loads(text)
            except Exception as e:
                raise ValueError("路由配置需为合法 JSON") from e
        elif isinstance(raw_value, dict):
            payload = raw_value
        else:
            raise ValueError(f"不支持的路由配置类型: {type(raw_value)}")

        if not isinstance(payload, dict):
            raise ValueError("路由配置必须是 JSON 对象")

        mapping: dict[str, str] = {}
        routes_raw = payload.get("routes")
        if routes_raw is None and isinstance(payload.get("patterns"), dict):
            routes_raw = payload.get("patterns")

        if isinstance(routes_raw, list):
            for i, item in enumerate(routes_raw):
                if not isinstance(item, dict):
                    raise ValueError(f"routes 第 {i + 1} 项必须是对象")
                pattern = str(item.get("pattern", "")).strip()
                target = str(item.get("node", item.get("target", ""))).strip()
                if not pattern or not target:
                    raise ValueError(f"routes 第 {i + 1} 项缺少 pattern 或 node")
                mapping[pattern] = target
        elif isinstance(routes_raw, dict):
            for pattern, target in routes_raw.items():
                pattern_text = str(pattern).strip()
                target_text = str(target).strip()
                if not pattern_text or not target_text:
                    raise ValueError("routes 字典的 pattern 或 target 不能为空")
                mapping[pattern_text] = target_text
        elif routes_raw is not None:
            raise ValueError("routes 必须是列表或字典")

        default_node = str(payload.get("default", "")).strip()
        confidence = float(payload.get("confidence", 0.8))
        if confidence <= 0 or confidence > 1:
            raise ValueError("confidence 取值范围应为 (0, 1]")

        return cls(routes=mapping, default=default_node, confidence=confidence)

    def to_dict(self):
        return {
            "routes": dict(self.routes),
            "default": str(self.default).strip(),
            "confidence": float(self.confidence),
        }


@dataclass
class WorkflowNode:
    node_id: str
    type: CommandType
    value: str
    timeout_second: float = 0
    category: NodeCategory = NodeCategory.MIDDLE
    routes: RouteConfig = field(default_factory=RouteConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int = 0, total: int = 0):
        node_id = str(data.get("node_id", data.get("id", index + 1))).strip() or str(index + 1)
        node_type = CommandType.from_raw(data.get("type", CommandType.LEFT_CLICK.value))
        node_value = str(data.get("value", ""))
        route_config = RouteConfig.from_raw(data.get("routes", data.get("route", None)))
        legacy_next = str(data.get("next", "")).strip()
        if not route_config.default and legacy_next:
            route_config.default = legacy_next

        if node_type == CommandType.ROUTE and not route_config.routes and not route_config.default:
            try:
                route_config = RouteConfig.from_raw(node_value)
            except Exception:
                pass

        return cls(
            node_id=node_id,
            type=node_type,
            value=node_value,
            timeout_second=float(data.get("timeout_second", data.get("retry", 0))),
            category=NodeCategory.from_raw(data.get("category", NodeCategory.MIDDLE.name)),
            routes=route_config,
        )

    def to_dict(self):
        return {
            "node_id": self.node_id,
            "category": self.category.name,
            "type": self.type.value,
            "value": self.value,
            "timeout_second": self.timeout_second,
            "routes": self.routes.to_dict(),
        }


@dataclass
class WorkflowConfig:
    start_node: str
    nodes: list[WorkflowNode]

    @classmethod
    def from_raw(cls, data: Any):
        if isinstance(data, cls):
            return data

        if isinstance(data, dict):
            raw_nodes = data.get("nodes", [])
            start_node = str(data.get("start_node", data.get("start", ""))).strip()
        else:
            raw_nodes = data or []
            start_node = ""

        if not isinstance(raw_nodes, list):
            raise ValueError("配置格式错误: nodes 必须是列表")

        nodes: list[WorkflowNode] = []
        for idx, node in enumerate(raw_nodes):
            if isinstance(node, WorkflowNode):
                current = WorkflowNode(
                    node_id=str(node.node_id).strip() or str(idx + 1),
                    category=NodeCategory.from_raw(getattr(node, "category", NodeCategory.MIDDLE)),
                    type=CommandType.from_raw(node.type),
                    value=str(node.value),
                    timeout_second=float(getattr(node, "timeout_second", 0)),
                    routes=RouteConfig.from_raw(getattr(node, "routes", None)),
                )
            elif isinstance(node, dict):
                current = WorkflowNode.from_dict(node, idx, len(raw_nodes))
            else:
                raise ValueError(f"节点格式错误: 第 {idx + 1} 项不是对象")
            nodes.append(current)

        used_ids: set[str] = set()
        for idx, node in enumerate(nodes):
            base_id = str(node.node_id).strip() or str(idx + 1)
            unique_id = base_id
            suffix = 2
            while unique_id in used_ids:
                unique_id = f"{base_id}_{suffix}"
                suffix += 1

            node.node_id = unique_id
            used_ids.add(unique_id)

        for idx, node in enumerate(nodes):
            if idx + 1 >= len(nodes):
                break
            if not node.routes.default and not node.routes.routes:
                node.routes.default = nodes[idx + 1].node_id

        if not nodes:
            raise ValueError("没有可执行的节点")

        if not start_node:
            start_node = nodes[0].node_id

        if start_node not in used_ids:
            start_node = nodes[0].node_id

        return cls(start_node=start_node, nodes=nodes)

    def to_dict(self):
        return {
            "start_node": self.start_node,
            "nodes": [node.to_dict() for node in self.nodes],
        }


def parse_route_value(raw_value: Any):
    """
    路由配置格式（JSON 字符串或 dict）:
    {
      "routes": {
        "a.png": "A",
        "b.png": "B"
      },
      "default": "C",
      "confidence": 0.8
    }

    兼容旧格式:
    {
      "routes": [
        {"pattern": "a.png", "node": "A"},
        {"pattern": "b.png", "node": "B"}
      ],
      "default": "C",
      "confidence": 0.8
    }
    """
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        raise ValueError("路由节点参数不能为空")

    route_config = RouteConfig.from_raw(raw_value)
    if not route_config.routes and not route_config.default:
        raise ValueError("路由节点缺少 routes 或 default 配置")

    return list(route_config.routes.items()), route_config.default, route_config.confidence


def parse_node_routes(node: WorkflowNode):
    route_config = RouteConfig.from_raw(getattr(node, "routes", None))
    if route_config.routes or route_config.default:
        return list(route_config.routes.items()), route_config.default, route_config.confidence

    node_type = CommandType.from_raw(node.type)
    if node_type == CommandType.ROUTE:
        return parse_route_value(node.value)

    return [], "", 0.8


def validate_workflow_config(workflow: WorkflowConfig):
    node_ids = []
    for idx, node in enumerate(workflow.nodes):
        node_id = str(node.node_id).strip()
        if not node_id:
            return f"第 {idx + 1} 行缺少节点ID"
        node_ids.append(node_id)

    if len(node_ids) != len(set(node_ids)):
        return "节点ID存在重复"

    node_id_set = set(node_ids)

    if str(workflow.start_node).strip() not in node_id_set:
        return f"起始节点不存在: {workflow.start_node}"

    for idx, node in enumerate(workflow.nodes):
        try:
            routes, default_node, _confidence = parse_node_routes(node)
        except Exception as e:
            return f"第 {idx + 1} 行路由配置错误: {e}"

        for pattern, target_node in routes:
            if target_node not in node_id_set:
                return f"第 {idx + 1} 行路由目标节点不存在: {target_node} (pattern={pattern})"

        if default_node and default_node not in node_id_set:
            return f"第 {idx + 1} 行路由默认节点不存在: {default_node}"

    return ""
