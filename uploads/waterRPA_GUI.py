# 引用自 rpa_gui.py，进行了重构和功能增强，新增了自动布局、流程箭头、日志输出等功能，并与核心逻辑和线程进行了更好的集成。
# @see https://github.com/unfiled0/waterRPA

import sys
import os
import json
from typing import Dict, List
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QPushButton, QLabel, QComboBox, QLineEdit, QScrollArea, 
                               QFileDialog, QTextEdit, QMessageBox, QFrame, QDialog,
                               QDialogButtonBox)
from PySide6.QtCore import Qt, Signal, QEvent, QPointF
from PySide6.QtGui import QPainter, QPen, QColor, QPolygonF, QPixmap

from core import CommandType, NodeCategory, RouteConfig, WorkflowConfig, WorkflowNode, parse_route_value, validate_workflow_config
from flow_draft_builder import build_draft_workflow_from_session
from input_recorder import InputRecorder
from workflow_yaml import load_workflow_from_yaml_file, dump_workflow_to_yaml_file
from worker import RPAEngine, WorkerThread

# --------------------------
# GUI 界面 (原 rpa_gui.py)
# --------------------------


class RouteRuleRow(QWidget):
    def __init__(self, node_ids: List[str], remove_callback, pattern: str = "", node: str = ""):
        super().__init__()
        self.remove_callback = remove_callback

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.pattern_input = QLineEdit()
        self.pattern_input.setPlaceholderText("pattern 图片路径")
        self.pattern_input.setText(pattern)
        layout.addWidget(self.pattern_input)

        self.pick_btn = QPushButton("选择图片")
        self.pick_btn.setFixedWidth(72)
        self.pick_btn.clicked.connect(self.pick_image)
        layout.addWidget(self.pick_btn)

        self.node_combo = QComboBox()
        self.node_combo.setEditable(True)
        self.node_combo.addItems(node_ids)
        if node:
            idx = self.node_combo.findText(node)
            if idx >= 0:
                self.node_combo.setCurrentIndex(idx)
            else:
                self.node_combo.setEditText(node)
        self.node_combo.setFixedWidth(100)
        layout.addWidget(self.node_combo)

        self.remove_btn = QPushButton("删除")
        self.remove_btn.setFixedWidth(50)
        self.remove_btn.clicked.connect(lambda: self.remove_callback(self))
        layout.addWidget(self.remove_btn)

    def pick_image(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择图片", os.getcwd(), "Image Files (*.png *.jpg *.bmp)")
        if filename:
            self.pattern_input.setText(filename)

    def to_dict(self):
        return {
            "pattern": self.pattern_input.text().strip(),
            "node": self.node_combo.currentText().strip(),
        }


class RouteEditorDialog(QDialog):
    def __init__(self, node_ids: List[str], raw_value: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑路由配置")
        self.resize(760, 420)
        self.node_ids = node_ids
        self.rule_rows: List[RouteRuleRow] = []

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel("路由项: pattern图片 -> 目标节点"))
        header.addStretch()
        self.add_rule_btn = QPushButton("+ 添加路由项")
        self.add_rule_btn.clicked.connect(lambda: self.add_rule_row())
        header.addWidget(self.add_rule_btn)
        layout.addLayout(header)

        self.rules_container = QWidget()
        self.rules_layout = QVBoxLayout(self.rules_container)
        self.rules_layout.setContentsMargins(0, 0, 0, 0)
        self.rules_layout.setSpacing(6)
        self.rules_layout.addStretch()

        self.rules_scroll = QScrollArea()
        self.rules_scroll.setWidgetResizable(True)
        self.rules_scroll.setWidget(self.rules_container)
        layout.addWidget(self.rules_scroll)

        footer = QHBoxLayout()
        footer.addWidget(QLabel("默认节点"))
        self.default_combo = QComboBox()
        self.default_combo.setEditable(True)
        self.default_combo.addItem("")
        self.default_combo.addItems(node_ids)
        self.default_combo.setFixedWidth(130)
        footer.addWidget(self.default_combo)

        footer.addWidget(QLabel("confidence"))
        self.confidence_input = QLineEdit("0.8")
        self.confidence_input.setFixedWidth(80)
        footer.addWidget(self.confidence_input)
        footer.addStretch()
        layout.addLayout(footer)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.load_from_raw(raw_value)

    def add_rule_row(self, pattern: str = "", node: str = ""):
        self.rules_layout.takeAt(self.rules_layout.count() - 1)
        row = RouteRuleRow(self.node_ids, self.remove_rule_row, pattern=pattern, node=node)
        self.rule_rows.append(row)
        self.rules_layout.addWidget(row)
        self.rules_layout.addStretch()

    def remove_rule_row(self, row_widget: RouteRuleRow):
        if row_widget in self.rule_rows:
            self.rule_rows.remove(row_widget)
            row_widget.deleteLater()

    def load_from_raw(self, raw_value: str):
        loaded = False
        try:
            routes, default_node, confidence = parse_route_value(raw_value)
            for pattern, node in routes:
                self.add_rule_row(pattern=pattern, node=node)
            self.default_combo.setEditText(default_node)
            self.confidence_input.setText(str(confidence))
            loaded = True
        except Exception:
            pass

        if not loaded:
            self.add_rule_row()

    def get_route_payload(self):
        routes: dict[str, str] = {}
        for idx, row in enumerate(self.rule_rows):
            item = row.to_dict()
            if not item["pattern"] or not item["node"]:
                raise ValueError(f"第 {idx+1} 个路由项的图片或目标节点为空")
            routes[item["pattern"]] = item["node"]

        if not routes:
            raise ValueError("至少需要一个路由项")

        try:
            confidence = float(self.confidence_input.text().strip())
        except ValueError as e:
            raise ValueError("confidence 必须是数字") from e

        payload = {
            "routes": routes,
            "default": self.default_combo.currentText().strip(),
            "confidence": confidence,
        }
        parse_route_value(payload)
        return payload


class CaptureGalleryDialog(QDialog):
    def __init__(self, image_files: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("录制图逐个确认")
        self.resize(760, 620)
        self.image_files = image_files
        self.current_index = 0
        self.selected_file = ""

        layout = QVBoxLayout(self)

        self.info_label = QLabel("")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.info_label)

        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(480)
        self.preview_label.setStyleSheet("border: 1px solid #8f9faf; background: #f5f8fc;")
        layout.addWidget(self.preview_label)

        button_row = QHBoxLayout()

        self.prev_btn = QPushButton("上一张")
        self.prev_btn.clicked.connect(self.show_prev)
        button_row.addWidget(self.prev_btn)

        self.next_btn = QPushButton("下一张")
        self.next_btn.clicked.connect(self.show_next)
        button_row.addWidget(self.next_btn)

        button_row.addStretch()

        self.pick_btn = QPushButton("选择当前图")
        self.pick_btn.clicked.connect(self.select_current)
        button_row.addWidget(self.pick_btn)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(self.cancel_btn)

        layout.addLayout(button_row)

        self.refresh_view()

    def refresh_view(self):
        if not self.image_files:
            self.info_label.setText("没有可用录制图")
            self.preview_label.setText("无图片")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.pick_btn.setEnabled(False)
            return

        self.current_index = max(0, min(self.current_index, len(self.image_files) - 1))
        file_path = self.image_files[self.current_index]
        self.info_label.setText(f"{self.current_index + 1} / {len(self.image_files)}    {file_path}")

        pix = QPixmap(file_path)
        if pix.isNull():
            self.preview_label.setText("图片读取失败")
            self.preview_label.setPixmap(QPixmap())
        else:
            scaled = pix.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.preview_label.setPixmap(scaled)

        self.prev_btn.setEnabled(self.current_index > 0)
        self.next_btn.setEnabled(self.current_index < len(self.image_files) - 1)
        self.pick_btn.setEnabled(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_view()

    def show_prev(self):
        if self.current_index > 0:
            self.current_index -= 1
            self.refresh_view()

    def show_next(self):
        if self.current_index < len(self.image_files) - 1:
            self.current_index += 1
            self.refresh_view()

    def select_current(self):
        if not self.image_files:
            return
        self.selected_file = self.image_files[self.current_index]
        self.accept()


def collect_capture_images(capture_dir: str):
    if not capture_dir or not os.path.isdir(capture_dir):
        return []

    files = []
    for name in sorted(os.listdir(capture_dir)):
        lower = name.lower()
        if lower.endswith(".png") or lower.endswith(".jpg") or lower.endswith(".bmp"):
            files.append(os.path.join(capture_dir, name))
    return files


def parse_row_routes(row):
    route_text = row.route_value_input.text().strip()
    if route_text:
        return parse_route_value(route_text)

    if CommandType.from_label(row.type_combo.currentText()) == CommandType.ROUTE:
        return parse_route_value(row.value_input.text())

    return [], row.default_input.text().strip(), 0.8


class RuntimeInsertDialog(QDialog):
    def __init__(self, node_ids: List[str], current_node_id: str = "", capture_dir: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("运行中在线插入")
        self.resize(560, 220)
        self.capture_dir = capture_dir

        layout = QVBoxLayout(self)

        anchor_layout = QHBoxLayout()
        anchor_layout.addWidget(QLabel("锚点节点"))
        self.anchor_combo = QComboBox()
        self.anchor_combo.setEditable(True)
        self.anchor_combo.addItems(node_ids)
        if current_node_id:
            self.anchor_combo.setEditText(current_node_id)
        anchor_layout.addWidget(self.anchor_combo)
        layout.addLayout(anchor_layout)

        node_id_layout = QHBoxLayout()
        node_id_layout.addWidget(QLabel("新节点ID"))
        self.node_id_input = QLineEdit()
        self.node_id_input.setPlaceholderText("例如: draft_1")
        node_id_layout.addWidget(self.node_id_input)
        layout.addLayout(node_id_layout)

        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel("类型"))
        self.type_combo = QComboBox()
        self.type_combo.addItems([cmd.label for cmd in CommandType.ordered()])
        type_layout.addWidget(self.type_combo)
        layout.addLayout(type_layout)

        value_layout = QHBoxLayout()
        value_layout.addWidget(QLabel("参数"))
        self.value_input = QLineEdit()
        self.value_input.setPlaceholderText("如图片路径/文本/等待秒数")
        value_layout.addWidget(self.value_input)
        self.pick_capture_btn = QPushButton("录制图")
        self.pick_capture_btn.clicked.connect(self.pick_capture_image)
        value_layout.addWidget(self.pick_capture_btn)
        layout.addLayout(value_layout)

        timeout_layout = QHBoxLayout()
        timeout_layout.addWidget(QLabel("超时(秒)"))
        self.timeout_input = QLineEdit("30.0")
        timeout_layout.addWidget(self.timeout_input)
        layout.addLayout(timeout_layout)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def get_patch(self):
        anchor = self.anchor_combo.currentText().strip()
        if not anchor:
            raise ValueError("锚点节点不能为空")

        node_id = self.node_id_input.text().strip() or "draft"
        cmd_type = CommandType.from_label(self.type_combo.currentText())
        value = self.value_input.text()
        timeout_second = float(self.timeout_input.text().strip() or "30.0")

        node = WorkflowNode(
            node_id=node_id,
            type=cmd_type,
            value=value,
            timeout_second=timeout_second,
        )

        return {
            "anchor_node_id": anchor,
            "nodes": [node],
            "effective_after": anchor,
        }

    def pick_capture_image(self):
        capture_files = collect_capture_images(self.capture_dir)
        if capture_files:
            dialog = CaptureGalleryDialog(capture_files, parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_file:
                self.value_input.setText(dialog.selected_file)
                return

        init_dir = self.capture_dir if self.capture_dir and os.path.isdir(self.capture_dir) else os.getcwd()
        filename, _ = QFileDialog.getOpenFileName(self, "选择录制图片", init_dir, "Image Files (*.png *.jpg *.bmp)")
        if filename:
            self.value_input.setText(filename)


class DraftPreviewDialog(QDialog):
    def __init__(self, workflow: WorkflowConfig, session_id: str, parent=None):
        super().__init__(parent)
        self._mode = ""
        self.setWindowTitle("草稿预览与确认")
        self.resize(760, 480)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"会话: {session_id}"))
        layout.addWidget(QLabel(f"草稿节点数: {len(workflow.nodes)}，起始节点: {workflow.start_node}"))

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlainText(self._render_preview(workflow))
        layout.addWidget(self.preview)

        button_row = QHBoxLayout()
        button_row.addStretch()

        self.replace_btn = QPushButton("替换当前")
        self.replace_btn.clicked.connect(self._accept_replace)
        button_row.addWidget(self.replace_btn)

        self.append_btn = QPushButton("追加到末尾")
        self.append_btn.clicked.connect(self._accept_append)
        button_row.addWidget(self.append_btn)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(self.cancel_btn)

        layout.addLayout(button_row)

    def _render_preview(self, workflow: WorkflowConfig):
        lines = []
        for idx, node in enumerate(workflow.nodes, start=1):
            node_type = CommandType.from_raw(node.type)
            default_target = RouteConfig.from_raw(getattr(node, "routes", None)).default
            lines.append(
                f"{idx:>3}. id={node.node_id} | type={node_type.label} | value={str(node.value)} | default={default_target}"
            )
        return "\n".join(lines)

    def _accept_replace(self):
        self._mode = "replace"
        self.accept()

    def _accept_append(self):
        self._mode = "append"
        self.accept()

    def apply_mode(self):
        return self._mode

class TaskRow(QFrame):
    changed = Signal()

    def __init__(self, parent_widget, delete_callback, get_node_ids_callback, get_capture_dir_callback):
        super().__init__(parent_widget)
        self.get_node_ids_callback = get_node_ids_callback
        self.get_capture_dir_callback = get_capture_dir_callback
        self._default_bg_color = "#cfdfef"
        self._visited_bg_color = "#fff59d"
        self._is_visited = False
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._apply_frame_style(self._default_bg_color)
        self.setMinimumHeight(120)

        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(10, 10, 10, 10)
        self.layout_.setSpacing(8)

        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel("节点"))

        self.node_id_input = QLineEdit()
        self.node_id_input.setPlaceholderText("ID")
        self.node_id_input.setFixedWidth(80)
        header_layout.addWidget(self.node_id_input)

        self.category_label = QLabel(NodeCategory.MIDDLE.value)
        self.category_label.setStyleSheet("color: #2d4f7c; background: #d9ecff; border-radius: 6px; padding: 2px 8px;")
        self.category_label.setFixedHeight(24)
        header_layout.addWidget(self.category_label)
        header_layout.addStretch()

        self.del_btn = QPushButton("删除")
        self.del_btn.setStyleSheet("color: #ff8080; font-weight: bold;")
        self.del_btn.setFixedWidth(55)
        self.del_btn.clicked.connect(lambda: delete_callback(self))
        header_layout.addWidget(self.del_btn)
        self.layout_.addLayout(header_layout)

        action_layout = QHBoxLayout()
        self.type_combo = QComboBox()
        self.type_combo.addItems([cmd.label for cmd in CommandType.ordered()])
        self.type_combo.currentTextChanged.connect(self.on_type_changed)
        action_layout.addWidget(self.type_combo)

        self.value_input = QLineEdit()
        self.value_input.setPlaceholderText("参数值 (如图片路径、文本、时间)")
        action_layout.addWidget(self.value_input)

        self.file_btn = QPushButton("选择图片")
        self.file_btn.clicked.connect(self.select_file)
        self.file_btn.setVisible(True)
        action_layout.addWidget(self.file_btn)

        self.route_edit_btn = QPushButton("编辑路由")
        self.route_edit_btn.clicked.connect(self.edit_route_value)
        self.route_edit_btn.setVisible(True)
        action_layout.addWidget(self.route_edit_btn)

        self.route_value_input = QLineEdit()
        self.route_value_input.setReadOnly(True)
        self.route_value_input.setPlaceholderText("路由JSON(点击“编辑路由”) ")
        action_layout.addWidget(self.route_value_input)
        self.layout_.addLayout(action_layout)

        route_layout = QHBoxLayout()
        self.timeout_input = QLineEdit()
        self.timeout_input.setPlaceholderText("超时(秒,0禁用)")
        self.timeout_input.setText("0")
        self.timeout_input.setFixedWidth(100)
        self.timeout_input.setVisible(True)
        route_layout.addWidget(self.timeout_input)

        self.default_input = QLineEdit()
        self.default_input.setPlaceholderText("默认 -> 节点(未命中pattern时)")
        self.default_input.setFixedWidth(180)
        route_layout.addWidget(self.default_input)
        route_layout.addStretch()
        self.layout_.addLayout(route_layout)

        self.node_id_input.textChanged.connect(lambda _: self.changed.emit())
        self.value_input.textChanged.connect(lambda _: self.changed.emit())
        self.timeout_input.textChanged.connect(lambda _: self.changed.emit())
        self.default_input.textChanged.connect(lambda _: self.changed.emit())
        self.type_combo.currentTextChanged.connect(lambda _: self.changed.emit())
        self.route_value_input.textChanged.connect(lambda _: self.changed.emit())
        
        self.show()

    def _apply_frame_style(self, bg_color: str):
        self.setStyleSheet(f"QFrame {{ border: 1px solid #8f9faf; border-radius: 8px; background: {bg_color}; }}")

    def set_visited(self, visited: bool):
        self._is_visited = bool(visited)
        self._apply_frame_style(self._visited_bg_color if self._is_visited else self._default_bg_color)

    def on_type_changed(self, text):
        cmd_type = CommandType.from_label(text)
        
        # 图片相关操作 (1, 2, 3, 8)
        if cmd_type in [CommandType.LEFT_CLICK, CommandType.LEFT_DOUBLE_CLICK, CommandType.RIGHT_CLICK, CommandType.HOVER]:
            self.file_btn.setVisible(True)
            self.file_btn.setText("选择图片")
            self.timeout_input.setVisible(True)
            self.value_input.setPlaceholderText("图片路径")
        # 输入 (4)
        elif cmd_type == CommandType.INPUT_TEXT:
            self.file_btn.setVisible(False)
            self.timeout_input.setVisible(False)
            self.value_input.setPlaceholderText("请输入要发送的文本")
        # 等待 (5)
        elif cmd_type == CommandType.WAIT:
            self.file_btn.setVisible(False)
            self.timeout_input.setVisible(False)
            self.value_input.setPlaceholderText("等待秒数 (如 1.5)")
        # 滚轮 (6)
        elif cmd_type == CommandType.SCROLL:
            self.file_btn.setVisible(False)
            self.timeout_input.setVisible(False)
            self.value_input.setPlaceholderText("滚动距离 (正数向上，负数向下)")
        # 系统按键 (7)
        elif cmd_type == CommandType.HOTKEY:
            self.file_btn.setVisible(False)
            self.timeout_input.setVisible(False)
            self.value_input.setPlaceholderText("组合键 (如 ctrl+s, alt+tab)")
        # 截图保存 (9)
        elif cmd_type == CommandType.SCREENSHOT:
            self.file_btn.setVisible(True)
            self.file_btn.setText("选择保存文件夹")
            self.timeout_input.setVisible(False)
            self.value_input.setPlaceholderText("保存目录 (如 D:\\Screenshots)")
        # 路由判断 (10)
        elif cmd_type == CommandType.ROUTE:
            self.file_btn.setVisible(False)
            self.timeout_input.setVisible(True)
            self.value_input.setPlaceholderText('路由JSON: {"routes":[{"pattern":"a.png","node":"2"}],"default":"3","confidence":0.8}')

        self.changed.emit()

    def edit_route_value(self):
        initial_text = self.route_value_input.text().strip()
        if not initial_text and CommandType.from_label(self.type_combo.currentText()) == CommandType.ROUTE:
            initial_text = self.value_input.text().strip()
        node_ids = self.get_node_ids_callback()
        dialog = RouteEditorDialog(node_ids=node_ids, raw_value=initial_text, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            payload = dialog.get_route_payload()
        except Exception as e:
            QMessageBox.warning(self, "警告", f"路由配置格式错误: {e}")
            return

        self.route_value_input.setText(json.dumps(payload, ensure_ascii=False))
        if CommandType.from_label(self.type_combo.currentText()) == CommandType.ROUTE:
            self.value_input.setText(json.dumps(payload, ensure_ascii=False))
        self.changed.emit()

    def set_data(self, data):
        """用于回填数据"""
        if isinstance(data, WorkflowNode):
            node_data = data
        elif isinstance(data, dict):
            node_data = WorkflowNode.from_dict(data)
        else:
            return

        cmd_type = CommandType.from_raw(node_data.type)
        value = node_data.value
        timeout_second = node_data.timeout_second
        node_id = str(node_data.node_id).strip()
        route_config = RouteConfig.from_raw(getattr(node_data, "routes", None))

        routes = ""
        if route_config.routes:
            routes = json.dumps(route_config.to_dict(), ensure_ascii=False)
        elif cmd_type == CommandType.ROUTE and str(value).strip():
            routes = str(value).strip()

        self.type_combo.setCurrentText(cmd_type.label)

        if cmd_type == CommandType.ROUTE and routes:
            self.value_input.setText(routes)
        else:
            self.value_input.setText(str(value))

        self.node_id_input.setText(node_id)
        self.default_input.setText(str(route_config.default).strip())
        self.route_value_input.setText(routes)
        self.timeout_input.setText(str(timeout_second))
        self.set_category(NodeCategory.from_raw(getattr(node_data, "category", NodeCategory.MIDDLE)))

    def set_category(self, category: NodeCategory):
        tag = category.value
        self.category_label.setText(tag)

        if category == NodeCategory.START:
            self.category_label.setStyleSheet("color: "+"#1e5d2f"+"; background: "+"#d7f5df"+"; border-radius: 6px; padding: 2px 8px;")
        elif category == NodeCategory.END:
            self.category_label.setStyleSheet("color: "+"#7a3b1f"+"; background: "+"#ffe8d8"+"; border-radius: 6px; padding: 2px 8px;")
        else:
            self.category_label.setStyleSheet("color: "+"#2d4f7c"+"; background: "+"#d9ecff"+"; border-radius: 6px; padding: 2px 8px;")

    def select_file(self):
        cmd_type = CommandType.from_label(self.type_combo.currentText())
        capture_dir = self.get_capture_dir_callback() if callable(self.get_capture_dir_callback) else ""
        init_dir = capture_dir if capture_dir and os.path.isdir(capture_dir) else os.getcwd()
        
        # 截图保存 (9.0) -> 选择文件夹
        if cmd_type == CommandType.SCREENSHOT:
            folder = QFileDialog.getExistingDirectory(self, "选择保存文件夹", init_dir)
            if folder:
                self.value_input.setText(folder)
        
        # 其他图片操作 (1, 2, 3, 8) -> 打开文件对话框
        else:
            capture_files = collect_capture_images(capture_dir)
            if capture_files:
                dialog = CaptureGalleryDialog(capture_files, parent=self)
                if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_file:
                    self.value_input.setText(dialog.selected_file)
                    return

            filename, _ = QFileDialog.getOpenFileName(self, "选择图片", init_dir, "Image Files (*.png *.jpg *.bmp)")
            if filename:
                self.value_input.setText(filename)

    def get_data(self):
        cmd_type = CommandType.from_label(self.type_combo.currentText())
        value = self.value_input.text()
        
        # 数据校验与转换
        try:
            if cmd_type in [CommandType.WAIT, CommandType.SCROLL]:
                # 尝试转换为数字，如果失败可能会在运行时报错，这里简单处理
                if not value: value = "0"
            
            timeout_second = 0.0
            if self.timeout_input.isVisible():
                timeout_text = self.timeout_input.text().strip()
                if timeout_text:
                    timeout_second = float(timeout_text)
                if timeout_second < 0:
                    timeout_second = 0.0
        except ValueError:
            timeout_second = 0.0

        default_node = self.default_input.text().strip()
        route_mapping: dict[str, str] = {}
        route_confidence = 0.8

        route_text = self.route_value_input.text().strip()
        if route_text:
            try:
                parsed_routes, parsed_default, parsed_confidence = parse_route_value(route_text)
                route_mapping = dict(parsed_routes)
                if parsed_default:
                    default_node = parsed_default
                route_confidence = parsed_confidence
            except Exception:
                pass

        if cmd_type == CommandType.ROUTE and not route_mapping and not default_node:
            try:
                parsed_routes, parsed_default, parsed_confidence = parse_route_value(value)
                route_mapping = dict(parsed_routes)
                default_node = parsed_default
                route_confidence = parsed_confidence
            except Exception:
                pass

        route_config = RouteConfig(
            routes=route_mapping,
            default=default_node,
            confidence=route_confidence,
        )

        normalized_value = value
        if cmd_type == CommandType.ROUTE and (route_mapping or default_node):
            normalized_value = json.dumps(route_config.to_dict(), ensure_ascii=False)

        return WorkflowNode(
            node_id=self.node_id_input.text().strip(),
            type=cmd_type,
            value=normalized_value,
            timeout_second=timeout_second,
            routes=route_config,
        )

class FlowArrowOverlay(QWidget):
    def __init__(self, parent, get_rows_callback):
        super().__init__(parent)
        self.get_rows_callback = get_rows_callback
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent;")

    def _draw_arrow(self, painter:QPainter, start, end, color, dashed=False):
        pen = QPen(color, 2)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(start, end)

        dx = end.x() - start.x()
        dy = end.y() - start.y()
        length = (dx ** 2 + dy ** 2) ** 0.5
        if length == 0:
            return

        ux = dx / length
        uy = dy / length
        arrow_size = 8

        tip = QPointF(end.x(), end.y())
        left = QPointF(
            end.x() - ux * arrow_size - uy * (arrow_size * 0.6),
            end.y() - uy * arrow_size + ux * (arrow_size * 0.6)
        )
        right = QPointF(
            end.x() - ux * arrow_size + uy * (arrow_size * 0.6),
            end.y() - uy * arrow_size - ux * (arrow_size * 0.6)
        )

        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([tip, left, right]))

    def paintEvent(self, event):
        super().paintEvent(event)

        rows = self.get_rows_callback()
        if not rows:
            return

        node_pos = {}
        for row in rows:
            node_id = row.node_id_input.text().strip()
            if node_id:
                node_pos[node_id] = row.geometry()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        for row in rows:
            src_id = row.node_id_input.text().strip()
            if not src_id:
                continue

            src_rect = row.geometry()
            try:
                routes, default_node, _confidence = parse_row_routes(row)

                for idx, (_pattern, target_node) in enumerate(routes):
                    if target_node not in node_pos:
                        continue
                    dst_rect = node_pos[target_node]
                    offset = (idx - (len(routes) - 1) / 2) * 20
                    start = QPointF(src_rect.center().x() + offset, src_rect.bottom() - 4)
                    end = QPointF(dst_rect.center().x(), dst_rect.top() + 4)
                    self._draw_arrow(painter, start, end, QColor("#4a78ff"), dashed=False)

                if default_node and default_node in node_pos:
                    dst_rect = node_pos[default_node]
                    start = QPointF(src_rect.center().x(), src_rect.bottom() - 4)
                    end = QPointF(dst_rect.center().x(), dst_rect.top() + 4)
                    self._draw_arrow(painter, start, end, QColor("#a66cff"), dashed=True)
            except Exception:
                pass

class RPAWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("不高兴就喝水 RPA 配置工具")
        self.resize(800, 600)
        
        self.engine = RPAEngine()
        self.worker = None
        self.logs_root = os.path.join(os.getcwd(), "logs")
        self.engine.set_event_log_root(self.logs_root)
        self.input_recorder = InputRecorder(
            log_root=self.logs_root,
            get_session_id=self.engine.get_session_id,
            get_step_seq=self.engine.get_step_seq,
            capture_interval_sec=0.8,
            capture_box_size=180,
        )
        self.rows: List["TaskRow"] = []
        self._refreshing_arrows = False
        self.node_width = 560
        self.node_height = 130
        self.h_gap = 90
        self.v_gap = 40
        self.canvas_margin = 20

        # 主布局
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # 顶部控制栏
        top_bar = QHBoxLayout()
        
        self.add_btn = QPushButton("+ 新增节点")
        self.add_btn.clicked.connect(self.add_row)
        top_bar.addWidget(self.add_btn)

        self.save_btn = QPushButton("保存配置")
        self.save_btn.clicked.connect(self.save_config)
        top_bar.addWidget(self.save_btn)

        self.load_btn = QPushButton("导入配置")
        self.load_btn.clicked.connect(self.load_config)
        top_bar.addWidget(self.load_btn)
        
        top_bar.addStretch()
        
        self.loop_check = QComboBox()
        self.loop_check.addItems(["执行一次", "循环执行"])
        top_bar.addWidget(self.loop_check)
        
        self.start_btn = QPushButton("开始运行")
        self.start_btn.setStyleSheet("background-color: "+"#4CAF50"+"; color: white;")
        self.start_btn.clicked.connect(self.start_task)
        top_bar.addWidget(self.start_btn)

        self.insert_runtime_btn = QPushButton("在线插入")
        self.insert_runtime_btn.clicked.connect(self.insert_runtime_node)
        self.insert_runtime_btn.setEnabled(False)
        top_bar.addWidget(self.insert_runtime_btn)

        self.build_draft_btn = QPushButton("生成草稿")
        self.build_draft_btn.clicked.connect(self.build_draft_from_last_session)
        self.build_draft_btn.setEnabled(True)
        top_bar.addWidget(self.build_draft_btn)
        
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setStyleSheet("background-color: "+"#f44336"+"; color: white;")
        self.stop_btn.clicked.connect(self.stop_task)
        self.stop_btn.setEnabled(False)
        top_bar.addWidget(self.stop_btn)
        
        main_layout.addLayout(top_bar)

        # 节点画布区域 (滚动)
        self.scroll_ = QScrollArea()
        self.scroll_.setWidgetResizable(True)
        self.task_container = QWidget()
        self.task_container.setMinimumSize(900, 500)
        self.scroll_.setWidget(self.task_container)
        main_layout.addWidget(self.scroll_)

        self.flow_hint = QLabel("蓝色实线=pattern路由    紫色虚线=default默认路由")
        main_layout.addWidget(self.flow_hint)

        self.arrow_overlay = FlowArrowOverlay(self.task_container, lambda: self.rows)
        self.arrow_overlay.raise_()
        self.task_container.installEventFilter(self)
        self.scroll_.viewport().installEventFilter(self)

        # 日志区域
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(150)
        main_layout.addWidget(QLabel("运行日志:"))
        main_layout.addWidget(self.log_area)

        # 初始添加一行
        self.add_row()
        self.refresh_arrows()

    def eventFilter(self, obj, event:QEvent):
        if obj in [self.task_container, self.scroll_.viewport()] and event.type() in [QEvent.Type.Resize, QEvent.Type.LayoutRequest]:
            self.refresh_arrows()
        return super().eventFilter(obj, event)

    def auto_layout_nodes(self):
        if not self.rows:
            if self.task_container.minimumSize().width() != 900 or self.task_container.minimumSize().height() != 500:
                self.task_container.setMinimumSize(900, 500)
            return

        row_by_id: Dict[str, TaskRow] = {}
        for row in self.rows:
            node_id = row.node_id_input.text().strip()
            if node_id and node_id not in row_by_id:
                row_by_id[node_id] = row

        occupied = set()
        placed = {}

        def alloc_down(x, y):
            ny = y
            while (x, ny) in occupied:
                ny += 1
            return x, ny

        def alloc_right(x, y):
            nx = x
            while (nx, y) in occupied:
                nx += 1
            return nx, y

        root_row = self.rows[0]
        root_id = root_row.node_id_input.text().strip() or "1"
        placed[root_id] = (0, 0)
        occupied.add((0, 0))

        queue = [root_id]
        visited = set()

        while queue:
            node_id = queue.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)

            src_row = row_by_id.get(node_id)
            if not src_row:
                continue

            sx, sy = placed[node_id]
            default_target = ""
            route_targets: list[str] = []
            try:
                routes, default_node, _confidence = parse_row_routes(src_row)
                default_target = default_node if default_node in row_by_id else ""
                for _pattern, target_node in routes:
                    if target_node in row_by_id and target_node not in route_targets:
                        route_targets.append(target_node)
            except Exception:
                pass

            if route_targets:
                base_y = sy + 1
                total = len(route_targets)
                start_x = sx - (total - 1) // 2

                for i, target_node in enumerate(route_targets):
                    if target_node in placed:
                        queue.append(target_node)
                        continue

                    candidate_x = start_x + i
                    px, py = alloc_right(candidate_x, base_y)
                    placed[target_node] = (px, py)
                    occupied.add((px, py))
                    queue.append(target_node)

            if default_target and default_target not in placed:
                px, py = alloc_down(sx, sy + 1)
                placed[default_target] = (px, py)
                occupied.add((px, py))
                queue.append(default_target)
            elif default_target:
                queue.append(default_target)

        max_y = max(y for _, y in occupied) if occupied else 0
        for row in self.rows:
            node_id = row.node_id_input.text().strip()
            if not node_id:
                continue
            if node_id not in placed:
                px, py = alloc_down(0, max_y + 1)
                placed[node_id] = (px, py)
                occupied.add((px, py))
                max_y = max(max_y, py)

        max_x = 0
        max_y = 0
        fallback_index = 0

        used_grid_points = []
        for row in self.rows:
            node_id = row.node_id_input.text().strip()
            if node_id:
                used_grid_points.append(placed.get(node_id, (0, fallback_index)))
            else:
                used_grid_points.append((0, fallback_index))
            fallback_index += 1

        if used_grid_points:
            min_gx = min(point[0] for point in used_grid_points)
            min_gy = min(point[1] for point in used_grid_points)
        else:
            min_gx = 0
            min_gy = 0

        shift_x = -min_gx if min_gx < 0 else 0
        shift_y = -min_gy if min_gy < 0 else 0

        fallback_index = 0
        for row in self.rows:
            node_id = row.node_id_input.text().strip()
            if node_id:
                gx, gy = placed.get(node_id, (0, fallback_index))
            else:
                gx, gy = (0, fallback_index)

            gx += shift_x
            gy += shift_y

            fallback_index += 1
            px = self.canvas_margin + gx * (self.node_width + self.h_gap)
            py = self.canvas_margin + gy * (self.node_height + self.v_gap)
            row.setFixedWidth(self.node_width)
            row.setFixedHeight(self.node_height)
            row.move(px, py)
            max_x = max(max_x, px + self.node_width)
            max_y = max(max_y, py + self.node_height)

        view_w = self.scroll_.viewport().width()
        min_w = max(view_w - 4, max_x + self.canvas_margin)
        min_h = max(500, max_y + self.canvas_margin)
        if self.task_container.width() != min_w or self.task_container.height() != min_h:
            self.task_container.resize(min_w, min_h)
        if self.task_container.minimumSize().width() != min_w or self.task_container.minimumSize().height() != min_h:
            self.task_container.setMinimumSize(min_w, min_h)

    def refresh_arrows(self):
        if self._refreshing_arrows:
            return

        self._refreshing_arrows = True
        try:
            self.update_node_categories()
            self.auto_layout_nodes()
            self.arrow_overlay.setGeometry(self.task_container.rect())
            self.arrow_overlay.raise_()
            self.arrow_overlay.update()
        finally:
            self._refreshing_arrows = False

    def update_node_categories(self):
        if not self.rows:
            return

        node_ids = {
            row.node_id_input.text().strip()
            for row in self.rows
            if row.node_id_input.text().strip()
        }

        in_degree: Dict[str, int] = {node_id: 0 for node_id in node_ids}
        out_degree: Dict[str, int] = {node_id: 0 for node_id in node_ids}

        for row in self.rows:
            src_id = row.node_id_input.text().strip()
            if not src_id or src_id not in node_ids:
                continue

            targets = set()

            try:
                routes, default_node, _confidence = parse_row_routes(row)
                for _pattern, target_node in routes:
                    if target_node in node_ids:
                        targets.add(target_node)
                if default_node and default_node in node_ids:
                    targets.add(default_node)
            except Exception:
                pass

            out_degree[src_id] += len(targets)
            for target in targets:
                in_degree[target] += 1

        for row in self.rows:
            node_id = row.node_id_input.text().strip()
            if not node_id or node_id not in node_ids:
                row.set_category(NodeCategory.MIDDLE)
                continue

            indeg = in_degree.get(node_id, 0)
            outdeg = out_degree.get(node_id, 0)

            if indeg == 0:
                row.set_category(NodeCategory.START)
            elif outdeg == 0:
                row.set_category(NodeCategory.END)
            else:
                row.set_category(NodeCategory.MIDDLE)

    def add_row(self, data=None):
        row = TaskRow(self.task_container, self.delete_row, self.get_available_node_ids, self.get_latest_capture_dir)
        if data:
            row.set_data(data)
        else:
            row.node_id_input.setText(str(len(self.rows) + 1))

        row.changed.connect(self.refresh_arrows)
        self.rows.append(row)
        
        self.refresh_arrows()

    def get_available_node_ids(self):
        return [
            row.node_id_input.text().strip()
            for row in self.rows
            if row.node_id_input.text().strip()
        ]

    def _clear_node_visit_highlight(self):
        for row in self.rows:
            row.set_visited(False)

    def _mark_node_visited(self, node_id: str):
        target_id = str(node_id).strip()
        if not target_id:
            return

        for row in self.rows:
            if row.node_id_input.text().strip() == target_id:
                row.set_visited(True)
                break

    def get_latest_capture_dir(self):
        session_id = self.engine.get_session_id() or self.engine.get_last_session_id()
        if not session_id:
            return ""
        capture_dir = os.path.join(self.logs_root, session_id, "cursor_crops")
        return capture_dir if os.path.isdir(capture_dir) else ""

    def delete_row(self, row_widget):
        if row_widget in self.rows:
            self.rows.remove(row_widget)
            row_widget.deleteLater()
            self.refresh_arrows()
            
    def save_config(self):
        nodes: list[WorkflowNode] = []
        for row in self.rows:
            data = row.get_data()
            nodes.append(data)
            
        if not nodes:
            QMessageBox.warning(self, "警告", "没有可保存的配置")
            return

        start_node = str(nodes[0].node_id).strip() or "1"
        payload = WorkflowConfig(start_node=start_node, nodes=nodes).to_dict()

        filename, _ = QFileDialog.getSaveFileName(self, "保存配置", os.getcwd(), "JSON Files (*.json);;YAML Files (*.yaml *.yml);;Text Files (*.txt)")
        if filename:
            try:
                ext = os.path.splitext(filename)[1].lower()
                if ext in [".yaml", ".yml"]:
                    workflow = WorkflowConfig.from_raw(payload)
                    dump_workflow_to_yaml_file(workflow, filename)
                else:
                    with open(filename, 'w', encoding='utf-8') as f:
                        json.dump(payload, f, indent=4, ensure_ascii=False)
                QMessageBox.information(self, "成功", "配置已保存！")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"保存失败: {e}")

    def _collect_current_nodes(self):
        nodes: list[WorkflowNode] = []
        for row in self.rows:
            nodes.append(row.get_data())
        return nodes

    def _load_workflow_rows(self, workflow: WorkflowConfig):
        for row in self.rows:
            row.deleteLater()
        self.rows.clear()

        for node in workflow.nodes:
            self.add_row(node)

        self.refresh_arrows()

    def _append_draft_workflow(self, draft_workflow: WorkflowConfig):
        current_nodes = self._collect_current_nodes()

        if not current_nodes:
            return WorkflowConfig.from_raw({"start_node": draft_workflow.start_node, "nodes": draft_workflow.nodes})

        merged_nodes = [
            WorkflowNode(
                node_id=str(node.node_id),
                category=NodeCategory.from_raw(getattr(node, "category", NodeCategory.MIDDLE)),
                type=CommandType.from_raw(node.type),
                value=str(node.value),
                timeout_second=float(node.timeout_second),
                routes=RouteConfig.from_raw(getattr(node, "routes", None)),
            )
            for node in current_nodes
        ]

        draft_nodes = [
            WorkflowNode(
                node_id=str(node.node_id),
                category=NodeCategory.from_raw(getattr(node, "category", NodeCategory.MIDDLE)),
                type=CommandType.from_raw(node.type),
                value=str(node.value),
                timeout_second=float(node.timeout_second),
                routes=RouteConfig.from_raw(getattr(node, "routes", None)),
            )
            for node in draft_workflow.nodes
        ]

        last_node = merged_nodes[-1]
        if not str(last_node.routes.default).strip() and draft_nodes:
            last_node.routes.default = str(draft_nodes[0].node_id)

        merged_nodes.extend(draft_nodes)

        start_node = str(merged_nodes[0].node_id).strip() if merged_nodes else ""
        return WorkflowConfig.from_raw({"start_node": start_node, "nodes": merged_nodes})

    def load_config(self):
        filename, _ = QFileDialog.getOpenFileName(self, "导入配置", os.getcwd(), "JSON Files (*.json);;YAML Files (*.yaml *.yml);;Text Files (*.txt)")
        if not filename:
            return
            
        try:
            ext = os.path.splitext(filename)[1].lower()
            if ext in [".yaml", ".yml"]:
                workflow = load_workflow_from_yaml_file(filename)
            else:
                with open(filename, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                workflow = WorkflowConfig.from_raw(config)

            # 清空现有行
            for row in self.rows:
                row.deleteLater()
            self.rows.clear()
            
            # 重新添加行
            for task in workflow.nodes:
                self.add_row(task)

            self.refresh_arrows()
                
            QMessageBox.information(self, "成功", f"成功导入 {len(workflow.nodes)} 条指令！")
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"导入失败: {e}")

    def validate_flow(self, nodes):
        start_node = str(nodes[0].node_id).strip() if nodes else ""
        workflow = WorkflowConfig.from_raw({"start_node": start_node, "nodes": nodes})
        return validate_workflow_config(workflow)

    def start_task(self):
        nodes: list[WorkflowNode] = []
        for row in self.rows:
            data = row.get_data()
            nodes.append(data)
            
        if not nodes:
            QMessageBox.warning(self, "警告", "请至少添加一条指令！")
            return

        error_msg = self.validate_flow(nodes)
        if error_msg:
            QMessageBox.warning(self, "警告", error_msg)
            return

        start_node = str(nodes[0].node_id).strip()
        workflow = WorkflowConfig(start_node=start_node, nodes=nodes)

        self.log_area.clear()
        self.log("任务开始...")
        self._clear_node_visit_highlight()
        
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.add_btn.setEnabled(False)
        self.insert_runtime_btn.setEnabled(True)
        self.build_draft_btn.setEnabled(True)
        
        loop = (self.loop_check.currentText() == "循环执行")
        
        self.worker = WorkerThread(self.engine, workflow, loop)
        self.worker.log_signal.connect(self.log)
        self.worker.node_visited_signal.connect(self._mark_node_visited)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

        if self.input_recorder.start():
            self.log("输入录制已开启")
        else:
            reason = self.input_recorder.unavailable_reason()
            self.log(f"输入录制不可用: {reason or '缺少 pynput 依赖'}")

    def stop_task(self):
        self.engine.stop()
        self.input_recorder.stop()
        self.log("正在停止...")

    def on_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.add_btn.setEnabled(True)
        self.insert_runtime_btn.setEnabled(False)
        self.build_draft_btn.setEnabled(True)
        self.input_recorder.stop()
        self.log("任务已结束")
        
        # 恢复窗口并置顶
        self.activateWindow()

    def log(self, msg):
        self.log_area.append(msg)

    def insert_runtime_node(self):
        if not self.engine.is_running:
            QMessageBox.warning(self, "提示", "当前未运行，无法在线插入")
            return

        node_ids = self.engine.get_runtime_node_ids() or self.get_available_node_ids()
        current_node_id = self.engine.get_current_node_id()
        dialog = RuntimeInsertDialog(
            node_ids=node_ids,
            current_node_id=current_node_id,
            capture_dir=self.get_latest_capture_dir(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            patch = dialog.get_patch()
            self.engine.request_insert_after(
                anchor_node_id=patch["anchor_node_id"],
                nodes=patch["nodes"],
                effective_after=patch["effective_after"],
            )
            self.log(f"已提交在线插入草稿: after {patch['anchor_node_id']}")
        except Exception as e:
            QMessageBox.warning(self, "插入失败", str(e))

    def build_draft_from_last_session(self):
        session_id = self.engine.get_last_session_id() or self.engine.get_session_id()
        if not session_id:
            QMessageBox.warning(self, "提示", "没有可用的最近会话")
            return

        try:
            workflow = build_draft_workflow_from_session(self.logs_root, session_id)
        except Exception as e:
            QMessageBox.warning(self, "草稿生成失败", str(e))
            return

        dialog = DraftPreviewDialog(workflow, session_id=session_id, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.log("已取消草稿应用")
            return

        mode = dialog.apply_mode()
        try:
            if mode == "replace":
                target = WorkflowConfig.from_raw({"start_node": workflow.start_node, "nodes": workflow.nodes})
            elif mode == "append":
                target = self._append_draft_workflow(workflow)
            else:
                self.log("草稿未应用")
                return
        except Exception as e:
            QMessageBox.warning(self, "草稿应用失败", str(e))
            return

        self._load_workflow_rows(target)
        action_text = "替换" if mode == "replace" else "追加"
        self.log(f"已从会话生成草稿并{action_text}应用: {session_id} ({len(workflow.nodes)} 节点)")

    def closeEvent(self, event):
        """窗口关闭事件：确保线程停止，防止残留"""
        self.input_recorder.stop()
        if self.worker and self.worker.isRunning():
            self.engine.stop()
            self.worker.quit()
            self.worker.wait()
        event.accept()

def main():
    app = QApplication(sys.argv)
    window = RPAWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
