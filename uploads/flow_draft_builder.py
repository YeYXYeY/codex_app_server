import json
import os
from datetime import datetime
from typing import Any

from core import CommandType, WorkflowConfig, WorkflowNode


def _parse_iso_ts(ts: str):
    text = str(ts or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def load_session_events(log_root: str, session_id: str):
    file_path = os.path.join(log_root, session_id, "events.jsonl")
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"未找到会话日志: {file_path}")

    events = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                events.append(json.loads(text))
            except Exception:
                continue
    return events


def _select_prior_full_frame(
    full_frames: list[dict[str, Any]],
    action_ts,
    backtrack_sec: float,
):
    if action_ts is None:
        return None

    selected = None
    for frame in full_frames:
        frame_ts = frame.get("_dt")
        if frame_ts is None or frame_ts > action_ts:
            continue

        delta = (action_ts - frame_ts).total_seconds()
        if delta <= float(backtrack_sec):
            selected = frame

    return selected


def _build_normal_target_from_full_frame(
    full_frame_event: dict[str, Any],
    click_payload: dict[str, Any],
    log_root: str,
    session_id: str,
    index: int,
    crop_size: int,
):
    full_payload = full_frame_event.get("payload", {})
    full_path = str(full_payload.get("file", "")).strip()
    if not full_path or not os.path.isfile(full_path):
        return ""

    x = int(click_payload.get("x", 0))
    y = int(click_payload.get("y", 0))

    try:
        from PIL import Image  # type: ignore

        img = Image.open(full_path)
        width, height = img.size
        size = max(40, int(crop_size))
        half = size // 2
        left = max(0, x - half)
        top = max(0, y - half)
        right = min(width, left + size)
        bottom = min(height, top + size)

        if right <= left or bottom <= top:
            return ""

        crop = img.crop((left, top, right, bottom))
        target_dir = os.path.join(log_root, session_id, "normal_targets")
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, f"normal_{index:06d}.png")
        crop.save(target_path)
        return target_path
    except Exception:
        return ""


def _to_action_node(
    event: dict[str, Any],
    index: int,
    full_frames: list[dict[str, Any]] | None = None,
    log_root: str = "",
    session_id: str = "",
    full_backtrack_sec: float = 1.0,
    crop_size: int = 180,
):
    event_type = str(event.get("event_type", "")).strip()
    payload = event.get("payload", {}) if isinstance(event.get("payload", {}), dict) else {}
    node_id = f"auto_{index}"

    if event_type == "mouse_click":
        button = str(payload.get("button", "")).lower()
        target_value = "__RECORDED_CLICK__"

        if full_frames and log_root and session_id:
            selected = _select_prior_full_frame(
                full_frames=full_frames,
                action_ts=event.get("_dt"),
                backtrack_sec=full_backtrack_sec,
            )
            if selected:
                path = _build_normal_target_from_full_frame(
                    full_frame_event=selected,
                    click_payload=payload,
                    log_root=log_root,
                    session_id=session_id,
                    index=index,
                    crop_size=crop_size,
                )
                if path:
                    target_value = path

        if "right" in button:
            return WorkflowNode(node_id=node_id, type=CommandType.RIGHT_CLICK, value=target_value)
        return WorkflowNode(node_id=node_id, type=CommandType.LEFT_CLICK, value=target_value)

    if event_type == "mouse_scroll":
        dy = int(payload.get("dy", 0))
        return WorkflowNode(node_id=node_id, type=CommandType.SCROLL, value=str(dy))

    if event_type == "keyboard_text":
        text = str(payload.get("text", ""))
        return WorkflowNode(node_id=node_id, type=CommandType.INPUT_TEXT, value=text)

    if event_type == "keyboard_key":
        key = str(payload.get("key", ""))
        return WorkflowNode(node_id=node_id, type=CommandType.HOTKEY, value=key)

    return None


def build_draft_workflow_from_events(events: list[dict[str, Any]], wait_threshold_sec: float = 1.2):
    normalized_events = []
    for item in events:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        copied["_dt"] = _parse_iso_ts(str(copied.get("ts", "")))
        normalized_events.append(copied)

    action_events = [
        item for item in normalized_events
        if str(item.get("event_type", "")).strip() in {"mouse_click", "mouse_scroll", "keyboard_text", "keyboard_key"}
    ]

    full_frames = [
        item for item in normalized_events
        if str(item.get("event_type", "")).strip() == "full_frame"
    ]

    action_events = [
        item for item in action_events
        if item.get("_dt") is not None
    ]

    full_frames = [
        item for item in full_frames
        if item.get("_dt") is not None
    ]

    nodes: list[WorkflowNode] = []
    prev_ts = None
    action_index = 1

    for event in action_events:
        cur_ts = event.get("_dt")
        if prev_ts is not None and cur_ts is not None:
            gap = (cur_ts - prev_ts).total_seconds()
            if gap >= float(wait_threshold_sec):
                nodes.append(
                    WorkflowNode(
                        node_id=f"auto_wait_{action_index}",
                        type=CommandType.WAIT,
                        value=f"{gap:.2f}",
                    )
                )

        node = _to_action_node(event, action_index)
        if node is not None:
            nodes.append(node)
            action_index += 1

        if cur_ts is not None:
            prev_ts = cur_ts

    if not nodes:
        raise ValueError("未从事件中识别到可构建节点")

    for idx, node in enumerate(nodes):
        node.routes.default = nodes[idx + 1].node_id if idx + 1 < len(nodes) else ""

    return WorkflowConfig(start_node=nodes[0].node_id, nodes=nodes)


def build_draft_workflow_from_session(log_root: str, session_id: str, wait_threshold_sec: float = 1.2):
    events = load_session_events(log_root, session_id)

    normalized_events = []
    for item in events:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        copied["_dt"] = _parse_iso_ts(str(copied.get("ts", "")))
        normalized_events.append(copied)

    action_events = [
        item for item in normalized_events
        if str(item.get("event_type", "")).strip() in {"mouse_click", "mouse_scroll", "keyboard_text", "keyboard_key"}
    ]
    full_frames = [
        item for item in normalized_events
        if str(item.get("event_type", "")).strip() == "full_frame"
    ]

    nodes: list[WorkflowNode] = []
    prev_ts = None
    action_index = 1

    for event in action_events:
        cur_ts = event.get("_dt")
        if prev_ts is not None and cur_ts is not None:
            gap = (cur_ts - prev_ts).total_seconds()
            if gap >= float(wait_threshold_sec):
                nodes.append(
                    WorkflowNode(
                        node_id=f"auto_wait_{action_index}",
                        type=CommandType.WAIT,
                        value=f"{gap:.2f}",
                    )
                )

        node = _to_action_node(
            event,
            action_index,
            full_frames=full_frames,
            log_root=log_root,
            session_id=session_id,
            full_backtrack_sec=1.0,
            crop_size=180,
        )
        if node is not None:
            nodes.append(node)
            action_index += 1

        if cur_ts is not None:
            prev_ts = cur_ts

    if not nodes:
        raise ValueError("未从事件中识别到可构建节点")

    for idx, node in enumerate(nodes):
        node.routes.default = nodes[idx + 1].node_id if idx + 1 < len(nodes) else ""

    return WorkflowConfig(start_node=nodes[0].node_id, nodes=nodes)
