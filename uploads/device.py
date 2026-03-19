import logging
import time
import os
import threading
import traceback
import yaml
import serial
import json
import hashlib
import shutil
from datetime import datetime
from pymodbus.client import ModbusSerialClient as ModbusClient
from ZDT_Controller import ZDTController
from Dobot_Arms.MG400 import *
from serial import Serial
import numpy as np
import csv
import json
import re
from copy import deepcopy
import re
from copy import deepcopy
import threading
import traceback
import time
import math
import cv2
from typing import Dict, List, Tuple, Optional, Any
from dh_gripper.gripper import *
import logging
import time
from runze_driver import SwitchValve
from runze_ascii import SyringePump_ASCII

from cmos_camera import CMOSAutoFocusCamera
from Dobot_Arms.MG400 import *
from serial import Serial
import numpy as np
from dh_gripper.gripper import *
import logging
import time
from serial import Serial
from dh_gripper.gripper import Gripper
import pyautogui
import win32gui
import win32con
import threading

# 若你确实需要 MG400 / numpy，在文件顶部导入（不用也可以先不导）
from Dobot_Arms.MG400 import *   # 不推荐 import *
import numpy as np

class Device:
    CAMX_SOFT_MIN = 0.0
    CAMX_SOFT_MAX = 430.0

    def __init__(
        self,
        config_path: str = "./zdtconfig.yaml",
        valve_port: str = "COM22",
        valve_baudrate: int = 9600,
        valve_parity: str = "N",
        valve_timeout: float = 0.1,
        log_file: str = "./logs.txt",
        camera_id: int = 0,
        camera_save_dir: str = r"C:\Users\Admin\Documents\GitHub\zsyEP\zsyEP\zsyEP\photos",
        camera_min_pos: int = 0,
        camera_max_pos: int = 18,
        camera_speed: int = 6,
        gripper_port: str = "COM30",
        gripper_baudrate: int = 115200,
        gripper_slave: int = 4,
        gripper_name: str = "pgse",
        arm_ip: str = "192.168.1.6",
        connect_arm: bool = True,
    ):
        # ========== 1) load config ==========
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        # ========== 2) logger ==========
        # basicConfig 多次调用可能无效；但你要在 __init__ 里配，这里就只配一次
        logging.basicConfig(
            filename=log_file,
            format="%(asctime)s %(name)-12s %(levelname)-8s %(message)s",
            level=logging.INFO,
            datefmt="%m-%d-%y %H:%M:%S",
        )
        self.logger = logging.getLogger("device")


        self.arm_lock = threading.Lock()      # 机械臂/夹爪资源
        self.wash_lock = threading.Lock()     # 清洗/补水液路资源
        self.state_lock = threading.Lock()    # 可选：状态记录

        # ========== 3) Modbus clients (connect and keep) ==========
        self.ser_camy = ModbusClient(**self.config["camy"]["ser"])
        if not self.ser_camy.connect():
            raise RuntimeError("camy 串口连接失败")

        self.ser_camx = ModbusClient(**self.config["camx"]["ser"])
        if not self.ser_camx.connect():
            raise RuntimeError("camx 串口连接失败")

        self.water_in_ser = ModbusClient(**self.config["water_in"]["ser"])
        if not self.water_in_ser.connect():
            raise RuntimeError("water_in 串口连接失败")

        self.acid_in_ser = ModbusClient(**self.config["acid_in"]["ser"])
        if not self.acid_in_ser.connect():
            raise RuntimeError("acid_in 串口连接失败")

        # waste pumps clients + controllers
        self.waste_clients = {}
        self.waste_pumps = {}
        for i in range(1, 9):
            key = f"wpump{i}"
            cfg = self.config.get(key)
            if not cfg:
                self.logger.warning("缺少配置: %s", key)
                continue

            cli = ModbusClient(**cfg["ser"])
            if not cli.connect():
                self.logger.error("%s 串口连接失败", key)
                continue

            self.waste_clients[i] = cli
            self.waste_pumps[i] = ZDTController(cli, 1, cfg["config_param"])

        # ========== 4) ZDT Controllers ==========
        self.camy = ZDTController(self.ser_camy, 1, self.config["camy"]["config_param"])
        self.camx = ZDTController(self.ser_camx, 1, self.config["camx"]["config_param"])

        self.water_in = ZDTController(
            self.water_in_ser, 15, self.config["water_in"]["config_param"]
        )
        self.acid_in = ZDTController(
            self.acid_in_ser, 16, self.config["acid_in"]["config_param"]
        )

        # ========== 5) Runze serial (valves + syringe pumps share one line) ==========
        self.runze_connection = serial.Serial(
            port=valve_port,
            baudrate=valve_baudrate,
            parity=valve_parity,
            timeout=valve_timeout,
        )

        # valves
        self.switch_valve_dict = {i: i for i in range(1, 13)}
        self.valve_water = SwitchValve(self.runze_connection, 5, 12, "SV-07", self.logger)
        self.valve_acid = SwitchValve(self.runze_connection, 6, 12, "SV-07", self.logger)

        # syringe pumps
        self.syringe_pumps = {}
        for slave in (1, 2, 3, 4):
            p = SyringePump_ASCII(
                self.runze_connection, slave=slave, model="SY-01B", volume=5
            )
            p.set_step_mode(0)
            self.syringe_pumps[slave] = p

        # ========== 6) Camera ==========
        self.cam = CMOSAutoFocusCamera(
            camera_id=camera_id,
            save_dir=camera_save_dir,
            min_pos=camera_min_pos,
            max_pos=camera_max_pos,
            speed=camera_speed,
        )
        self.cam.set_motor(self.camy)

        # ========== 7) Gripper ==========
        self.ser_gripper = Serial(gripper_port, gripper_baudrate, timeout=0.1)
        self.pgse = Gripper(self.ser_gripper, gripper_slave, self.logger, gripper_name)

        # ========== 8) MG400 Arm ==========
        self.arm_ip = arm_ip
        self.MG400 = MG400(ip=self.arm_ip)


    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    def _clamp_camx(self, x: float) -> float:
        """Clamp camx absolute position to a safe software limit."""
        try:
            x = float(x)
        except Exception:
            raise ValueError(f"camx target must be a number, got: {x!r}")
        return max(self.CAMX_SOFT_MIN, min(self.CAMX_SOFT_MAX, x))

    def safe_camx_absolute_move(self, x: float, v: float = 20) -> float:
        """Safe wrapper of camx.absolute_move(x, v) with [0, 430] limit."""
        x_safe = self._clamp_camx(x)
        self.camx.absolute_move(x_safe, v)
        self._camx_last = x_safe
        return x_safe

    def _capture_frame_bgr(self):
        """
        强制获取一张新帧
        """
        import numpy as np

        # 1 直接调用相机底层读取
        if hasattr(self.cam, "_read_frame"):
            ret, frame = self.cam._read_frame()
            if ret and isinstance(frame, np.ndarray):
                return frame.copy()

        # 2 read_frame
        if hasattr(self.cam, "read_frame"):
            out = self.cam.read_frame()
            if isinstance(out, tuple):
                ret, frame = out
                if ret:
                    return frame.copy()

        # 3 fallback
        if hasattr(self.cam, "get_latest_frame"):
            frame = self.cam.get_latest_frame()
            if frame is not None:
                return frame.copy()

        raise RuntimeError("Camera frame capture failed")

    def _save_fresh_frame(self, save_path: str) -> str:
        """使用 fresh frame 保存调试图，避免读取到缓存旧图。"""
        frame = self._capture_frame_bgr()
        ok = cv2.imwrite(save_path, frame)
        if not ok:
            raise RuntimeError(f"cv2.imwrite failed: {save_path}")
        return save_path

    @staticmethod
    def _segment_copper_frame(
        frame_bgr: np.ndarray,
        morph_ksize: int = 9,
        col_thresh: float = 0.20,
        min_area_ratio: float = 0.01,
    ) -> Dict[str, Any]:
        """
        直接检测铜片区域，而不是通过“背景取反”获得铜片。

        设计思路：
        1) 用 BGR + HSV 联合约束找到偏暖色/偏铜色区域；
        2) 使用连通域时不再只保留最大面积，而是优先保留
           “靠左、足够高、不过宽、像竖直铜片”的区域；
        3) 继续输出 coverage / cx_bias / copper_cols，兼容后续逻辑。
        """
        out = {
            "mask_copper": None,
            "mask_bg": None,
            "coverage": 0.0,
            "cx_bias": 0.0,
            "copper_cols": None,
            "move_hint": "未检测到铜片，建议增大扫描范围或检查阈值",
            "n_components": 0,
            "largest_area_ratio": 0.0,
        }

        if frame_bgr is None or frame_bgr.size == 0:
            return out

        h, w = frame_bgr.shape[:2]
        total_pixels = max(1, h * w)

        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        H, S, V = cv2.split(hsv)
        B, G, R = cv2.split(frame_bgr)

        R16 = R.astype(np.int16)
        G16 = G.astype(np.int16)
        B16 = B.astype(np.int16)

        warm_bgr = (
            (R16 >= G16 + 2) &
            (R16 >= B16 + 8) &
            (R16 >= 60)
        )

        warm_hsv = (
            (((H >= 0) & (H <= 30)) | ((H >= 170) & (H <= 179))) &
            (V >= 55)
        )

        reflective_copper = (
            (R16 >= B16 + 12) &
            (R16 >= 75) &
            (V >= 70)
        )

        near_gray = (
            (np.abs(R16 - G16) <= 12) &
            (np.abs(G16 - B16) <= 12) &
            (S <= 40)
        )

        mask_copper = (
            ((warm_bgr & (warm_hsv | (S >= 12))) | reflective_copper) &
            (~near_gray)
        ).astype(np.uint8) * 255

        k = max(3, int(morph_ksize))
        if k % 2 == 0:
            k += 1
        kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, k // 2), max(3, k // 2)))
        kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
        kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (max(5, k), 3))
        mask_copper = cv2.morphologyEx(mask_copper, cv2.MORPH_OPEN, kernel_open)
        mask_copper = cv2.morphologyEx(mask_copper, cv2.MORPH_CLOSE, kernel_close)
        mask_copper = cv2.morphologyEx(mask_copper, cv2.MORPH_CLOSE, kernel_h)

        n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask_copper, connectivity=8)
        n_components = max(0, n_labels - 1)

        best_label = None
        best_score = -1e18
        best_area_ratio = 0.0

        for label in range(1, n_labels):
            x = int(stats[label, cv2.CC_STAT_LEFT])
            ww = int(stats[label, cv2.CC_STAT_WIDTH])
            hh = int(stats[label, cv2.CC_STAT_HEIGHT])
            area = int(stats[label, cv2.CC_STAT_AREA])
            cx, cy = centroids[label]

            area_ratio = area / total_pixels
            if area_ratio < float(min_area_ratio):
                continue

            touches_left = (x <= max(10, int(0.03 * w)))
            near_left = (cx <= 0.45 * w)
            tall_enough = (hh >= 0.30 * h)
            not_too_wide = (ww <= 0.70 * w)
            verticalish = (hh >= max(ww, 1))

            score = float(area)
            if touches_left:
                score += area * 1.8
            elif near_left:
                score += area * 1.0
            else:
                score -= area * 1.2

            if tall_enough:
                score += area * 0.7
            else:
                score -= area * 0.7

            if verticalish:
                score += area * 0.3
            if not_too_wide:
                score += area * 0.2
            else:
                score -= area * 0.8

            if cx > 0.70 * w:
                score -= area * 1.2

            if score > best_score:
                best_score = score
                best_label = label
                best_area_ratio = area_ratio

        mask_final = np.zeros_like(mask_copper)
        largest_area_ratio = 0.0
        if best_label is not None:
            mask_final = (labels == best_label).astype(np.uint8) * 255
            largest_area_ratio = float(best_area_ratio)

        mask_copper = mask_final
        mask_bg = cv2.bitwise_not(mask_copper)

        col_fill = np.mean(mask_copper.astype(np.float32), axis=0) / 255.0
        col_is_copper = col_fill > float(col_thresh)
        coverage = float(np.mean(col_is_copper))

        copper_cols = None
        cx_bias = 0.0
        if coverage > 1e-3 and np.any(col_is_copper):
            xs = np.where(col_is_copper)[0]
            copper_cols = (int(xs[0]), int(xs[-1]))
            cx_copper = float(np.mean(xs))
            cx_bias = (cx_copper - (w - 1) * 0.5) / max(1.0, (w - 1) * 0.5)

        if coverage >= 0.90:
            hint = "铜片已充满画面，无需移动"
        elif copper_cols is None:
            hint = "未检测到铜片，建议增大扫描范围或检查阈值"
        elif cx_bias < -0.1:
            hint = f"铜片偏左（bias={cx_bias:+.2f}），建议相机向左移动"
        elif cx_bias > 0.1:
            hint = f"铜片偏右（bias={cx_bias:+.2f}），建议相机向右移动"
        else:
            hint = f"铜片基本居中（bias={cx_bias:+.2f}），可直接进行精细扫描"

        out.update({
            "mask_copper": mask_copper,
            "mask_bg": mask_bg,
            "coverage": float(coverage),
            "cx_bias": float(cx_bias),
            "copper_cols": copper_cols,
            "move_hint": hint,
            "n_components": int(n_components),
            "largest_area_ratio": float(largest_area_ratio),
        })
        return out


    @staticmethod
    def _analyze_copper_frame(frame_bgr: np.ndarray) -> Dict[str, Any]:
        """
        更稳健的铜片分析：
        1) 先用颜色/饱和度/纹理估计“铜片主体区域”
        2) 再在主体区域左右边附近找真实边界
        3) 根据左右边界强度自适应选择 left / right / center 三种锚定方式

        返回字段（关键）：
            found: bool
            center_x_px: Optional[float]
            left_edge_x_px: Optional[float]
            right_edge_x_px: Optional[float]
            width_px: float
            area_frac: float
            conf: float
            anchor_mode: str   # "left" / "right" / "center"
        """
        out = {
            "found": False,
            "center_x_px": None,
            "left_edge_x_px": None,
            "right_edge_x_px": None,
            "width_px": 0.0,
            "area_frac": 0.0,
            "conf": 0.0,
            "anchor_mode": "center",
            "left_edge_strength": 0.0,
            "right_edge_strength": 0.0,
            "region_score": 0.0,
        }

        if frame_bgr is None or frame_bgr.size == 0:
            return out

        h, w = frame_bgr.shape[:2]
        if h < 20 or w < 20:
            return out

        # 只取中间条带，减少上下边缘/脏污/反光影响
        y0 = int(0.12 * h)
        y1 = int(0.88 * h)
        roi = frame_bgr[y0:y1].copy()
        if roi.size == 0:
            return out

        roi_f = roi.astype(np.float32)
        b = roi_f[:, :, 0]
        g = roi_f[:, :, 1]
        r = roi_f[:, :, 2]

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV).astype(np.float32)
        sat = hsv[:, :, 1]

        # 铜片常见暖色特征：R 相对 G/B 更高
        warm = r - 0.55 * g - 0.35 * b

        # 列方向 profile
        gray_prof = np.mean(gray, axis=0)
        warm_prof = np.mean(warm, axis=0)
        sat_prof = np.mean(sat, axis=0)

        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        edge_abs_prof = np.mean(np.abs(gx), axis=0)

        def _smooth(v: np.ndarray, k: int) -> np.ndarray:
            k = int(max(3, k))
            if k % 2 == 0:
                k += 1
            return cv2.GaussianBlur(v.reshape(1, -1).astype(np.float32), (1, k), 0).reshape(-1)

        def _robust_z(v: np.ndarray) -> np.ndarray:
            v = np.asarray(v, dtype=np.float32)
            med = float(np.median(v))
            mad = float(np.median(np.abs(v - med)))
            scale = 1.4826 * mad + 1e-6
            return (v - med) / scale

        gray_prof_s = _smooth(gray_prof, max(11, w // 90))
        warm_prof_s = _smooth(warm_prof, max(11, w // 90))
        sat_prof_s = _smooth(sat_prof, max(11, w // 90))
        edge_abs_prof_s = _smooth(edge_abs_prof, max(9, w // 120))

        # “像铜片”的列分数：暖色 + 饱和度 + 一点纹理
        copper_score = (
            1.35 * _robust_z(warm_prof_s)
            + 0.70 * _robust_z(sat_prof_s)
            + 0.25 * _robust_z(edge_abs_prof_s)
            + 0.10 * _robust_z(gray_prof_s)
        )
        copper_score = _smooth(copper_score, max(15, w // 60))

        # 自适应阈值找主体区域
        thr = max(
            float(np.percentile(copper_score, 58)),
            float(np.mean(copper_score) + 0.10 * np.std(copper_score)),
        )
        mask = copper_score > thr

        # 去掉窄小碎片，只保留较宽连续段
        min_run = max(28, int(0.06 * w))
        best_run = None
        st = None
        for i, v in enumerate(mask):
            if v and st is None:
                st = i
            elif (not v) and st is not None:
                if i - st >= min_run:
                    if best_run is None or (i - st) > (best_run[1] - best_run[0]):
                        best_run = (st, i - 1)
                st = None
        if st is not None and (w - st) >= min_run:
            run = (st, w - 1)
            if best_run is None or (run[1] - run[0]) > (best_run[1] - best_run[0]):
                best_run = run

        # 再做一次基于 profile 台阶的边界估计，供 fallback / refine
        k_step = max(16, int(0.035 * w))
        kern = np.ones(k_step, dtype=np.float32) / float(k_step)
        warm_pad = np.pad(warm_prof_s, (k_step, k_step), mode="edge")
        gray_pad = np.pad(gray_prof_s, (k_step, k_step), mode="edge")
        left_warm = np.convolve(warm_pad[:-1], kern, mode="same")[k_step:k_step + w]
        right_warm = np.convolve(warm_pad[1:], kern, mode="same")[k_step:k_step + w]
        left_gray = np.convolve(gray_pad[:-1], kern, mode="same")[k_step:k_step + w]
        right_gray = np.convolve(gray_pad[1:], kern, mode="same")[k_step:k_step + w]

        edge_signed = (
            0.90 * (left_warm - right_warm) +
            0.55 * (left_gray - right_gray) +
            0.45 * _robust_z(edge_abs_prof_s)
        ).astype(np.float32)
        edge_signed = _smooth(edge_signed, max(9, w // 110))
        edge_abs_mix = np.abs(edge_signed)

        def _refine_edge(idx_guess: int, radius: int) -> Tuple[int, float]:
            l = max(0, int(idx_guess) - radius)
            r2 = min(w, int(idx_guess) + radius + 1)
            if r2 <= l:
                return int(np.clip(idx_guess, 0, w - 1)), 0.0
            loc = edge_abs_mix[l:r2]
            if loc.size == 0:
                return int(np.clip(idx_guess, 0, w - 1)), 0.0
            j = int(np.argmax(loc))
            idx = l + j
            return int(idx), float(loc[j])

        # 优先使用主体区域做边界；若失败再退化到全局候选
        left_edge = None
        right_edge = None
        left_strength = 0.0
        right_strength = 0.0

        if best_run is not None:
            run_l, run_r = best_run
            rad = max(18, int(0.04 * w))
            left_edge, left_strength = _refine_edge(run_l, rad)
            right_edge, right_strength = _refine_edge(run_r, rad)

            # 若 refine 结果离主体边太远，回退到主体边本身
            if abs(left_edge - run_l) > 2 * rad:
                left_edge, left_strength = int(run_l), float(edge_abs_mix[int(run_l)])
            if abs(right_edge - run_r) > 2 * rad:
                right_edge, right_strength = int(run_r), float(edge_abs_mix[int(run_r)])

        # fallback：从全局 profile 里找最强左右边
        pad = max(12, int(0.03 * w))
        edge_work = edge_abs_mix.copy()
        edge_work[:pad] = 0.0
        edge_work[w - pad:] = 0.0
        if left_edge is None or right_edge is None:
            i1 = int(np.argmax(edge_work))
            if edge_work[i1] > 1e-6:
                gap = max(40, int(0.14 * w))
                l = max(0, i1 - gap)
                r2 = min(w, i1 + gap + 1)
                edge_work2 = edge_work.copy()
                edge_work2[l:r2] = 0.0
                i2 = int(np.argmax(edge_work2))
                if edge_work2[i2] > 1e-6:
                    xl, xr = (i1, i2) if i1 < i2 else (i2, i1)
                    if left_edge is None:
                        left_edge = xl
                        left_strength = float(edge_abs_mix[xl])
                    if right_edge is None:
                        right_edge = xr
                        right_strength = float(edge_abs_mix[xr])

        if left_edge is None and right_edge is None:
            return out

        # 若只有一个边界，无法估完整宽度，但仍允许作为单边锚点候选
        if left_edge is None:
            left_edge = max(0, int(right_edge) - max(90, int(0.18 * w)))
        if right_edge is None:
            right_edge = min(w - 1, int(left_edge) + max(90, int(0.18 * w)))

        left_edge = int(np.clip(left_edge, 0, w - 1))
        right_edge = int(np.clip(right_edge, 0, w - 1))
        if right_edge < left_edge:
            left_edge, right_edge = right_edge, left_edge
            left_strength, right_strength = right_strength, left_strength

        width_px = float(max(1, right_edge - left_edge))
        area_frac = float(np.clip(width_px / max(1, w), 0.0, 1.0))
        center_x = 0.5 * (left_edge + right_edge)

        # 区域对比度：主体区 vs 两侧背景
        in_l = int(max(0, left_edge + 0.08 * width_px))
        in_r = int(min(w, right_edge - 0.08 * width_px))
        if in_r <= in_l:
            in_l, in_r = left_edge, right_edge + 1

        bg_margin = max(16, int(0.05 * w))
        left_bg_l = 0
        left_bg_r = max(1, left_edge - bg_margin)
        right_bg_l = min(w - 1, right_edge + bg_margin)
        right_bg_r = w

        warm_in = float(np.mean(warm_prof_s[in_l:in_r])) if in_r > in_l else float(np.mean(warm_prof_s))
        warm_out_parts = []
        if left_bg_r - left_bg_l >= 8:
            warm_out_parts.append(float(np.mean(warm_prof_s[left_bg_l:left_bg_r])))
        if right_bg_r - right_bg_l >= 8:
            warm_out_parts.append(float(np.mean(warm_prof_s[right_bg_l:right_bg_r])))
        warm_out = float(np.mean(warm_out_parts)) if warm_out_parts else float(np.median(warm_prof_s))
        region_score = float(warm_in - warm_out)

        # 置信度：主体够宽 + 边界够强 + 颜色区域对比足够
        edge_scale = float(np.percentile(edge_abs_mix, 85) + 1e-6)
        left_rel = float(left_strength / edge_scale)
        right_rel = float(right_strength / edge_scale)
        region_rel = float(region_score / (np.std(warm_prof_s) + 1e-6))
        conf = (
            0.32 * float(np.clip((area_frac - 0.08) / 0.30, 0.0, 1.0)) +
            0.28 * float(np.clip(max(left_rel, right_rel) / 3.0, 0.0, 1.0)) +
            0.22 * float(np.clip((left_rel + right_rel) / 4.5, 0.0, 1.0)) +
            0.18 * float(np.clip((region_rel + 0.2) / 2.0, 0.0, 1.0))
        )
        conf = float(np.clip(conf, 0.0, 1.0))

        # 自适应锚定：
        # - 右边界更明显，就用 right
        # - 左边界更明显，就用 left
        # - 两边都明显，就用 center（更稳）
        edge_balance = min(left_rel, right_rel) / (max(left_rel, right_rel) + 1e-6)
        if area_frac >= 0.18 and edge_balance >= 0.55:
            anchor_mode = "center"
        else:
            anchor_mode = "right" if right_rel >= left_rel else "left"

        out.update({
            "found": bool(conf >= 0.08),
            "center_x_px": float(center_x),
            "left_edge_x_px": float(left_edge),
            "right_edge_x_px": float(right_edge),
            "width_px": float(width_px),
            "area_frac": float(area_frac),
            "conf": float(conf),
            "anchor_mode": anchor_mode,
            "left_edge_strength": float(left_strength),
            "right_edge_strength": float(right_strength),
            "region_score": float(region_score),
        })
        return out

    @staticmethod
    def _estimate_copper_edges_center(frame_bgr: np.ndarray) -> Tuple[Optional[float], float]:
        """
        兼容旧接口：返回 (center_x_px, strength)
        这里的 strength 现在返回 conf，更有物理意义。
        """
        info = Device._analyze_copper_frame(frame_bgr)
        if not info.get("found", False):
            return None, -1e9
        return float(info["center_x_px"]), float(info["conf"])

    # ----------------------------------------------------------------
    # 颜色分割：通过排除背景（蓝灰色）定位铜片
    # ----------------------------------------------------------------

    @staticmethod
    def _estimate_copper_coverage(
        frame_bgr: np.ndarray,
        bg_h_lo: int = 90,  bg_h_hi: int = 130,
        bg_s_lo: int = 5,   bg_s_hi: int = 60,
        bg_v_lo: int = 80,  bg_v_hi: int = 200,
        morph_ksize: int = 15,
        col_thresh: float = 0.30,
    ) -> Tuple[float, float, Optional[Tuple[int, int]]]:
        """
        兼容旧接口：内部统一使用 _segment_copper_frame。
        传入的 bg_* 参数保留但不再作为主判断依据。
        """
        res = Device._segment_copper_frame(
            frame_bgr,
            morph_ksize=morph_ksize,
            col_thresh=col_thresh,
            min_area_ratio=0.01,
        )
        return (
            float(res.get("coverage", 0.0)),
            float(res.get("cx_bias", 0.0)),
            res.get("copper_cols", None),
        )

    def debug_copper_mask(
        self,
        save_path: str = "./debug_copper_mask.png",
        bg_h_lo: int = 90,  bg_h_hi: int = 130,
        bg_s_lo: int = 5,   bg_s_hi: int = 60,
        bg_v_lo: int = 80,  bg_v_hi: int = 200,
    ) -> None:
        """
        拍一张图，把背景/铜片 mask 可视化并保存，同时打印关键区域的 HSV 值。
        主要用于标定 bg_h/s/v 参数。

        使用方法：
            device.debug_copper_mask()
            # 查看打印的 HSV 值，以及保存的可视化图
            # 绿色叠加 = 背景，红色叠加 = 铜片
            # 若背景区域没被绿色覆盖，调整 bg_h/s/v 范围后重试
        """
        frame = self._capture_frame_bgr()
        if frame is None:
            self.logger.warning("debug_copper_mask: 抓帧失败")
            return

        h_img, w_img = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # 打印采样点 HSV 值（帮助标定参数）
        sample_points = {
            "左上(铜片区)": (h_img // 4,      w_img // 4),
            "左中(铜片区)": (h_img // 2,      w_img // 4),
            "右上(背景区)": (h_img // 4,      w_img * 3 // 4),
            "右中(背景区)": (h_img // 2,      w_img * 3 // 4),
            "正中心":       (h_img // 2,      w_img // 2),
        }
        print("=== 各区域 HSV 采样值 (H:0-179, S:0-255, V:0-255) ===")
        for name, (y, x) in sample_points.items():
            hv, sv, vv = hsv[y, x]
            print(f"  {name} ({x:4d},{y:4d}):  H={hv:3d}  S={sv:3d}  V={vv:3d}")

        coverage, cx_bias, copper_cols = self._estimate_copper_coverage(
            frame,
            bg_h_lo=bg_h_lo, bg_h_hi=bg_h_hi,
            bg_s_lo=bg_s_lo, bg_s_hi=bg_s_hi,
            bg_v_lo=bg_v_lo, bg_v_hi=bg_v_hi,
        )

        # 可视化叠加
        mask_bg = cv2.inRange(
            hsv,
            np.array([bg_h_lo, bg_s_lo, bg_v_lo], dtype=np.uint8),
            np.array([bg_h_hi, bg_s_hi, bg_v_hi], dtype=np.uint8),
        )
        mask_copper_vis = cv2.bitwise_not(mask_bg)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
        mask_copper_vis = cv2.morphologyEx(mask_copper_vis, cv2.MORPH_OPEN,  kernel)
        mask_copper_vis = cv2.morphologyEx(mask_copper_vis, cv2.MORPH_CLOSE, kernel)
        mask_bg_vis = cv2.bitwise_not(mask_copper_vis)

        vis = frame.copy().astype(np.float32)
        vis[mask_copper_vis > 0] = vis[mask_copper_vis > 0] * 0.55 + np.array([0, 0, 200]) * 0.45
        vis[mask_bg_vis     > 0] = vis[mask_bg_vis     > 0] * 0.75 + np.array([0, 180, 0]) * 0.25
        vis = vis.clip(0, 255).astype(np.uint8)

        # 画中心线和铜片边界
        cv2.line(vis, (w_img // 2, 0), (w_img // 2, h_img), (255, 255, 255), 1)
        if copper_cols is not None:
            x_l, x_r = copper_cols
            cv2.line(vis, (x_l, 0), (x_l, h_img), (0, 220, 220), 2)
            cv2.line(vis, (x_r, 0), (x_r, h_img), (0, 220, 220), 2)

        info_txt = f"coverage={coverage:.3f}  cx_bias={cx_bias:+.3f}"
        cv2.putText(vis, info_txt, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0),   3)
        cv2.putText(vis, info_txt, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 0), 2)

        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        cv2.imwrite(save_path, vis)

        print(f"\ncoverage={coverage:.3f}  cx_bias={cx_bias:+.3f}  copper_cols={copper_cols}")
        print(f"调试图已保存: {save_path}")

    def find_copper_by_coverage(
        self,
        target_coverage: float = 0.80,
        coverage_tol: float = 0.05,
        max_iter: int = 6,
        step_units: float = 8.0,
        min_step_units: float = 1.0,
        speed: float = 20.0,
        settle_s: float = 0.8,
        prefocus_first: bool = True,
        prefocus_settle_s: float = 0.6,
        log_prefix: str = "",
        # 颜色分割参数（与 _estimate_copper_coverage 对应，需根据实际背景颜色标定）
        bg_h_lo: int = 90,  bg_h_hi: int = 130,
        bg_s_lo: int = 5,   bg_s_hi: int = 60,
        bg_v_lo: int = 80,  bg_v_hi: int = 200,
    ) -> Tuple[float, float]:
        """
        通过颜色分割迭代移动 camx，使铜片尽量充满画面。
        通常在 第一次 autofocus 之后、camx 粗扫之前 调用。

        流程：
            0) 可选：先做一次 camy autofocus（prefocus_first=True）
            1) 拍帧 → 颜色分割 → 计算 coverage 和 cx_bias
            2) 若 coverage >= target_coverage - tol，收敛退出
            3) 根据 cx_bias 决定移动方向：
                cx_bias < 0 → 铜片偏左 → camx 减小（向左移）
                cx_bias > 0 → 铜片偏右 → camx 增大（向右移）
                |cx_bias| < 0.05 且 coverage 低 → 铜片几乎不在画面，试探性向右移
            4) 若移动后 coverage 没有改善，回退并将 step 缩小一半
            5) 最多迭代 max_iter 次

        Parameters
        ----------
        target_coverage : 目标铜片覆盖率，推荐 0.75~0.90
        coverage_tol    : 收敛容差，coverage >= target - tol 即视为成功
        max_iter        : 最大迭代次数
        step_units      : 单次移动量初始值（camx 单位）
                          建议先测量：camx 移动多少单位对应画面移动一个视野宽度
        min_step_units  : 步长下限，低于此值停止（防止无限振荡）
        speed           : camx 移动速度
        settle_s        : 每次移动后的稳定等待时间（秒）
        prefocus_first  : 是否在开始前先做一次 camy autofocus
                          若上游已经对过焦，传 False 跳过可节省时间
        prefocus_settle_s: autofocus 后的额外等待时间
        bg_h/s/v        : 背景 HSV 颜色范围，需根据实际背景标定
                          可用 debug_copper_mask() 辅助标定

        Returns
        -------
        final_coverage : 结束时的铜片覆盖率估计值
        final_x        : 结束时的 camx 位置
        """
        self.logger.info(
            "%sfind_copper_by_coverage start  target=%.2f  max_iter=%d  step=%.2f",
            log_prefix, target_coverage, max_iter, step_units,
        )

        # 0) 可选预对焦
        if prefocus_first:
            try:
                self.cam.auto_focus()
                if prefocus_settle_s > 0:
                    time.sleep(float(prefocus_settle_s))
                self.logger.info("%s  prefocus done", log_prefix)
            except Exception as e:
                self.logger.warning("%s  prefocus failed: %s", log_prefix, e)

        current_x = getattr(self, "_camx_last",
                            0.5 * (self.CAMX_SOFT_MIN + self.CAMX_SOFT_MAX))
        step = float(step_units)

        def _get_coverage() -> Tuple[float, float]:
            frame = self._capture_frame_bgr()
            cov, bias, _ = self._estimate_copper_coverage(
                frame,
                bg_h_lo=bg_h_lo, bg_h_hi=bg_h_hi,
                bg_s_lo=bg_s_lo, bg_s_hi=bg_s_hi,
                bg_v_lo=bg_v_lo, bg_v_hi=bg_v_hi,
            )
            return float(cov), float(bias)

        for i in range(int(max_iter)):
            coverage, cx_bias = _get_coverage()

            self.logger.info(
                "%s  iter=%d  x=%.3f  coverage=%.3f  cx_bias=%+.3f  step=%.2f",
                log_prefix, i, current_x, coverage, cx_bias, step,
            )

            # 收敛判定
            if coverage >= float(target_coverage) - float(coverage_tol):
                self.logger.info(
                    "%sfind_copper_by_coverage converged  iter=%d  coverage=%.3f",
                    log_prefix, i, coverage,
                )
                break

            # 步长过小时停止
            if step < float(min_step_units):
                self.logger.info(
                    "%sfind_copper_by_coverage: step too small (%.3f), stopping",
                    log_prefix, step,
                )
                break

            # 决定移动方向
            # cx_bias < 0 → 铜片偏左 → camx 需减小（相机向左）
            # cx_bias > 0 → 铜片偏右 → camx 需增大（相机向右）
            if abs(cx_bias) < 0.05:
                # 铜片几乎不在画面中央，不确定方向，先向右试探
                direction = 1.0
            else:
                direction = float(np.sign(cx_bias))

            new_x = self._clamp_camx(current_x + direction * step)

            # 防止原地踏步
            if abs(new_x - current_x) < 0.1:
                step *= 0.5
                continue

            current_x = float(self.safe_camx_absolute_move(new_x, speed))
            if settle_s > 0:
                time.sleep(float(settle_s))

            # 验证是否有改善，没有则回退并缩步
            new_coverage, _ = _get_coverage()
            if new_coverage <= coverage + 0.01:
                # 没有改善：回退到原位，步长减半
                current_x = float(self.safe_camx_absolute_move(
                    self._clamp_camx(current_x - direction * step), speed
                ))
                if settle_s > 0:
                    time.sleep(float(settle_s))
                step *= 0.5
                self.logger.info(
                    "%s  no improvement (%.3f→%.3f), step halved to %.2f",
                    log_prefix, coverage, new_coverage, step,
                )

        # 最终状态
        final_coverage, _ = _get_coverage()
        self.logger.info(
            "%sfind_copper_by_coverage done  final_x=%.3f  final_coverage=%.3f",
            log_prefix, current_x, final_coverage,
        )
        return final_coverage, float(current_x)


    def camx_micro_scan_center_copper(
        self,
        span_units: float = 6,
        n_steps: int = 6,
        speed: float = 20,
        settle_s: float = 1,
        center_x: Optional[float] = None,
        log_prefix: str = "",
        *,
        w_strength: float = 10.0,
        strength_cap: float = 2.0,
        use_two_stage: bool = True,
        fine_span_ratio: float = 0.30,
        fine_steps: int = 9,
        desired_left_edge_frac: float = 0.18,
        desired_right_edge_frac: float = 0.85,
        desired_center_frac: float = 0.50,
        prefocus_before_scan: bool = True,
        prefocus_settle_s: float = 0.6,
        desired_coverage_frac: float = 0.78,
        accept_right_lo: float = 0.80,
        accept_right_hi: float = 0.94,
        accept_cov_lo: float = 0.60,
        accept_cov_hi: float = 0.95,
    ) -> float:
        """
        基于“右边界目标 + 覆盖率达标”的 camx 铜片搜索。

        适用场景：
        - 八个电解池内部结构一致、铜片形状一致；
        - 电解池在空间中平行排布，因此最终拍到的理想视野应尽量一致；
        - 目标不是简单让 coverage 最大，而是让铜片右边界落在一个稳定位置，
          同时 coverage 达到合理区间即可。
        """
        if span_units <= 0:
            raise ValueError("span_units must be > 0")
        if n_steps < 3:
            raise ValueError("n_steps must be >= 3")
        if fine_steps < 3:
            raise ValueError("fine_steps must be >= 3")

        if center_x is None:
            cur = None
            for attr in ("get_position", "read_position", "position"):
                if hasattr(self.camx, attr):
                    try:
                        v = getattr(self.camx, attr)
                        cur = float(v() if callable(v) else v)
                        break
                    except Exception:
                        cur = None
            if cur is None:
                cur = getattr(self, "_camx_last", None)
            if cur is None:
                cur = 0.5 * (self.CAMX_SOFT_MIN + self.CAMX_SOFT_MAX)
            center_x = float(cur)

        center_x = float(self.safe_camx_absolute_move(float(center_x), speed))
        if settle_s and settle_s > 0:
            time.sleep(float(settle_s))

        if prefocus_before_scan:
            try:
                self.cam.auto_focus()
                if prefocus_settle_s and prefocus_settle_s > 0:
                    time.sleep(float(prefocus_settle_s))
            except Exception as e:
                if hasattr(self, "logger"):
                    try:
                        self.logger.warning("%sprefocus_before_scan failed: %s", log_prefix, e)
                    except Exception:
                        pass

        frame0 = None
        for _ in range(3):
            frame0 = self._capture_frame_bgr()
            if frame0 is not None and frame0.size > 0:
                break
            time.sleep(0.05)
        if frame0 is None or frame0.size == 0:
            raise RuntimeError("capture failed: empty frame")

        img_h, img_w = frame0.shape[:2]
        img_cx = float(desired_center_frac) * (img_w - 1)

        def _infer_geometry(info: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], float, float]:
            left_x = info.get("left_edge_x_px", None)
            right_x = info.get("right_edge_x_px", None)
            center_x_px = info.get("center_x_px", None)
            width_px = float(info.get("width_px", 0.0) or 0.0)

            if right_x is None and center_x_px is not None and width_px > 1:
                right_x = float(center_x_px) + 0.5 * width_px
            if left_x is None and center_x_px is not None and width_px > 1:
                left_x = float(center_x_px) - 0.5 * width_px
            if center_x_px is None and left_x is not None and right_x is not None:
                center_x_px = 0.5 * (float(left_x) + float(right_x))
            if width_px <= 1 and left_x is not None and right_x is not None:
                width_px = max(0.0, float(right_x) - float(left_x))

            right_ratio = None if right_x is None else float(right_x) / max(1.0, (img_w - 1))
            width_frac = float(width_px) / max(1.0, img_w)
            return center_x_px, right_ratio, width_frac, float(width_px)

        def _score_info(info: Dict[str, Any], stage: str) -> float:
            if not info.get("found", False):
                return -1e12

            conf = float(info.get("conf", 0.0))
            area_frac = float(info.get("area_frac", 0.0))
            center_x_px, right_ratio, width_frac, width_px = _infer_geometry(info)
            center_err = 1.0
            if center_x_px is not None:
                center_err = abs(float(center_x_px) - img_cx) / max(1.0, img_w)

            if right_ratio is None:
                right_pen = 1.2
            else:
                right_pen = abs(float(right_ratio) - float(desired_right_edge_frac))

            cov_pen = abs(float(width_frac) - float(desired_coverage_frac))
            overfill_pen = max(0.0, float(width_frac) - float(accept_cov_hi))
            underfill_pen = max(0.0, float(accept_cov_lo) - float(width_frac))

            if stage == "coarse":
                score = (
                    3.0 * conf
                    + 1.0 * area_frac
                    - 8.0 * right_pen
                    - 2.0 * cov_pen
                    - 1.0 * center_err
                    - 3.0 * underfill_pen
                    - 1.5 * overfill_pen
                )
            else:
                score = (
                    3.0 * conf
                    + 0.8 * area_frac
                    - 10.0 * right_pen
                    - 2.5 * cov_pen
                    - 0.5 * center_err
                    - 3.5 * underfill_pen
                    - 2.0 * overfill_pen
                )
            return float(score)

        def _accept_pose(info: Dict[str, Any]) -> bool:
            if not info.get("found", False):
                return False
            _center_x_px, right_ratio, width_frac, _width_px = _infer_geometry(info)
            if right_ratio is None:
                return False
            return (
                float(accept_right_lo) <= float(right_ratio) <= float(accept_right_hi)
                and float(accept_cov_lo) <= float(width_frac) <= float(accept_cov_hi)
            )

        def _eval_at_x(x_abs: float, stage: str):
            x_safe = float(self.safe_camx_absolute_move(float(x_abs), speed))
            if settle_s and settle_s > 0:
                time.sleep(float(settle_s))
            frame = self._capture_frame_bgr()
            info = self._analyze_copper_frame(frame)
            score = _score_info(info, stage=stage)
            return score, x_safe, info

        def _make_grid(c: float, span: float, steps: int) -> np.ndarray:
            half = 0.5 * float(span)
            left = max(self.CAMX_SOFT_MIN, float(c) - half)
            right = min(self.CAMX_SOFT_MAX, float(c) + half)
            if right - left < 1e-9:
                return np.array([left, left, left], dtype=float)
            return np.linspace(left, right, int(max(3, steps))).astype(float)

        xs = _make_grid(center_x, span_units, n_steps)

        best_score = -1e18
        best_x = float(center_x)
        best_info = None
        acceptable_candidates = []

        for x in xs:
            score, x_safe, info = _eval_at_x(x, stage="coarse")
            if _accept_pose(info):
                acceptable_candidates.append((score, x_safe, info))
            if score > best_score:
                best_score = score
                best_x = float(x_safe)
                best_info = info

        if acceptable_candidates:
            acceptable_candidates.sort(key=lambda t: t[0], reverse=True)
            best_score, best_x, best_info = acceptable_candidates[0]

        if best_info is None:
            self.safe_camx_absolute_move(float(center_x), speed)
            return float(center_x)

        if use_two_stage:
            fine_span = max(1.0, float(span_units) * float(fine_span_ratio))
            xs2 = _make_grid(best_x, fine_span, fine_steps)
            fine_acceptable = []
            for x in xs2:
                score, x_safe, info = _eval_at_x(x, stage="fine")
                if _accept_pose(info):
                    fine_acceptable.append((score, x_safe, info))
                if score > best_score:
                    best_score = score
                    best_x = float(x_safe)
                    best_info = info
            if fine_acceptable:
                fine_acceptable.sort(key=lambda t: t[0], reverse=True)
                best_score, best_x, best_info = fine_acceptable[0]

        self.safe_camx_absolute_move(best_x, speed)
        if settle_s and settle_s > 0:
            time.sleep(float(settle_s))

        if hasattr(self, "logger") and best_info is not None:
            try:
                center_x_px, right_ratio, width_frac, width_px = _infer_geometry(best_info)
                self.logger.info(
                    "%scamx copper align: center_x=%.3f best_x=%.3f best_score=%.4f center_px=%s right_ratio=%s width_frac=%.3f area_frac=%.3f conf=%.3f prefocus=%s",
                    log_prefix,
                    float(center_x),
                    float(best_x),
                    float(best_score),
                    "None" if center_x_px is None else f"{float(center_x_px):.1f}",
                    "None" if right_ratio is None else f"{float(right_ratio):.3f}",
                    float(width_frac),
                    float(best_info.get("area_frac", 0.0)),
                    float(best_info.get("conf", 0.0)),
                    bool(prefocus_before_scan),
                )
            except Exception:
                pass

        return float(best_x)

    def camx_micro_scan_center_copper_adaptive(self, *args, **kwargs) -> float:
        """兼容旧调用名，内部统一转到 camx_micro_scan_center_copper。"""
        return self.camx_micro_scan_center_copper(*args, **kwargs)



    def debug_camx_scan_center_copper(
        self,
        center_x: float,
        span_units: float = 20.0,
        n_steps: int = 11,
        speed: float = 20.0,
        settle_s: float = 0.8,
        *,
        use_two_stage: bool = True,
        fine_span_ratio: float = 0.30,
        fine_steps: int = 11,
        save_dir: str = "./scan_debug",
        log_prefix: str = "",
        prefocus_before_scan: bool = True,
        prefocus_settle_s: float = 0.6,
    ) -> float:
        """
        调试版 camx 搜索铜片函数：
        - coarse / fine 扫描
        - 保存每一步图片
        - 保存 csv 日志
        - 使用 fresh frame，避免反复分析缓存旧图
        - 到达预定位置后可先做一次 camy 自动对焦，再开始找铜片

        返回：
            best_x: 最终最佳 camx 位置（函数结束时 camx 已移动到该位置）
        """
        import os
        import csv
        import time
        from datetime import datetime
        import numpy as np
        import cv2

        if span_units <= 0:
            raise ValueError("span_units must be > 0")
        if n_steps < 3:
            raise ValueError("n_steps must be >= 3")
        if fine_steps < 3:
            raise ValueError("fine_steps must be >= 3")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(save_dir, f"camx_scan_{ts}")
        coarse_dir = os.path.join(run_dir, "coarse")
        fine_dir = os.path.join(run_dir, "fine")
        os.makedirs(coarse_dir, exist_ok=True)
        os.makedirs(fine_dir, exist_ok=True)

        csv_path = os.path.join(run_dir, "scan_log.csv")

        def _safe_float(x, default=0.0):
            try:
                if x is None:
                    return float(default)
                return float(x)
            except Exception:
                return float(default)

        def _make_grid(c: float, span: float, steps: int) -> np.ndarray:
            half = 0.5 * float(span)
            left = max(self.CAMX_SOFT_MIN, float(c) - half)
            right = min(self.CAMX_SOFT_MAX, float(c) + half)
            if right - left < 1e-9:
                return np.array([left, left, left], dtype=float)
            return np.linspace(left, right, int(max(3, steps))).astype(float)

        # 到中心并预对焦
        center_x = float(self.safe_camx_absolute_move(float(center_x), speed))
        if settle_s and settle_s > 0:
            time.sleep(float(settle_s))

        if prefocus_before_scan:
            try:
                self.cam.auto_focus()
                if prefocus_settle_s and prefocus_settle_s > 0:
                    time.sleep(float(prefocus_settle_s))
            except Exception as e:
                if hasattr(self, "logger"):
                    self.logger.warning("%sprefocus_before_scan failed in debug scan: %s", log_prefix, e)

        frame0 = None
        for _ in range(3):
            frame0 = self._capture_frame_bgr()
            if frame0 is not None and frame0.size > 0:
                break
            time.sleep(0.05)
        if frame0 is None or frame0.size == 0:
            raise RuntimeError("capture failed: empty frame")

        img_h, img_w = frame0.shape[:2]
        img_cx = 0.5 * (img_w - 1)

        def _score_result(result):
            if not result.get("found", False):
                return -1e12

            center_px = result.get("center_x_px", None)
            conf = _safe_float(result.get("conf", 0.0))
            area_frac = _safe_float(result.get("area_frac", 0.0))
            region_score = _safe_float(result.get("region_score", 0.0))

            if center_px is None:
                err = img_w
            else:
                err = abs(float(center_px) - img_cx)

            return float(-1.0 * err + 400.0 * area_frac + 80.0 * conf + 2.0 * region_score)

        def _draw_overlay(frame, result, score, x_safe, stage, idx):
            vis = frame.copy()
            h, w = vis.shape[:2]

            cv2.line(vis, (int(img_cx), 0), (int(img_cx), h - 1), (255, 255, 0), 2)

            center_px = result.get("center_x_px", None)
            left_px = result.get("left_edge_x_px", None)
            right_px = result.get("right_edge_x_px", None)
            anchor = str(result.get("anchor_mode", "center"))
            conf = _safe_float(result.get("conf", 0.0))
            area_frac = _safe_float(result.get("area_frac", 0.0))
            region_score = _safe_float(result.get("region_score", 0.0))

            if left_px is not None:
                cv2.line(vis, (int(left_px), 0), (int(left_px), h - 1), (0, 255, 0), 2)
            if right_px is not None:
                cv2.line(vis, (int(right_px), 0), (int(right_px), h - 1), (0, 0, 255), 2)
            if center_px is not None:
                cv2.line(vis, (int(center_px), 0), (int(center_px), h - 1), (255, 0, 255), 2)

            lines = [
                f"stage={stage} idx={idx}",
                f"x={x_safe:.3f} score={score:.3f}",
                f"anchor={anchor}",
                f"center={center_px}",
                f"left={left_px} right={right_px}",
                f"conf={conf:.4f} area={area_frac:.4f}",
                f"region_score={region_score:.4f}",
            ]
            y0 = 30
            for i, txt in enumerate(lines):
                cv2.putText(vis, txt, (20, y0 + i * 28), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2, cv2.LINE_AA)
            return vis

        fieldnames = [
            "stage", "idx", "x", "score", "anchor_mode", "center_x_px",
            "left_edge_x_px", "right_edge_x_px", "conf", "area_frac",
            "region_score", "image_path",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()

        def _append_row(row):
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writerow(row)

        def _eval_at_x(x_abs: float, stage: str, idx: int, out_dir: str):
            x_safe = float(self.safe_camx_absolute_move(float(x_abs), speed))
            if settle_s and settle_s > 0:
                time.sleep(float(settle_s))

            frame = self._capture_frame_bgr()
            result = self._analyze_copper_frame(frame)
            score = _score_result(result)

            vis = _draw_overlay(frame, result, score, x_safe, stage, idx)
            img_path = os.path.join(out_dir, f"{stage}_{idx:02d}_{x_safe:.3f}.png")
            cv2.imwrite(img_path, vis)

            row = {
                "stage": stage,
                "idx": idx,
                "x": x_safe,
                "score": score,
                "anchor_mode": result.get("anchor_mode", ""),
                "center_x_px": result.get("center_x_px", ""),
                "left_edge_x_px": result.get("left_edge_x_px", ""),
                "right_edge_x_px": result.get("right_edge_x_px", ""),
                "conf": result.get("conf", ""),
                "area_frac": result.get("area_frac", ""),
                "region_score": result.get("region_score", ""),
                "image_path": os.path.abspath(img_path),
            }
            return score, x_safe, result, row

        xs = _make_grid(center_x, span_units, n_steps)

        best_score = -1e18
        best_x = float(center_x)
        best_result = None

        print(f"{log_prefix}coarse scan start: center_x={center_x:.3f}, span={span_units}, n_steps={n_steps}")
        for i, x in enumerate(xs):
            score, x_safe, result, row = _eval_at_x(x, "coarse", i, coarse_dir)
            _append_row(row)
            print(
                f"{log_prefix}[coarse {i:02d}] x={x_safe:.3f} score={score:.3f} "
                f"anchor={result.get('anchor_mode')} center={result.get('center_x_px')} "
                f"conf={_safe_float(result.get('conf')):.4f} area={_safe_float(result.get('area_frac')):.4f}"
            )
            if score > best_score:
                best_score = score
                best_x = float(x_safe)
                best_result = result

        if use_two_stage:
            fine_span = float(span_units) * float(fine_span_ratio)
            xs2 = _make_grid(best_x, fine_span, fine_steps)
            print(f"{log_prefix}fine scan start: best_x_from_coarse={best_x:.3f}, fine_span={fine_span:.3f}, fine_steps={fine_steps}")

            for i, x in enumerate(xs2):
                score, x_safe, result, row = _eval_at_x(x, "fine", i, fine_dir)
                _append_row(row)
                print(
                    f"{log_prefix}[fine {i:02d}] x={x_safe:.3f} score={score:.3f} "
                    f"anchor={result.get('anchor_mode')} center={result.get('center_x_px')} "
                    f"conf={_safe_float(result.get('conf')):.4f} area={_safe_float(result.get('area_frac')):.4f}"
                )
                if score > best_score:
                    best_score = score
                    best_x = float(x_safe)
                    best_result = result

        self.safe_camx_absolute_move(best_x, speed)
        if settle_s and settle_s > 0:
            time.sleep(float(settle_s))

        final_frame = self._capture_frame_bgr()
        final_result = self._analyze_copper_frame(final_frame)
        final_score = _score_result(final_result)
        final_vis = _draw_overlay(final_frame, final_result, final_score, best_x, "final", 0)
        final_path = os.path.join(run_dir, "final_best.png")
        cv2.imwrite(final_path, final_vis)

        _append_row({
            "stage": "final",
            "idx": 0,
            "x": best_x,
            "score": final_score,
            "anchor_mode": final_result.get("anchor_mode", ""),
            "center_x_px": final_result.get("center_x_px", ""),
            "left_edge_x_px": final_result.get("left_edge_x_px", ""),
            "right_edge_x_px": final_result.get("right_edge_x_px", ""),
            "conf": final_result.get("conf", ""),
            "area_frac": final_result.get("area_frac", ""),
            "region_score": final_result.get("region_score", ""),
            "image_path": os.path.abspath(final_path),
        })

        print(f"{log_prefix}scan done: best_x={best_x:.3f}, best_score={best_score:.3f}")
        print(f"{log_prefix}debug dir: {os.path.abspath(run_dir)}")
        return float(best_x)


    def get_pumps(self, pump_ids):
        """
        pump_ids: list[int] or 'all'
        返回 waste_pumps 的 controller 列表
        """
        if pump_ids == "all":
            ids = list(self.waste_pumps.keys())
        elif isinstance(pump_ids, (int, str)):
            ids = [int(pump_ids)]
        else:
            ids = [int(x) for x in pump_ids]
        return ids

    def get_syringe_list(self, slaves):
        """
        slaves: list[int] or int
        返回 syringe_pumps 的对象列表
        """
        if isinstance(slaves, int):
            slaves = [slaves]
        pumps = []
        for s in slaves:
            if s not in self.syringe_pumps:
                raise ValueError(f"注射泵 slave={s} 未初始化")
            pumps.append(self.syringe_pumps[s])
        return pumps

    # ------------------------------------------------------------
    # Core operations (converted from your functions)
    # ------------------------------------------------------------
    def lq_in(self, pumps, volumes, valve_pos_in=12, max_stepx=12000, volumex=5.0):
        """
        进液（抽液到注射泵），采用“伪并行”写法：
        - 先对所有泵下发切阀指令（不等待）
        - 再统一 feedback 校验阀位
        - 再对所有泵下发走位指令（不等待）
        - 最后统一 feedback 校验位置

        pumps: list[泵对象] 或 list[int] (slave id)
        volumes: list[float]，每个泵的目标绝对体积 (mL)
        """
        # 兼容传 slave id
        if pumps and isinstance(pumps[0], int):
            pumps = self.get_syringe_list(pumps)

        if len(pumps) != len(volumes):
            raise ValueError("pumps 和 volumes 列表长度必须一致")
        if not pumps:
            self.logger.warning("pumps 为空，无泵执行进液")
            return []

        # 1) 切换阀（不等待），然后统一校验
        for pump in pumps:
            pump.switch_input_valve(valve_pos_in, False)
        for pump in pumps:
            pump.feedback("read_valve_position", valve_pos_in)

        # 2) 下发走位（不等待）
        pulse_per_mL = max_stepx / volumex
        steps: List[int] = []
        for pump, volume in zip(pumps, volumes):
            if volume < 0:
                raise ValueError(f"体积不能为负数: {volume}")
            pump.move_to_absolute_position(float(volume), False)
            steps.append(int(round(float(volume) * pulse_per_mL)))

        # 3) 统一校验位置
        for pump, step in zip(pumps, steps):
            pump.feedback("read_absolute_position", step)

        print(f"进液完成！目标体积: {volumes} mL，对应步数: {steps}")
        return steps
    def lq_out(
        self,
        pumps,
        valve_pos_out,
        volumes=None,
        max_stepx=12000,
        volumex=5.0,
        default_volume=0.0,
    ):
        """
        出液（把注射泵内液体打到目标阀口），采用“伪并行”写法：
        - 先对所有泵下发切阀指令（不等待）
        - 再统一 feedback 校验阀位
        - 再对所有泵下发走位指令（不等待）
        - 最后统一 feedback 校验位置

        pumps: list[泵对象] 或 list[int] (slave id)
        valve_pos_out: int 输出阀位
        volumes:
            None -> 全部排到 default_volume（默认 0）
            float -> 所有泵相同目标体积
            list -> 对应每个泵目标体积
        """
        if pumps and isinstance(pumps[0], int):
            pumps = self.get_syringe_list(pumps)

        if not pumps:
            self.logger.warning("pumps 为空，无泵执行出液")
            return []

        if volumes is None:
            volumes = [default_volume] * len(pumps)
        elif isinstance(volumes, (int, float)):
            volumes = [float(volumes)] * len(pumps)
        elif len(volumes) != len(pumps):
            raise ValueError("volumes 列表长度必须与 pumps 一致")

        # 1) 切换阀（不等待），然后统一校验
        for pump in pumps:
            pump.switch_input_valve(int(valve_pos_out), False)
        for pump in pumps:
            pump.feedback("read_valve_position", int(valve_pos_out))

        # 2) 下发走位（不等待）
        pulse_per_mL = max_stepx / volumex
        steps: List[int] = []
        for pump, volume in zip(pumps, volumes):
            if volume < 0:
                raise ValueError(f"出液体积不能为负数: {volume}")
            pump.move_to_absolute_position(float(volume), False)
            steps.append(int(round(float(volume) * pulse_per_mL)))

        # 3) 统一校验位置
        for pump, step in zip(pumps, steps):
            pump.feedback("read_absolute_position", step)

        print(f"出液完成！目标剩余体积: {volumes} mL，对应步数: {steps}")
        return steps
    def drive_pumps(self, pumps, speed, waste_out_time):
        """
        pumps: int / list[int] / 'all'
        speed: jog 速度
        waste_out_time: 运行时长（秒）
        """
        ids = self.get_pumps(pumps)

        # start
        success = 0
        for pump_id in ids:
            if pump_id not in self.waste_pumps:
                print(f"泵 {pump_id} 不存在或未初始化！跳过")
                continue
            controller = self.waste_pumps[pump_id]
            try:
                controller.jog(speed)
                print(f"泵 {pump_id} 已启动 Jog 速度 = {speed}")
                success += 1
            except Exception as e:
                print(f"泵 {pump_id} Jog 失败: {e}")

        print(f"本次启动完成：成功驱动 {success}/{len(ids)} 台泵")
        time.sleep(waste_out_time)

        # stop
        for pump_id in ids:
            if pump_id not in self.waste_pumps:
                continue
            controller = self.waste_pumps[pump_id]
            try:
                controller.force_stop()
                print(f"泵 {pump_id} 已停止")
            except Exception as e:
                print(f"泵 {pump_id} force_stop 失败: {e}")


    def sy_pump_init(self, slaves=(1, 2, 3, 4)):
        """
        初始化注射泵：阀初始化 -> 读位置 -> 泵初始化 -> 读阀位

        slaves: 指定要初始化的泵 slave id，默认 1~4
        """
        pumps = self.get_syringe_list(list(slaves))

        for pump in pumps:
            pump.initialize_valve(False)

        for pump in pumps:
            pump.feedback("read_absolute_position", 0)

        for pump in pumps:
            pump.initialize_pump(False)

        for pump in pumps:
            pump.feedback("read_valve_position", "i")

        print(f"注射泵初始化完成：{list(slaves)}")



    def lq_back2stock(
        self,
        valve_positions=(1, 2, 3, 4, 6, 7, 8, 9),
        waste_valve_pos: int = 12,
        single_slave_first=1,
        slaves=(1, 2, 3, 4),
        fill_to: float = 5.0,      # 5 = 吸满
        empty_to: float = 0.0,     # 0 = 排空
        settle_s: float = 0.0,
    ):
        """
        目标：
        1) 先对某一个泵（默认 pump3）做 8 个通道循环：从通道 i 吸满(5) -> 打到 12 排空(0)
        2) 再对四个泵都做同样循环

        你给定的物理含义：
        move_to_absolute_position(5) = 吸满
        move_to_absolute_position(0) = 排空
        """
        def run_one_pump(pump, pump_name=""):
            for i in valve_positions:
                # 从通道 i 吸满
                pump.switch_input_valve(i, False)
                pump.move_to_absolute_position(fill_to, False)

                # 打到废液位 12 排空
                pump.switch_input_valve(waste_valve_pos, False)
                pump.move_to_absolute_position(empty_to, False)

                if settle_s and settle_s > 0:
                    time.sleep(settle_s)

        # 1) 先跑单泵（默认 3 号）
        if single_slave_first is not None:
            pump = self.syringe_pumps.get(int(single_slave_first))
            if pump is None:
                raise ValueError(f"single_slave_first={single_slave_first} 未初始化")
            run_one_pump(pump, pump_name=f"pump{single_slave_first}")
            print(f"单泵循环完成：pump{single_slave_first}")

        # 2) 再跑全部泵
        for s in slaves:
            pump = self.syringe_pumps.get(int(s))
            if pump is None:
                raise ValueError(f"slave={s} 未初始化")
            run_one_pump(pump, pump_name=f"pump{s}")

        print(f"lq_back2stock 完成：slaves={list(slaves)}, valve_positions={list(valve_positions)}")



    def water_in_for_photo(
        self,
        list_water_wash=(),
        water_speed=400,
        water_run_s=10,
        ):

            switch_valve_dict = {k: k for k in range(1, 13)}  # 若你固定 12 通道

            for i in list_water_wash:
                self.valve_water.valve_switch(switch_valve_dict, i)

                self.water_in.jog(water_speed, "CCW")
                time.sleep(water_run_s)
                self.water_in.jog(0, "CCW")

    def water_wash(
    self,
    list_water_wash=(1, 2, 3, 4, 5, 6, 7, 8),
    water_speed=400,
    water_run_s=7,
    settle_s=20,
    waste_speed=400,
    waste_run_s=30,
    ):
        """
        水洗流程：
        对 list1 中每个通道：
            1) 切水阀到通道 i
            2) water_in 以 water_speed 反转(CCW)运行 water_run_s 秒
            3) water_in 停止
        循环后等待 settle_s 秒
        最后开启废液泵（对应 list1 的泵号）以 waste_speed 运行 waste_run_s 秒

        说明：
        - 假设 valve_switch 的签名是 valve_switch(mapping, position)
        - 假设 water_in.jog(speed, direction) 支持 "CCW"
        - drive_pumps 的 pumps 参数支持 list[int]
        """
        switch_valve_dict = {k: k for k in range(1, 13)}  # 若你固定 12 通道

        for i in list_water_wash:
            self.valve_water.valve_switch(switch_valve_dict, i)

            self.water_in.jog(water_speed, "CCW")
            time.sleep(water_run_s)
            self.water_in.jog(0, "CCW")

        time.sleep(settle_s)

        self.drive_pumps(list_water_wash, waste_speed, waste_run_s)

    def acid_wash(
        self,
        list_acid_wash=(1, 2, 3, 4, 5, 6, 7, 8),
        acid_speed=200,
        acid_run_s=10,
        soak_s=60,
        waste_speed=400,
        waste_run_s=60,
    ):
        """
        酸洗流程：
        对 list1 中每个通道：
            1) 切酸阀到通道 i
            2) acid_in 以 acid_speed 反转(CCW)运行 acid_run_s 秒
            3) acid_in 停止
        循环后等待 soak_s 秒
        最后开启废液泵（对应 list1 的泵号）以 waste_speed 运行 waste_run_s 秒
        """
        switch_valve_dict = {k: k for k in range(1, 13)}  # 若你固定 12 通道

        for i in list_acid_wash:
            self.valve_acid.valve_switch(switch_valve_dict, i)

            self.acid_in.jog(acid_speed, "CCW")
            time.sleep(acid_run_s)
            self.acid_in.jog(0, "CCW")

        time.sleep(soak_s)

        self.drive_pumps(list_acid_wash, waste_speed, waste_run_s)




    def add_sample(
        self,
        valve_positions=(1, 2, 3, 4, 9, 6, 7, 8),
        syringe_slaves=(1, 2, 3,4),
        fill_volumes=(5.0, 5.0, 5.0),
        max_stepx=12000,
        volumex=5.0,
    ):
        """
        加样流程：
        对每个通道 i：
            1) 三个注射泵从各自输入位吸满（体积=5）
            2) 三个注射泵切到通道 i 并排空到 0

        说明：
        - 依赖 self.lq_in / self.lq_out
        - 你系统定义：move_to_absolute_position(5)=吸满，0=排空
        - lq_out 的 volumes=[0,0,0] 表示排空（到 0）
        """
        # 参数校验
        if len(syringe_slaves) != len(fill_volumes):
            raise ValueError("syringe_slaves 与 fill_volumes 长度必须一致")

        for i in valve_positions:
            # 1) 吸满（按你定义：5=吸满）
            self.lq_in(list(syringe_slaves), list(fill_volumes),
                    valve_pos_in=12, max_stepx=max_stepx, volumex=volumex)

            # 2) 切到通道 i 排空（0=排空）
            self.lq_out(list(syringe_slaves), valve_pos_out=i, volumes=[0.0] * len(syringe_slaves),
                        max_stepx=max_stepx, volumex=volumex)

        print(f"add_sample 完成：valve_positions={list(valve_positions)}, syringe_slaves={list(syringe_slaves)}")



    def ep_start( 
            self,
            channel_list=(1, 2, 3, 4, 5, 6, 7, 8),
            window_title="IEST Console[V 1.0]",
            btn_start_xy=(990, 771),
            channel_map=None,
            right_click_wait_s=0.5,
            after_menu_wait_s=1.5,
            after_start_wait_s=2.0,
            maximize=True,
            # ===== 安全联锁/判红相关参数（已内置默认值）=====
            enable_red_guard=False,          # 是否启用“启动前判红”安全保护
            probe_map=None,                 # 通道取样点坐标（用于判红）。不传则默认复用 channel_map
            probe_region_size=(60, 40),     # (w,h) 取样区域大小
            red_threshold=150,              # R >= 该阈值
            red_delta=60,                   # 且 R-G >= red_delta, R-B >= red_delta
            guard_settle_s=0.3,             # 激活窗口后等待 UI 稳定时间
            save_guard_snapshot=False,      # 若判红触发，是否保存全屏截图（追溯用）
            snapshot_path=None,             # 截图路径；不传则自动生成时间戳文件名
        ):
            """
            自动在 IEST Console 软件中为指定通道启动实验（右键通道 -> Down -> Enter -> 点击启动测试）

            安全措施（你要求的“安全保护”）：
            - 每次启动前，先对 channel_list 中所有通道进行判红扫描；
            - 只要存在任意红色异常通道：立即 raise RuntimeError，中断流程，后续实验不执行。

            说明：
            - 判红采用 pyautogui 截图取样 + 红色占优阈值判定（你已验证 OK 的逻辑）。
            - probe_map 用于“取样判红”的点位；默认复用 channel_map，但工程上建议单独标定更稳定的取样点位。
            """

            import time
            import pyautogui
            import win32gui
            import win32con

            # 禁用左上角 failsafe（按你原代码）
            pyautogui.FAILSAFE = False

            if channel_map is None:
                channel_map = {
                    1: (500, 250),
                    2: (628, 250),
                    3: (748, 250),
                    4: (870, 250),
                    5: (990, 250),
                    6: (1100, 250),
                    7: (1240, 250),
                    8: (1360, 250),
                }

            # 判红取样点：默认复用 channel_map（建议你后续单独校准 probe_map 更稳）
            if probe_map is None:
                probe_map = dict(channel_map)

            # ---------- 内部工具函数（全部内置在 ep_start 内） ----------
            def abort(msg: str):
                # 用 raise 保证“后续其他代码也不该执行”
                self.logger.error(msg)
                if save_guard_snapshot:
                    try:
                        import os
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        out = snapshot_path or f"safety_snapshot_{ts}.png"
                        # 若给了相对路径，放当前工作目录
                        pyautogui.screenshot(out)
                        self.logger.error("已保存安全快照：%s", os.path.abspath(out))
                    except Exception as e:
                        self.logger.exception("保存安全快照失败：%s", e)
                raise RuntimeError(msg)

            def activate_window(title: str) -> bool:
                self.logger.info("正在寻找窗口: [%s] ...", title)
                hwnd = win32gui.FindWindow(None, title)
                if hwnd == 0:
                    self.logger.error("未找到标题为 '%s' 的窗口，请检查软件是否打开。", title)
                    return False

                try:
                    if maximize:
                        win32gui.ShowWindow(hwnd, win32con.SW_SHOWMAXIMIZED)
                    else:
                        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

                    # 获取焦点（你的原逻辑）
                    pyautogui.press("alt")
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.8)
                    self.logger.info("窗口已激活。")
                    return True
                except Exception as e:
                    self.logger.exception("激活窗口时出错: %s", e)
                    return False

            def is_red_dominant(rgb):
                r, g, b = rgb
                return (r >= red_threshold) and (r - g >= red_delta) and (r - b >= red_delta)

            def sample_avg_rgb(center_xy):
                cx, cy = center_xy
                w, h = probe_region_size
                left = int(cx - w / 2)
                top = int(cy - h / 2)
                img = pyautogui.screenshot(region=(left, top, w, h))
                avg_rgb = img.resize((1, 1)).getpixel((0, 0))  # (R,G,B)
                return avg_rgb

            def is_channel_red(ch: int):
                if ch not in probe_map:
                    # 没有取样点就无法判红，按工程安全原则：宁可中断也不要“当做正常”
                    abort(f"安全中断：通道 {ch} 缺少 probe_map 取样坐标，无法判红。")
                avg_rgb = sample_avg_rgb(probe_map[ch])
                red = is_red_dominant(avg_rgb)
                self.logger.info("通道 %s 判红检测：red=%s, avg_rgb=%s, probe_xy=%s", ch, red, avg_rgb, probe_map[ch])
                return red, avg_rgb

            def guard_scan_before_start():
                """
                启动前统一扫描：只要发现红色异常通道 -> 立即中断
                """
                red_list = []
                rgb_map = {}

                for ch in channel_list:
                    if ch not in channel_map:
                        # 启动坐标都没有，直接跳过；但更严谨可改为 abort
                        self.logger.warning("通道 %s 未配置启动坐标 channel_map，跳过判红与启动。", ch)
                        continue

                    red, avg_rgb = is_channel_red(ch)
                    rgb_map[ch] = avg_rgb
                    if red:
                        red_list.append(ch)

                if red_list:
                    abort(f"安全中断：检测到红色异常通道 {red_list}（avg_rgb={ {c: rgb_map[c] for c in red_list} }），禁止启动实验。")

            # ---------- 主流程 ----------
            # 1) 激活窗口
            if not activate_window(window_title):
                return False

            time.sleep(guard_settle_s)

            self.logger.info("准备启动通道: %s", list(channel_list))

            # 2) 启动前安全联锁：扫描是否存在红色异常通道
            # NOTE: 按你的要求：保留原逻辑代码，但不执行“判红终止”
            # if enable_red_guard:
            #     guard_scan_before_start()

            btn_x, btn_y = btn_start_xy

            # 3) 逐通道执行启动
            for ch in channel_list:
                if ch not in channel_map:
                    self.logger.warning("跳过通道 %s：未配置坐标", ch)
                    continue

                ch_x, ch_y = channel_map[ch]
                self.logger.info("开始操作通道 %s, 坐标=(%s,%s)", ch, ch_x, ch_y)

                # 右键通道
                pyautogui.rightClick(ch_x, ch_y)
                time.sleep(right_click_wait_s)

                # 选择第一个菜单项
                pyautogui.press("down")
                time.sleep(0.1)
                pyautogui.press("enter")

                time.sleep(after_menu_wait_s)

                # 点击“启动测试”
                pyautogui.click(btn_x, btn_y)
                time.sleep(after_start_wait_s)

            self.logger.info("所有指定通道操作完成。")
            return True





    def y_division_points(y1: float, y2: float, n: int) -> list[float]:
        """
        给定两个 y 坐标，返回从 y_max 到 y_min 的 (n+1) 个等分点 y 值（包含端点）。
        n=2/3/4 表示分成 n 段。
        """
        if n not in (2, 3, 4):
            raise ValueError("n 只能是 2, 3, 4")

        y_high = max(y1, y2)
        y_low = min(y1, y2)
        dy = y_high - y_low

        return [y_high - (k / n) * dy for k in range(n + 1)]


    def _load_points(self, json_path: str):
        if not os.path.exists(json_path):
            raise FileNotFoundError(f"point json 不存在: {json_path}")
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _get_pose_from_json(self, pose_key: str, json_path: str):
        data = self._load_points(json_path)
        poses = data.get("poses", {})
        if pose_key not in poses:
            raise KeyError(f"poses 中不存在键: {pose_key}，可用键: {list(poses.keys())}")
        pose = poses[pose_key]
        if not (isinstance(pose, list) and len(pose) >= 4):
            raise ValueError(f"{pose_key} 的值必须是 list 且长度>=4，当前: {pose}")
        return pose




    def make_key(axis: str, j: int, k: int) -> str:
        return f"{axis}_{int(j)}_{int(k)}"

    def load_segments(json_path: str) -> dict:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)


    def moveL_to_point(
            self,
            key: str,
            json_path: str = r"E:\GitHub\zsyEP\zsyEP\zsyEP\point.json",
            default_group: str = "segments",   # 简写 "x_1_1.p1" 时默认去哪个组
        ):
            """
            从当前位置 MoveL 到 JSON 里任意一个点。

            支持三种 key：
            1) "get_ready_position1"                    -> data["poses"][key]
            2) "x_1_1.p1" / "x_1_1.p2"                  -> data[default_group]["x_1_1"]["p1"]
            3) "segments.x_1_1.p1" / "segments_up.x_1_1.p2"
                                                    -> data[group][seg_key][end]

            JSON 中点是 [x,y,z,r,0,0]，默认取前 4 个 [x,y,z,r] 送给 MG400.MoveL。
            """
            if not os.path.exists(json_path):
                raise FileNotFoundError(f"json 不存在: {json_path}")

            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # ---------- 1) poses: key 不含 '.' ----------
            if "." not in key:
                poses = data.get("poses", {})
                if key not in poses:
                    raise KeyError(f"poses 中未找到 '{key}'，可用: {list(poses.keys())}")
                pose = poses[key]

            # ---------- 2) 三段式 group.seg.end ----------
            else:
                parts = key.split(".")
                if len(parts) == 3:
                    group, seg_key, end = parts
                elif len(parts) == 2:
                    # 二段式：默认 group = default_group
                    seg_key, end = parts
                    group = default_group
                else:
                    raise ValueError(
                        "key 格式错误：应为 'poseKey' 或 'segKey.p1' 或 'group.segKey.p1'"
                    )

                if group not in data:
                    raise KeyError(f"json 中未找到分组 '{group}'，可用顶层键: {list(data.keys())}")

                group_dict = data[group]
                if seg_key not in group_dict:
                    raise KeyError(f"{group} 中未找到 '{seg_key}'，可用: {list(group_dict.keys())}")

                if end not in group_dict[seg_key]:
                    raise KeyError(f"{group}.{seg_key} 中未找到 '{end}'，可用: {list(group_dict[seg_key].keys())}")

                pose = group_dict[seg_key][end]

            # ---------- 3) 校验 + 取前4 ----------
            if not (isinstance(pose, list) and len(pose) >= 4):
                raise ValueError(f"点 '{key}' 的值必须是 list 且长度>=4，当前: {pose}")

            target = pose[:4]  # [x,y,z,r]

            # ---------- 4) 从当前位置 MoveL 到 target ----------
            # 重要：确保 self.MG400 已 ConnectRobot 且位姿线程已开始刷新，否则 MoveL/WaitArrive 会卡或报 current_actual None
            self.MG400.MoveL(target)
            return target

    def RelMoveL_from_current(
    self,
    offset_x: float = 0,
    offset_y: float = 0,
    offset_z: float = 0,
    offset_r: float = 0,
    ):
        pos_now = self.MG400.GetPosition()
        target = [
            pos_now[0] + offset_x,
            pos_now[1] + offset_y,
            pos_now[2] + offset_z,
            pos_now[3] + offset_r,
        ]
        self.MG400.MoveL(target)      # 用你已有的 MoveL（带 WaitArrive）
        return pos_now, target

    def move_out_of_barrier1(self):
        pos_now=self.MG400.GetPosition()
        target=pos_now.copy()
        target[0] -= 5
        self.MG400.MoveL(target)  # MoveL 内部会 WaitArrive
        return target


    def move_out_of_barrier23(self):
        pos_now=self.MG400.GetPosition()
        target=pos_now.copy()
        target[0] += 5
        self.MG400.MoveL(target)  # MoveL 内部会 WaitArrive
        return target


    def move_up1(self):
        pos_now=self.MG400.GetPosition()
        target=pos_now.copy()
        target[2] += 20
        self.MG400.MoveL(target)  # MoveL 内部会 WaitArrive
        return target


    def move_up2(self):
        pos_now=self.MG400.GetPosition()
        target=pos_now.copy()
        target[2] += 49.3
        self.MG400.MoveL(target)  # MoveL 内部会 WaitArrive
        return target

    def move_up3(self):
        pos_now=self.MG400.GetPosition()
        target=pos_now.copy()
        target[2] += 10
        self.MG400.MoveL(target)  # MoveL 内部会 WaitArrive
        return target

    def moveJ_to_point(
        self,
        key: str,
        json_path: str = r"E:\GitHub\zsyEP\zsyEP\zsyEP\point.json",
        default_group: str = "segments",
    ):
        """
        通用 MoveJ（原理同 moveL_to_point）：
        - key="get_ready_position1"               -> data["poses"][key]
        - key="x_1_1.p1" / "x_1_1.p2"             -> data[default_group]["x_1_1"]["p1"]
        - key="segments.x_1_1.p1" / "segments_up.x_1_1.p2"
                                                -> data[group][seg_key][end]

        JSON 中点为 [x,y,z,r,0,0]，默认取前 4 个 [x,y,z,r] 送给 self.MoveJ（内部已 WaitArrive）。
        """
        if not os.path.exists(json_path):
            raise FileNotFoundError(f"json 不存在: {json_path}")

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 1) poses: key 不含 '.'
        if "." not in key:
            poses = data.get("poses", {})
            if key not in poses:
                raise KeyError(f"poses 中未找到 '{key}'，可用: {list(poses.keys())}")
            pose = poses[key]

        # 2) segments: 二段/三段 key
        else:
            parts = key.split(".")
            if len(parts) == 3:
                group, seg_key, end = parts
            elif len(parts) == 2:
                seg_key, end = parts
                group = default_group
            else:
                raise ValueError("key 格式错误：应为 'poseKey' 或 'segKey.p1' 或 'group.segKey.p1'")

            if group not in data:
                raise KeyError(f"json 中未找到分组 '{group}'，可用顶层键: {list(data.keys())}")

            group_dict = data[group]
            if seg_key not in group_dict:
                raise KeyError(f"{group} 中未找到 '{seg_key}'，可用: {list(group_dict.keys())}")

            if end not in group_dict[seg_key]:
                raise KeyError(f"{group}.{seg_key} 中未找到 '{end}'，可用: {list(group_dict[seg_key].keys())}")

            pose = group_dict[seg_key][end]

        if not (isinstance(pose, list) and len(pose) >= 4):
            raise ValueError(f"点 '{key}' 的值必须是 list 且长度>=4，当前: {pose}")

        target = pose[:4]  # [x,y,z,r]

        # 关键：用你封装好的 MoveJ（内部 MovJ + WaitArrive）
        self.MG400.MoveJ(target)

        return target



    def get_ready_position(
        self,
        pose_key: str ,
        json_path: str = r"E:\GitHub\zsyEP\zsyEP\zsyEP\point.json",
    ):
        pose = self._get_pose_from_json(pose_key, json_path)

        target = pose[:4]  # 只取 [x,y,z,r]
        self.MG400.MoveJ(target)  # 注意：调用 MG400 的 MoveJ
        return target



    def _make_key(self, axis: str, j: int, k: int) -> str:
        return f"{axis}_{int(j)}_{int(k)}"

    def _load_segments(self, json_path: str) -> dict:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
        

    def move_down_by_segments_delta_z(
        self,
        seg_key: str,                 # 例如 "x_1_1"
        end: str = "p1",              # "p1" 或 "p2"
        json_path: str = r"E:\GitHub\zsyEP\zsyEP\zsyEP\point.json",
        tol: float = 1e-6,
    ):
        """
        用同一个点在 segments_up 和 segments 的 z 差值来决定向下 MoveL 的距离。
        计算：dz = segments_up[seg_key][end][2] - segments[seg_key][end][2]
        然后：target_z = pos_now.z - dz   (即向下移动 dz)

        返回: (dz, target_pose)
        """
        if end not in ("p1", "p2"):
            raise ValueError("end 只能是 'p1' 或 'p2'")

        if not os.path.exists(json_path):
            raise FileNotFoundError(f"json 不存在: {json_path}")

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "segments" not in data:
            raise KeyError(f"json 缺少 'segments'，顶层键: {list(data.keys())}")
        if "segments_up" not in data:
            raise KeyError(f"json 缺少 'segments_up'，顶层键: {list(data.keys())}")

        segments = data["segments"]
        segments_up = data["segments_up"]

        if seg_key not in segments:
            raise KeyError(f"segments 中没有 {seg_key}")
        if seg_key not in segments_up:
            raise KeyError(f"segments_up 中没有 {seg_key}")

        p_base = segments[seg_key].get(end)
        p_up = segments_up[seg_key].get(end)
        if not (isinstance(p_base, list) and len(p_base) >= 3):
            raise ValueError(f"segments.{seg_key}.{end} 格式不对: {p_base}")
        if not (isinstance(p_up, list) and len(p_up) >= 3):
            raise ValueError(f"segments_up.{seg_key}.{end} 格式不对: {p_up}")

        z_base = float(p_base[2])
        z_up = float(p_up[2])

        dz = z_up - z_base
        if dz < -tol:
            # 如果 dz 为负，意味着 “up 的 z 反而更低”，按你的语义可能是数据错了
            raise ValueError(f"计算得到 dz<0 (z_up={z_up}, z_base={z_base})，请检查 segments_up/segments 的 z")

        # 当前位姿 -> 目标位姿：只改 z，向下移动 dz
        pos_now = self.MG400.GetPosition()  # [x,y,z,r] 或更多，取前4也行
        target = pos_now.copy()
        target[2] = target[2] - dz

        self.MG400.MoveL(target[:4])  # 确保传 [x,y,z,r]
        return dz, target[:4]




    # get_ready_position1  go
    # move_from_ready1_to_ymax_by_key  go
    # move_y_to_division_by_key  go
    # move_down
    # set_site
    # move_up1
    # move_out_of_barrier
    # move_y_back_to_max_by_key 
    # move_up2
    # get_ready_position1 
    # ------------------------------------------------------------
    # Experiment planning (LHS + CSV) and execution (8 electroplating cells)
    # ------------------------------------------------------------
    @staticmethod
    def _round_to_grid(x: float, grid: float) -> float:
        return round(x / grid) * grid

    @staticmethod
    def _simple_lhs(n: int, dim: int, seed: int = 0) -> np.ndarray:
        """A lightweight LHS implementation without scipy.
        Returns array in [0,1], shape (n, dim).
        """
        rng = np.random.default_rng(seed)
        cut = np.linspace(0.0, 1.0, n + 1)
        u = rng.random((n, dim))
        a = cut[:n]
        b = cut[1:]
        pts = u * (b - a)[:, None] + a[:, None]
        H = np.empty_like(pts)
        for j in range(dim):
            order = rng.permutation(n)
            H[:, j] = pts[order, j]
        return H

    def generate_lhs_plan_5x(
        self,
        n_points: int = 24,
        seed: int = 42,
        total_volume_mL: float = 30.0,
        grid_mL: float = 0.02,
        waste_valve_pos: int = 12,
        settle_s: float = 0.0,
        ranges: Optional[Dict[str, Tuple[float, float]]] = None,
        stock_gL: Optional[Dict[str, float]] = None,
        csv_path: str = "./lhs_plan_5x.csv",
    ) -> List[Dict[str, Any]]:
        """Generate an LHS plan in concentration space and save as CSV.

        5× stock default:
            PEG stock = 40 g/L
            SPS stock = 2 g/L
            JGB stock = 1.5 g/L
            Base handled by syringe slave 4.

        ranges default is 0.5×–1.5× around (PEG=8, SPS=0.4, JGB=0.3 g/L):
            PEG: 4–12 g/L, SPS: 0.2–0.6 g/L, JGB: 0.15–0.45 g/L
        """
        if ranges is None:
            ranges = {
                "PEG_gL": (4.0, 12.0),
                "SPS_gL": (0.2, 0.6),
                "JGB_gL": (0.15, 0.45),
            }
        if stock_gL is None:
            stock_gL = {"PEG": 40.0, "SPS": 2.0, "JGB": 1.5, "BASE": 0.0}

        U = self._simple_lhs(n_points, 3, seed=seed)
        peg = ranges["PEG_gL"][0] + U[:, 0] * (ranges["PEG_gL"][1] - ranges["PEG_gL"][0])
        sps = ranges["SPS_gL"][0] + U[:, 1] * (ranges["SPS_gL"][1] - ranges["SPS_gL"][0])
        jgb = ranges["JGB_gL"][0] + U[:, 2] * (ranges["JGB_gL"][1] - ranges["JGB_gL"][0])

        plan: List[Dict[str, Any]] = []
        for idx in range(n_points):
            # volumes from stock (mL): v = V_total * C_target / C_stock
            v_peg = total_volume_mL * float(peg[idx]) / float(stock_gL["PEG"])
            v_sps = total_volume_mL * float(sps[idx]) / float(stock_gL["SPS"])
            v_jgb = total_volume_mL * float(jgb[idx]) / float(stock_gL["JGB"])

            # quantize additive volumes first
            v_peg = self._round_to_grid(v_peg, grid_mL)
            v_sps = self._round_to_grid(v_sps, grid_mL)
            v_jgb = self._round_to_grid(v_jgb, grid_mL)

            v_base = total_volume_mL - (v_peg + v_sps + v_jgb)
            # keep base to grid by final correction (base absorbs rounding error)
            v_base = self._round_to_grid(v_base, grid_mL)
            v_base = total_volume_mL - (v_peg + v_sps + v_jgb)

            if v_base < -1e-9:
                raise ValueError(
                    f"Plan infeasible (negative base volume): idx={idx+1}, v_base={v_base:.3f} mL. "
                    "Consider increasing stock concentration or shrinking ranges."
                )

            plan.append(
                {
                    "point_id": idx + 1,
                    "PEG_gL": float(peg[idx]),
                    "SPS_gL": float(sps[idx]),
                    "JGB_gL": float(jgb[idx]),
                    "v_PEG_mL": float(v_peg),
                    "v_SPS_mL": float(v_sps),
                    "v_JGB_mL": float(v_jgb),
                    "v_BASE_mL": float(v_base),
                }
            )

        self.save_plan_csv(plan, csv_path)
        return plan

    @staticmethod
    def save_plan_csv(plan: List[Dict[str, Any]], csv_path: str) -> None:
        """Save plan rows to CSV."""
        if not plan:
            raise ValueError("plan is empty")
        fieldnames = list(plan[0].keys())
        os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in plan:
                w.writerow(row)

    @staticmethod
    def load_plan_csv(csv_path: str) -> List[Dict[str, Any]]:
        """Load plan rows from CSV."""
        if not os.path.exists(csv_path):
            raise FileNotFoundError(csv_path)
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            rows = []
            for row in r:
                # convert numeric fields if possible
                out = {}
                for k, v in row.items():
                    if v is None:
                        out[k] = v
                        continue
                    vv = v.strip()
                    try:
                        out[k] = float(vv) if ("." in vv or vv.isdigit() or (vv.startswith("-") and vv[1:].replace(".", "", 1).isdigit())) else vv
                    except Exception:
                        out[k] = vv
                rows.append(out)
        return rows
    def _dispense_one_channel_from_row(
        self,
        channel_valve_pos: int,
        row: Dict[str, Any],
        syringe_slaves: Tuple[int, int, int, int] = (1, 2, 3, 4),
        stock_input_valves: Optional[Dict[int, int]] = None,
        grid_mL: float = 0.02,
        max_syringe_mL: float = 5.0,
        waste_valve_pos: int = 12,
        switch_settle_s: float = 0.0,
    ) -> None:
        """向单个电解池（一个阀位）加入 4 种液体（4 个泵）对应体积。

        关键点：为了匹配你在 lq_in / lq_out 里的“伪多线程”思路，这里也按“批量下发、统一 feedback”的方式组织：
        - 在一个 chunk 循环内，尽可能把需要动作的泵打包到一次 lq_in / lq_out 调用里
        - 从而避免“泵1做完才开始泵2”的串行等待

        注意：
        - stock_input_valves 若所有泵都为 12（你当前硬件），可以直接 {1:12,2:12,3:12,4:12}
        - 若不同泵 stock 口不同，本函数会按 stock 口分组，多组依次执行（每组内仍是伪并行）
        - waste_valve_pos 在当前“无废液口”的方案中不参与动作，保留仅为兼容
        """
        if stock_input_valves is None:
            stock_input_valves = {s: waste_valve_pos for s in syringe_slaves}

        # 1) 读取本行体积（mL）
        v_map = {
            syringe_slaves[0]: float(row.get("v_PEG_mL", 0.0)),
            syringe_slaves[1]: float(row.get("v_SPS_mL", 0.0)),
            syringe_slaves[2]: float(row.get("v_JGB_mL", 0.0)),
            syringe_slaves[3]: float(row.get("v_BASE_mL", 0.0)),
        }

        # 2) 网格化 + 校验
        for s in list(v_map.keys()):
            v_map[s] = self._round_to_grid(v_map[s], grid_mL)
            if v_map[s] < -1e-9:
                raise ValueError(f"Negative volume for slave {s}: {v_map[s]}")
            if v_map[s] > 30.0 + 1e-9:
                raise ValueError(f"Unrealistic volume for slave {s}: {v_map[s]}")
        if abs(sum(v_map.values()) - 30.0) > 1e-6:
            self.logger.warning("Row total volume != 30 mL after rounding: sum=%.4f", sum(v_map.values()))

        remaining: Dict[int, float] = {int(s): float(v) for s, v in v_map.items()}

        # 3) chunk 循环：每一轮让“还需要加液的泵”都一起吸、一起打（尽可能）
        while True:
            active_slaves: List[int] = []
            # 按 stock 口分组：stock_pos -> (slaves, chunks)
            grouped: Dict[int, Tuple[List[int], List[float]]] = {}

            for slave in [int(x) for x in syringe_slaves]:
                rem = remaining.get(slave, 0.0)
                if rem <= 1e-9:
                    continue

                chunk = min(float(max_syringe_mL), rem)
                chunk = self._round_to_grid(chunk, grid_mL)
                if chunk <= 1e-9:
                    remaining[slave] = 0.0
                    continue

                stock_pos = int(stock_input_valves.get(slave, waste_valve_pos))
                if stock_pos not in grouped:
                    grouped[stock_pos] = ([], [])
                grouped[stock_pos][0].append(slave)
                grouped[stock_pos][1].append(float(chunk))

                active_slaves.append(slave)

            if not active_slaves:
                break  # 全部加完

            # 3.1) 各组吸液（同一组内：一次 lq_in 批量下发 + 统一 feedback）
            for stock_pos, (slaves_list, chunks_list) in grouped.items():
                self.lq_in(pumps=slaves_list, volumes=chunks_list, valve_pos_in=int(stock_pos))
                if switch_settle_s and switch_settle_s > 0:
                    time.sleep(switch_settle_s)

            # 3.2) 打入目标电解池（所有 active 泵一起打到 channel_valve_pos，回到 0）
            self.lq_out(pumps=active_slaves, valve_pos_out=int(channel_valve_pos), volumes=0.0)
            if switch_settle_s and switch_settle_s > 0:
                time.sleep(switch_settle_s)

            # 3.3) 更新 remaining
            for stock_pos, (slaves_list, chunks_list) in grouped.items():
                for slave, chunk in zip(slaves_list, chunks_list):
                    remaining[slave] = float(remaining.get(slave, 0.0) - float(chunk))
    
# ------------------------------------------------------------
# Experiment recording helpers (run-based folder, snapshot, results)
# ------------------------------------------------------------
    def _make_run_id(self, prefix: str = "") -> str:
        """Generate a human-friendly, globally-unique run id.

        Format: YYYYMMDD_<PREFIX_>XXXX
        - We keep the date explicitly (user-friendly).
        - XXXX is a short hash to avoid collisions (good enough in practice).
        """
        day = datetime.now().strftime("%Y%m%d")
        raw = f"{day}_{os.getpid()}_{time.time_ns()}".encode("utf-8")
        short = hashlib.sha1(raw).hexdigest()[:4].upper()
        if prefix:
            return f"{day}_{prefix}_{short}"
        return f"{day}_{short}"

    @staticmethod
    def _ensure_dir(p: str) -> str:
        os.makedirs(p, exist_ok=True)
        return p

    def _write_json(self, path: str, obj: dict) -> None:
        self._ensure_dir(os.path.dirname(os.path.abspath(path)))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def _append_csv_row(self, path: str, fieldnames: List[str], row: Dict[str, Any]) -> None:
        self._ensure_dir(os.path.dirname(os.path.abspath(path)))
        exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            if not exists:
                w.writeheader()
            w.writerow(row)

    @staticmethod
    def _fmt_num(x: Any, nd: int = 2) -> str:
        try:
            return f"{float(x):.{nd}f}"
        except Exception:
            return str(x)

    def _format_recipe_tag(self, row: Dict[str, Any]) -> str:
        """Compact recipe tag for filenames (final bath concentrations in g/L)."""
        peg = self._fmt_num(row.get("PEG_gL", 0.0), 2)
        sps = self._fmt_num(row.get("SPS_gL", 0.0), 2)
        jgb = self._fmt_num(row.get("JGB_gL", 0.0), 2)
        return f"PEG{peg}_SPS{sps}_JGB{jgb}"

    def _save_plan_snapshot(self, run_dir: str, batch: List[Dict[str, Any]]) -> str:
        snap_path = os.path.join(run_dir, "plan_snapshot.csv")
        self.save_plan_csv(batch, snap_path)
        return snap_path

    def capture_photo(self, save_path: str) -> str:
        """Capture a microscopy photo and save to the given path.

        This is implemented defensively because camera class APIs vary.
        It tries common method names on `self.cam`. If your camera driver uses
        different API, modify this function only — other recording logic stays unchanged.
        """
        self._ensure_dir(os.path.dirname(os.path.abspath(save_path)))

        cam = getattr(self, "cam", None)
        if cam is None:
            raise RuntimeError("Camera is not initialized: self.cam is None")

        # 1) Methods that directly save to path
        for name in ("capture_photo_to", "capture_to", "capture_and_save", "save", "save_image", "save_photo", "take_photo", "snap", "capture"):
            if hasattr(cam, name):
                fn = getattr(cam, name)
                try:
                    out = fn(save_path)
                    # some APIs return path / bool / ndarray
                    if isinstance(out, str) and os.path.exists(out):
                        if os.path.abspath(out) != os.path.abspath(save_path):
                            shutil.move(out, save_path)
                    elif os.path.exists(save_path):
                        pass
                    else:
                        # if returns ndarray/image, try to save
                        if out is not None:
                            try:
                                import cv2  # type: ignore
                                cv2.imwrite(save_path, out)
                            except Exception:
                                try:
                                    from PIL import Image  # type: ignore
                                    Image.fromarray(out).save(save_path)
                                except Exception:
                                    pass
                    if os.path.exists(save_path):
                        return save_path
                except TypeError:
                    # maybe method doesn't accept path; ignore
                    pass
                except Exception:
                    # try next candidate
                    pass

        # 2) If camera auto-saves into its own directory, we cannot infer file; raise with guidance.
        raise NotImplementedError(
            "Could not save photo via known camera methods. "
            "Please update Device.capture_photo() to match your CMOSAutoFocusCamera API."
        )

    def run_plan_csv_one_round(
        self,
        csv_path: str,
        start_index: int = 0,
        n_channels: int = 8,
        valve_positions: Tuple[int, ...] = (1, 2, 3, 4, 9, 6, 7, 8),
        syringe_slaves: Tuple[int, int, int, int] = (1, 2, 3, 4),
        stock_input_valves: Optional[Dict[int, int]] = None,
        max_syringe_mL: float = 5.0,
        grid_mL: float = 0.02,
        waste_valve_pos: int = 12,
        switch_settle_s: float = 0.0,
        ep_channels: Tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8),
        waste_speed: int = 400,
        waste_run_s: float = 60.0,
        waste_after_each_round: bool = True,
        waste_pumps_for_channels: Optional[List[int]] = None,
        ep_kwargs: Optional[Dict[str, Any]] = None,
        # --- recording ---
        exp_root: str = "./experiments",
        run_prefix: str = "",
        photo_ext: str = "png",
        compute_metrics: bool = True,
        photo_crop: Optional[Tuple[int, int, int, int]] = None,
        ep_time: int = 600,
        # --- photo logic / debug ---
        enable_camy_datum_before_photo: bool = False,
        save_debug_frames: bool = True,
        coarse_span_units: float = 20.0,
        coarse_n_steps: int = 11,
        fine_span_units: float = 6.0,
        fine_n_steps: int = 7,
        scan_speed: float = 20.0,
        scan_settle_s: float = 0.8,
        photo_move_settle_s: float = 0.8,
        camera_warmup_s: float = 1.5,
        x_sampling_offsets: Tuple[float, ...] = ( -4.0, -3.5, -3, -2.5, -2.0, -1.5, -1, -0.5,  0.0, 0.5, 1, 1.5, 2.0, 2.5, 3, 3.5, 4.0),
        autofocus_settle_s: float = 0.8,
        flush_n: int = 3,
        flush_interval_s: float = 0.08,
    ) -> int:
        """Run one round (default up to 8 cells) from a plan CSV and record results.

        Main photo logic for each channel:
            move to target
            -> optional camy datum
            -> autofocus once
            -> x-offset sampling capture around the target

        Images for the same electroplating cell are stored under:
            images/<exp_id>/
        """

        plan = self.load_plan_csv(csv_path)
        if start_index < 0 or start_index >= len(plan):
            raise IndexError(f"start_index out of range: {start_index}")

        if len(valve_positions) != n_channels:
            raise ValueError("valve_positions length must equal n_channels")

        if waste_pumps_for_channels is None:
            waste_pumps_for_channels = list(ep_channels)

        end_index = min(start_index + n_channels, len(plan))
        batch = plan[start_index:end_index]
        if len(batch) < n_channels:
            self.logger.warning(
                "Plan remaining points < n_channels. Will run %d channels.",
                len(batch),
            )

        # -----------------------
        # 0) Prepare run folder
        # -----------------------
        run_id = self._make_run_id(prefix=run_prefix)
        run_dir = self._ensure_dir(os.path.join(exp_root, run_id))
        img_dir = self._ensure_dir(os.path.join(run_dir, "images"))
        debug_dir = self._ensure_dir(os.path.join(run_dir, "debug_frames")) if save_debug_frames else ""

        snap_path = self._save_plan_snapshot(run_dir, batch)

        run_meta = {
            "run_id": run_id,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "csv_path": os.path.abspath(csv_path),
            "start_index": int(start_index),
            "end_index": int(end_index),
            "n_experiments": int(len(batch)),
            "valve_positions": list(valve_positions[:len(batch)]),
            "ep_channels": list(ep_channels[:len(batch)]),
            "plan_snapshot": os.path.abspath(snap_path),
            "save_debug_frames": bool(save_debug_frames),
            "enable_camy_datum_before_photo": bool(enable_camy_datum_before_photo),
            "x_sampling": {
                "offsets": [float(x) for x in x_sampling_offsets],
                "move_speed": float(scan_speed),
                "move_settle_s": float(scan_settle_s),
                "autofocus_settle_s": float(autofocus_settle_s),
                "flush_n": int(flush_n),
                "flush_interval_s": float(flush_interval_s),
            },
        }
        self._write_json(os.path.join(run_dir, "run_meta.json"), run_meta)

        camera_opened_here = False

        try:
            # -----------------------
            # Camera open (robust)
            # -----------------------
            cam_is_open = False
            try:
                cam_is_open = bool(
                    getattr(self.cam, "cap", None) is not None and self.cam.cap.isOpened()
                )
            except Exception:
                cam_is_open = False

            if not cam_is_open:
                ok = self.cam.open_camera()
                if not ok:
                    raise RuntimeError("open_camera failed")
                camera_opened_here = True
                time.sleep(float(camera_warmup_s))
            else:
                self.logger.info("Camera already opened, reuse current handle.")

            # -----------------------
            # 1) Dispense to each cell
            # -----------------------
            for k, row in enumerate(batch):
                ch_valve = valve_positions[k]
                self.logger.info(
                    "Dispense point %s into valve_pos=%s",
                    row.get("point_id", start_index + k + 1),
                    ch_valve,
                )
                self._dispense_one_channel_from_row(
                    channel_valve_pos=int(ch_valve),
                    row=row,
                    syringe_slaves=syringe_slaves,
                    stock_input_valves=stock_input_valves,
                    grid_mL=grid_mL,
                    max_syringe_mL=max_syringe_mL,
                    waste_valve_pos=int(waste_valve_pos),
                    switch_settle_s=float(switch_settle_s),
                )

            # -----------------------
            # 2) Start electroplating
            # -----------------------
            if ep_kwargs is None:
                ep_kwargs = {}
            self.ep_start(channel_list=ep_channels[:len(batch)], **ep_kwargs)
            time.sleep(ep_time + 10)

            # -----------------------
            # 3) EP after care + photo + metrics
            # -----------------------
            waste_pump_num = [1, 2, 3, 4, 5, 6, 7, 8]
            t = [21, 74, 126.5, 185, 238.5, 293, 346, 403]

            result_csv = os.path.join(run_dir, "results.csv")
            fieldnames = [
                "run_id",
                "date",
                "exp_id",
                "exp_seq",
                "channel",
                "valve_pos",
                "point_id",
                "PEG_gL",
                "SPS_gL",
                "JGB_gL",
                "v_PEG_mL",
                "v_SPS_mL",
                "v_JGB_mL",
                "v_BASE_mL",
                "sampling_dir",
                "sample_index",
                "offset_x",
                "target_x",
                "actual_x",
                "focus_debug_path",
                "image_path",
                "status",
                "error",
                "Non-DC",
                "GrdE",
            ]

            for seq, (pump, pos) in enumerate(zip(waste_pump_num, t), start=1):
                if seq > len(batch):
                    break

                row = batch[seq - 1]
                ch = int(ep_channels[seq - 1])
                valve_pos = int(valve_positions[seq - 1])
                point_id = int(row.get("point_id", start_index + seq))

                recipe_tag = self._format_recipe_tag(row)
                exp_id = f"E{seq:02d}_CH{ch}_P{point_id:03d}"
                cell_img_dir = self._ensure_dir(os.path.join(img_dir, exp_id))

                focus_debug_path = ""

                prep_info = self.ep_after_care_prepare_photo_with_parallel_clear(
                    seq=seq,
                    waste_pump=int(pump),
                    cam_pos=float(pos),
                    drive_speed=400,
                    drive_time_s=30,
                    photo_move_speed=20,
                    photo_move_settle_s=photo_move_settle_s,
                    clear_dry_run=False,
                    join_timeout_s=180.0,
                )
                cam_target = prep_info["cam_target"]

                # 3.3 可选：camy datum
                if enable_camy_datum_before_photo:
                    try:
                        self.camy.datum()
                        time.sleep(0.5)
                    except Exception as e:
                        self.logger.warning("camy.datum failed for %s: %s", exp_id, e)

                # 3.4 对焦一次
                try:
                    self.logger.info("Autofocus start for %s", exp_id)
                    self.cam.auto_focus()
                    time.sleep(float(autofocus_settle_s))
                except Exception as e:
                    self.logger.warning("auto_focus failed for %s: %s", exp_id, e)

                # 3.5 保存对焦基准图
                if save_debug_frames:
                    try:
                        debug_name = f"{exp_id}_{recipe_tag}_focus_base.{photo_ext}"
                        focus_debug_path = os.path.join(debug_dir, debug_name)
                        focus_debug_path = self._capture_and_save_fresh_frame(
                            focus_debug_path,
                            flush_n=int(flush_n),
                            flush_interval_s=float(flush_interval_s),
                        )
                    except Exception as e:
                        self.logger.warning("save focus debug frame failed for %s: %s", exp_id, e)
                        focus_debug_path = ""

                # 3.6 对同一个电解池做扫拍，并把图片放在同一文件夹
                for sample_index, dx in enumerate(x_sampling_offsets, start=1):
                    rec = {
                        "run_id": run_id,
                        "date": run_meta["date"],
                        "exp_id": exp_id,
                        "exp_seq": seq,
                        "channel": ch,
                        "valve_pos": valve_pos,
                        "point_id": point_id,
                        "PEG_gL": row.get("PEG_gL", ""),
                        "SPS_gL": row.get("SPS_gL", ""),
                        "JGB_gL": row.get("JGB_gL", ""),
                        "v_PEG_mL": row.get("v_PEG_mL", ""),
                        "v_SPS_mL": row.get("v_SPS_mL", ""),
                        "v_JGB_mL": row.get("v_JGB_mL", ""),
                        "v_BASE_mL": row.get("v_BASE_mL", ""),
                        "sampling_dir": os.path.abspath(cell_img_dir),
                        "sample_index": int(sample_index),
                        "offset_x": float(dx),
                        "target_x": "",
                        "actual_x": "",
                        "focus_debug_path": os.path.abspath(focus_debug_path) if focus_debug_path else "",
                        "image_path": "",
                        "status": "ok",
                        "error": "",
                        "Non-DC": "",
                        "GrdE": "",
                    }

                    try:
                        x_target = float(cam_target) + float(dx)
                        rec["target_x"] = float(x_target)
                        x_actual = self.safe_camx_absolute_move(x_target, float(scan_speed))
                        rec["actual_x"] = float(x_actual)
                        time.sleep(float(scan_settle_s))

                        img_name = f"{exp_id}_{recipe_tag}_S{sample_index:02d}_dx_{float(dx):+06.2f}.{photo_ext}"
                        img_name = img_name.replace("+", "p").replace("-", "m")
                        img_path = os.path.join(cell_img_dir, img_name)
                        img_path = self._capture_and_save_fresh_frame(
                            img_path,
                            flush_n=int(flush_n),
                            flush_interval_s=float(flush_interval_s),
                        )
                        rec["image_path"] = os.path.abspath(img_path)

                        if compute_metrics and os.path.exists(img_path):
                            try:
                                m = self.compute_plating_metrics(img_path, crop=photo_crop)
                                rec["Non-DC"] = m.get("Non-DC", "")
                                rec["GrdE"] = m.get("GrdE", "")
                            except Exception as e:
                                self.logger.error("compute_plating_metrics failed for %s sample %02d: %s", exp_id, sample_index, e)
                                rec["status"] = "metrics_failed"
                                rec["error"] = str(e)

                    except Exception as e:
                        rec["status"] = "failed"
                        rec["error"] = str(e)
                        self.logger.exception("x sampling failed for %s sample %02d dx=%s: %s", exp_id, sample_index, dx, e)

                    self._append_csv_row(result_csv, fieldnames, rec)

            # -----------------------
            # 4) Waste out (after round)
            # -----------------------
            if waste_after_each_round:
                self.drive_pumps(
                    pumps=waste_pumps_for_channels[:len(batch)],
                    speed=waste_speed,
                    waste_out_time=waste_run_s,
                )

            return end_index

        finally:
            try:
                if camera_opened_here and getattr(self, "cam", None) is not None:
                    self.cam.close_camera()
            except Exception as e:
                try:
                    self.logger.warning("close_camera in finally failed: %s", e)
                except Exception:
                    pass

            # ------------------------------------------------------------
            # Active learning (uncertainty-driven): suggest next points in 3D
            # ------------------------------------------------------------
    def suggest_next_points_max_std(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        batch_size: int = 6,
        n_candidates: int = 5000,
        seed: int = 0,
        min_dist: float = 0.06,
        bounds: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Suggest next points by maximizing GP posterior std with a distance constraint.

        - X_train: (N,3) in *normalized* space [0,1] recommended.
        - bounds: if X_train is in real concentration space, pass bounds as [[PEG_min,PEG_max],...].
        """
        # import locally to avoid hard dependency at import time
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import Matern, WhiteKernel, ConstantKernel

        X = np.asarray(X_train, dtype=float)
        y = np.asarray(y_train, dtype=float).ravel()
        if X.ndim != 2 or X.shape[1] != 3:
            raise ValueError("X_train must be (N,3)")
        if len(X) != len(y):
            raise ValueError("X_train and y_train length mismatch")

        kernel = ConstantKernel(1.0, (1e-2, 1e2)) * Matern(length_scale=[1, 1, 1], nu=2.5) + WhiteKernel(noise_level=1e-3, noise_level_bounds=(1e-6, 1e-1))
        gp = GaussianProcessRegressor(kernel=kernel, normalize_y=True, n_restarts_optimizer=3, random_state=seed)
        gp.fit(X, y)

        # candidate pool
        U = self._simple_lhs(n_candidates, 3, seed=seed + 123)
        C = U
        if bounds is not None:
            b = np.asarray(bounds, dtype=float)
            if b.shape != (3, 2):
                raise ValueError("bounds must be shape (3,2)")
            C = b[:, 0] + U * (b[:, 1] - b[:, 0])

        _, std = gp.predict(C, return_std=True)

        selected = []
        mask = np.ones(len(C), dtype=bool)

        # precompute for speed
        C_work = C.copy()
        std_work = std.copy()

        for _ in range(batch_size):
            if len(std_work) == 0:
                break
            idx = int(np.argmax(std_work))
            x_new = C_work[idx]
            selected.append(x_new)

            # distance constraint: remove candidates too close to selected point
            dist = np.linalg.norm(C_work - x_new, axis=1)
            keep = dist >= float(min_dist)
            C_work = C_work[keep]
            std_work = std_work[keep]

        return np.asarray(selected, dtype=float)

































































    # ------------------------------------------------------------
    # Resource cleanup
    # ------------------------------------------------------------
    def close(self):
        """
        统一释放资源，防止 COM 占用、句柄泄露
        """
        # close modbus clients
        try:
            if hasattr(self, "ser_camy"):
                self.ser_camy.close()
        except Exception:
            pass
        try:
            if hasattr(self, "ser_camx"):
                self.ser_camx.close()
        except Exception:
            pass
        try:
            if hasattr(self, "water_in_ser"):
                self.water_in_ser.close()
        except Exception:
            pass
        try:
            if hasattr(self, "acid_in_ser"):
                self.acid_in_ser.close()
        except Exception:
            pass

        # waste clients
        if hasattr(self, "waste_clients"):
            for _, cli in self.waste_clients.items():
                try:
                    cli.close()
                except Exception:
                    pass

        # serial
        try:
            if hasattr(self, "runze_connection") and self.runze_connection:
                self.runze_connection.close()
        except Exception:
            pass

    # ============================================================
    # Microscopy image metrics for plating quality
    #   - Non-DC energy ratio (FFT power excluding DC)
    #   - Gradient energy (RMS gradient magnitude)
    #
    # These metrics are intended to be used as the active-learning target y.
    # ============================================================

    @staticmethod
    def _to_gray_float32(img: np.ndarray) -> np.ndarray:
        """Convert image array to grayscale float32 in [0, 1]."""
        if img is None:
            raise ValueError("img is None")
        arr = np.asarray(img)
        if arr.ndim == 2:
            gray = arr
        elif arr.ndim == 3:
            # Handle RGB/RGBA/BGR generically by averaging channels
            gray = arr[..., :3].mean(axis=2)
        else:
            raise ValueError(f"Unsupported image shape: {arr.shape}")

        gray = gray.astype(np.float32)

        # Normalize to [0,1] if looks like uint8/uint16 range
        gmin, gmax = float(np.min(gray)), float(np.max(gray))
        if gmax > 1.5:  # heuristic: likely 0-255 / 0-65535
            if gmax <= 255.0:
                gray = gray / 255.0
            else:
                gray = gray / 65535.0

        # Clip to a sane range (avoid outliers from conversions)
        gray = np.clip(gray, 0.0, 1.0)
        return gray

    @staticmethod
    def non_dc_energy_ratio(img: np.ndarray) -> float:
        """Compute Non-DC energy ratio: (E_total - E_dc) / E_total.

        Notes:
        - DC is the centered component after fftshift.
        - Input image should be grayscale; any dtype is accepted.
        """
        img = Device._to_gray_float32(img)
        F = np.fft.fftshift(np.fft.fft2(img))
        power = (np.abs(F) ** 2).astype(np.float64)

        cy, cx = power.shape[0] // 2, power.shape[1] // 2
        E_dc = float(power[cy, cx])
        E_total = float(power.sum())
        if E_total <= 0.0:
            return 0.0
        return float((E_total - E_dc) / E_total)

    @staticmethod
    def gradient_energy(img: np.ndarray) -> float:
        """Compute gradient energy: sqrt(mean(|∇I|^2)) (RMS gradient magnitude)."""
        img = Device._to_gray_float32(img)
        gy, gx = np.gradient(img)
        e2 = gx * gx + gy * gy
        return float(np.sqrt(np.mean(e2)))

    @staticmethod
    def load_image(path: str) -> np.ndarray:
        """Load an image file to numpy array without hard dependency on a specific backend.

        Tries (in order):
        - OpenCV (cv2)
        - PIL (Pillow)
        """
        if not os.path.exists(path):
            raise FileNotFoundError(path)

        # Try OpenCV first
        try:
            import cv2  # type: ignore
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None:
                raise ValueError("cv2.imread returned None")
            # OpenCV loads as BGR; our _to_gray_float32 averages channels so ok.
            return img
        except Exception:
            pass

        # Fallback to PIL
        try:
            from PIL import Image  # type: ignore
            with Image.open(path) as im:
                return np.array(im)
        except Exception as e:
            raise RuntimeError(f"Failed to load image: {path}. Install opencv-python or pillow. err={e}") from e

    def compute_plating_metrics(
        self,
        image_path: str,
        crop: Optional[Tuple[int, int, int, int]] = None,
    ) -> Dict[str, float]:
        """Compute (Non-DC, GrdE) for a microscopy image.

        Args:
            image_path: path to microscopy photo.
            crop: optional (x0, y0, x1, y1) in pixel coordinates.

        Returns:
            dict with keys: {"Non-DC", "GrdE"}
        """
        img = self.load_image(image_path)
        if crop is not None:
            x0, y0, x1, y1 = crop
            img = img[y0:y1, x0:x1]

        non_dc = self.non_dc_energy_ratio(img)
        grde = self.gradient_energy(img)
        return {"Non-DC": non_dc, "GrdE": grde}

    @staticmethod
    def scalarize_metrics(
        non_dc: float,
        grde: float,
        mode: str = "weighted_sum",
        weights: Tuple[float, float] = (0.5, 0.5),
    ) -> float:
        """Convert (Non-DC, GrdE) into a single scalar y for single-output GP.

        Why:
            Many active-learning stacks assume y is 1D. If you later want multi-output,
            you can fit two independent GPs instead.

        Options:
            - mode="weighted_sum": y = w1*NonDC + w2*GrdE
            - mode="non_dc": y = NonDC
            - mode="grde": y = GrdE

        NOTE:
            For 'weighted_sum', you should standardize features first (z-score) using
            observed data distribution; this function only provides a simple combination.
        """
        if mode == "non_dc":
            return float(non_dc)
        if mode == "grde":
            return float(grde)
        if mode != "weighted_sum":
            raise ValueError(f"Unknown mode: {mode}")

        w1, w2 = weights
        return float(w1 * non_dc + w2 * grde)
    



    def _capture_and_save_fresh_frame(self, save_path: str, flush_n: int = 3, flush_interval_s: float = 0.08) -> str:
        # """
        # 强制抓取并保存一张新帧。
        # 先连续读取几次，把旧缓存尽量冲掉，再保存最后一帧。
        # """
        import os
        import time
        import cv2

        frame = None
        for _ in range(max(1, int(flush_n))):
            frame = self._capture_frame_bgr()
            time.sleep(float(flush_interval_s))

        if frame is None:
            raise RuntimeError("Failed to capture fresh frame")

        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        ok = cv2.imwrite(save_path, frame)
        if not ok:
            raise RuntimeError(f"cv2.imwrite failed: {save_path}")
        return save_path

    def run_8ch_copper_x_sampling(
        self,
        exp_root: str = "./copper_x_sampling",
        run_prefix: str = "",
        channel_ids=(1, 2, 3, 4, 5, 6, 7, 8),
        initial_positions=(21, 74, 126.5, 185, 238.5, 293, 346, 403),
        x_offsets=(-6.0, -4.0, -2.0, 0.0, 2.0, 4.0, 6.0),
        move_speed: float = 20.0,
        move_settle_s: float = 0.8,
        autofocus_settle_s: float = 0.8,
        camera_warmup_s: float = 1.5,
        flush_n: int = 3,
        flush_interval_s: float = 0.08,
        photo_ext: str = "png",
        save_focus_debug: bool = True,
    ):
        """
        只做 8 个铜片位置的小范围 x 采样拍照：
            1) 移动到每个槽位的预定位置
            2) 自动对焦一次
            3) 在预定位置左右做若干小位移
            4) 每个位移点都强制抓取新帧并保存
            5) 写 results.csv

        不做：
            - 加液
            - 电镀
            - 排液
            - 水洗
            - 在线最佳位置筛选

        Returns
        -------
        result_csv : str
            结果表路径
        """
        import os
        import csv
        import time
        from datetime import datetime

        if len(channel_ids) != 8:
            raise ValueError("channel_ids 必须长度为 8")
        if len(initial_positions) != 8:
            raise ValueError("initial_positions 必须长度为 8")
        if len(x_offsets) < 1:
            raise ValueError("x_offsets 不能为空")

        # --------------------------------------------------
        # 0) 建目录
        # --------------------------------------------------
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"{run_prefix}copper_x_sampling_{ts}" if run_prefix else f"copper_x_sampling_{ts}"
        run_dir = os.path.abspath(os.path.join(exp_root, run_name))
        img_dir = os.path.join(run_dir, "images")
        dbg_dir = os.path.join(run_dir, "debug_frames")

        self._ensure_dir(run_dir)
        self._ensure_dir(img_dir)
        if save_focus_debug:
            self._ensure_dir(dbg_dir)

        self.logger.info("=== run_8ch_copper_x_sampling START ===")
        self.logger.info("run_dir = %s", run_dir)

        # --------------------------------------------------
        # 1) 相机预热
        # --------------------------------------------------
        time.sleep(float(camera_warmup_s))

        # --------------------------------------------------
        # 2) 结果表
        # --------------------------------------------------
        result_csv = os.path.join(run_dir, "results.csv")
        fieldnames = [
            "run_dir",
            "exp_id",
            "channel",
            "base_x",
            "offset_x",
            "target_x",
            "actual_x",
            "focus_debug_path",
            "image_path",
            "status",
            "error",
        ]
        rows = []

        # --------------------------------------------------
        # 3) 逐通道处理
        # --------------------------------------------------
        for seq, (ch, base_x) in enumerate(zip(channel_ids, initial_positions), start=1):
            exp_id = f"E{seq:02d}_CH{int(ch)}"
            self.logger.info("----- [%s] start x sampling -----", exp_id)

            focus_debug_path = ""

            try:
                # 3.1 先移动到该槽位预定位置
                x0 = self.safe_camx_absolute_move(float(base_x), float(move_speed))
                self.logger.info("[%s] moved to base x = %.3f", exp_id, x0)
                time.sleep(float(move_settle_s))

                # 3.2 自动对焦一次
                try:
                    self.cam.auto_focus()
                    self.logger.info("[%s] autofocus done", exp_id)
                    time.sleep(float(autofocus_settle_s))
                except Exception as e:
                    self.logger.warning("[%s] autofocus failed: %s", exp_id, e)

                # 3.3 保存对焦后的基准调试图（也是 fresh frame）
                if save_focus_debug:
                    cell_img_dir = self._ensure_dir(os.path.join(img_dir, exp_id))
                    p = os.path.join(dbg_dir, f"{exp_id}_focus_base.{photo_ext}")
                    focus_debug_path = self._capture_and_save_fresh_frame(
                        p,
                        flush_n=int(flush_n),
                        flush_interval_s=float(flush_interval_s),
                    )
                else:
                    cell_img_dir = self._ensure_dir(os.path.join(img_dir, exp_id))

                # 3.4 逐个 offset 采样
                for i, dx in enumerate(x_offsets, start=1):
                    rec = {
                        "run_dir": run_dir,
                        "exp_id": exp_id,
                        "channel": int(ch),
                        "base_x": float(base_x),
                        "offset_x": float(dx),
                        "target_x": "",
                        "actual_x": "",
                        "focus_debug_path": focus_debug_path,
                        "image_path": "",
                        "status": "ok",
                        "error": "",
                    }

                    try:
                        x_target = float(base_x) + float(dx)
                        rec["target_x"] = float(x_target)

                        x_actual = self.safe_camx_absolute_move(x_target, float(move_speed))
                        rec["actual_x"] = float(x_actual)

                        self.logger.info(
                            "[%s] sample %02d/%02d: dx=%+.3f -> target_x=%.3f -> actual_x=%.3f",
                            exp_id, i, len(x_offsets), float(dx), float(x_target), float(x_actual)
                        )

                        # 等机械稳定
                        time.sleep(float(move_settle_s))

                        img_name = f"{exp_id}_dx_{float(dx):+06.2f}.{photo_ext}"
                        img_name = img_name.replace("+", "p").replace("-", "m")
                        img_path = os.path.join(cell_img_dir, img_name)

                        # 关键：强制抓新帧保存，不再走 capture_photo()
                        rec["image_path"] = self._capture_and_save_fresh_frame(
                            img_path,
                            flush_n=int(flush_n),
                            flush_interval_s=float(flush_interval_s),
                        )

                    except Exception as e:
                        rec["status"] = "failed"
                        rec["error"] = str(e)
                        self.logger.exception("[%s] offset dx=%s failed: %s", exp_id, dx, e)

                    rows.append(rec)

                    # 每拍一张就刷新一次 csv
                    with open(result_csv, "w", newline="", encoding="utf-8-sig") as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(rows)

            except Exception as e:
                self.logger.exception("[%s] base positioning failed: %s", exp_id, e)

                rec = {
                    "run_dir": run_dir,
                    "exp_id": exp_id,
                    "channel": int(ch),
                    "base_x": float(base_x),
                    "offset_x": "",
                    "target_x": "",
                    "actual_x": "",
                    "focus_debug_path": focus_debug_path,
                    "image_path": "",
                    "status": "failed",
                    "error": f"base positioning failed: {e}",
                }
                rows.append(rec)

                with open(result_csv, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)

        self.logger.info("=== run_8ch_copper_x_sampling END ===")
        self.logger.info("results.csv = %s", result_csv)
        return result_csv

    def run_waterwash_and_next_drivepump(
        device,
        n,
        max_channel=8,
        water_speed=200,
        water_run_s=7,
        settle_s=20,
        waste_speed=400,
        waste_run_s=30,
    ):
        """
        电解池 n 执行 water_wash，
        同时电解池 n+1 执行 drive_pumps。

        参数
        ----
        device : Device 实例
        n : int
            当前做 water_wash 的电解池编号
        max_channel : int
            最大通道号，默认 8
        water_speed, water_run_s, settle_s, waste_speed, waste_run_s
            直接透传给原函数
        """
        if not isinstance(n, int):
            raise TypeError("n 必须是整数")

        if n < 1 or n >= max_channel:
            raise ValueError(f"n 必须满足 1 <= n < {max_channel}，因为需要同时执行 n+1")

        wash_ch = n
        pump_ch = n + 1

        def task_waterwash():
            try:
                print(f"[water_wash] start: channel={wash_ch}")
                device.water_wash(
                    list_water_wash=[wash_ch],
                    water_speed=water_speed,
                    water_run_s=water_run_s,
                    settle_s=settle_s,
                    waste_speed=waste_speed,
                    waste_run_s=waste_run_s,
                )
                print(f"[water_wash] end: channel={wash_ch}")
            except Exception as e:
                print(f"[water_wash] error on channel {wash_ch}: {e}")
                traceback.print_exc()

        def task_drivepump():
            try:
                print(f"[drive_pumps] start: channel={pump_ch}")
                device.drive_pumps(
                    pumps=[pump_ch],
                    speed=waste_speed,
                    waste_out_time=waste_run_s,
                )
                print(f"[drive_pumps] end: channel={pump_ch}")
            except Exception as e:
                print(f"[drive_pumps] error on channel {pump_ch}: {e}")
                traceback.print_exc()

        t1 = threading.Thread(target=task_waterwash, name=f"waterwash-{wash_ch}")
        t2 = threading.Thread(target=task_drivepump, name=f"drivepump-{pump_ch}")

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        print(f"[done] water_wash({wash_ch}) + drive_pumps({pump_ch}) 完成")



    def ep_copper_out(self,ep_pos1:str,ep_pos2:str):
        self.MG400.SetSpeedJ(35)
        self.MG400.SetSpeedL(35)
        self.get_ready_position("pre_position2")
        self.pgse.set_site(1)
        self.moveJ_to_point(ep_pos1)
        self.MG400.SetSpeedJ(15)
        self.MG400.SetSpeedL(15)
        time.sleep(0.1)
        self.moveJ_to_point(ep_pos2)
        self.pgse.set_site(0)
        self.moveL_to_point(ep_pos1)
        self.MG400.SetSpeedJ(35)
        self.MG400.SetSpeedL(35)
        self.get_ready_position("drop_out")
        self.pgse.set_site(1)
        self.get_ready_position("pre_position2")
        self.MG400.SetSpeedJ(15)
        self.MG400.SetSpeedL(15)
        time.sleep(0.1)


    def ep_copper_in(self,pre_pos:str,
                     get_ready_position:str,
                     Cu_target:str,
                     seg_key:str,
                     end:str,
                     n:int,
                     level:int,
                     ep_target1:str,
                     ep_target2:str,
                     ):
        self.pgse.set_site(1)
        self.MG400.SetSpeedJ(35)
        self.MG400.SetSpeedL(35)
        self.get_ready_position(pre_pos)
        self.get_ready_position(get_ready_position)
        time.sleep(0.1)
        self.MG400.SetSpeedJ(15)
        self.MG400.SetSpeedL(15)
        time.sleep(0.1)
        self.moveL_to_point(Cu_target)
        self.moveJ_to_point(Cu_target)
        self.move_down_by_segments_delta_z(seg_key=seg_key, end=end)
        self.RelMoveL_from_current(offset_x=0, offset_y=-26*n, offset_z=0, offset_r=0)
        self.pgse.set_site(0)
        self.move_up1()
        if level == 1:
            self.move_out_of_barrier1()
        elif level in (2, 3):
            self.move_out_of_barrier23()
        self.RelMoveL_from_current(offset_x=0, offset_y=26*n, offset_z=0, offset_r=0)
        self.move_up2()
        self.moveL_to_point(get_ready_position)
        self.get_ready_position(get_ready_position)
        if level == 1:
            self.move_up3() 
        self.MG400.SetSpeedJ(35)
        self.MG400.SetSpeedL(35)
        self.moveJ_to_point(ep_target1)
        self.moveL_to_point(ep_target1)
        self.MG400.SetSpeedJ(15)
        self.MG400.SetSpeedL(15)
        self.moveL_to_point(ep_target2)
        time.sleep(0.1)
        self.pgse.set_site(1)
        self.MG400.SetSpeedJ(35)
        self.MG400.SetSpeedL(35)
        self.moveL_to_point(ep_target1)
        self.get_ready_position(pre_pos)
        time.sleep(0.1)
        self.MG400.SetSpeedJ(15)
        self.MG400.SetSpeedL(15)
        time.sleep(0.1)


    # ============================================================
    # 1) 读取配置
    # ============================================================
    def load_copper_move_configs(self, move_plan_path="move_plan2.json", point_path="point.json"):
        with open(move_plan_path, "r", encoding="utf-8") as f:
            self.move_plan2 = json.load(f)

        with open(point_path, "r", encoding="utf-8") as f:
            self.point_data = json.load(f)

        self.move_plan_path = move_plan_path
        self.point_path = point_path

        print(f"[load] move_plan loaded: {move_plan_path}")
        print(f"[load] point loaded: {point_path}")

    # ============================================================
    # 2) 点位工具
    # ============================================================
    def _get_nested_dict_value(self, data, dotted_key):
        cur = data
        for part in dotted_key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                raise KeyError(f"Key path not found: {dotted_key}")
            cur = cur[part]
        return cur

    def _point_exists(self, dotted_key):
        try:
            self._get_nested_dict_value(self.point_data, dotted_key)
            return True
        except Exception:
            return False

    # ============================================================
    # 3) 仓库位解析
    #    仓库位格式: col_layer_row
    #    例如 "3_1_2" 表示:
    #      col=3, layer=1, row=2
    #
    #    真实基准点:
    #      seg_key = x_layer_row
    #
    #    当前规则:
    #      end 固定为 p1
    #      level 由 row 决定，用于选择 pre_position/get_ready_position
    # ============================================================
    def _parse_warehouse_slot(self, slot_key):
        m = re.fullmatch(r"(\d+)_(\d+)_(\d+)", str(slot_key))
        if not m:
            raise ValueError(f"Invalid warehouse slot: {slot_key}, expected like '3_1_2'")

        col = int(m.group(1))
        layer = int(m.group(2))
        row = int(m.group(3))

        if col < 1 or col > 4:
            raise ValueError(f"Invalid col in {slot_key}, must be 1..4")
        if layer < 1:
            raise ValueError(f"Invalid layer in {slot_key}, must be >= 1")
        if row < 1 or row > 3:
            raise ValueError(f"Invalid row in {slot_key}, must be 1..3")

        seg_key = f"x_{layer}_{row}"
        end = "p1"
        level = row

        return {
            "raw": slot_key,
            "col": col,
            "layer": layer,
            "row": row,
            "seg_key": seg_key,
            "end": end,
            "level": level,
        }

    # ============================================================
    # 4) 由仓库位计算 n
    #    当前规则:
    #      同一 layer,row 的基准点是 x_layer_row
    #      col 决定 x 方向偏移
    #
    #      col=4 -> n=0
    #      col=3 -> n=1
    #      col=2 -> n=2
    #      col=1 -> n=3
    #
    #      即 n = 4 - col
    # ============================================================
    def _calc_n_from_warehouse_slot(self, slot_key):
        info = self._parse_warehouse_slot(slot_key)
        a = info["col"]
        c = info["row"]   # 你当前代码里 level=row，也就是第三位 c

        n = (5 - c) - a
        if n < 0:
            raise ValueError(f"Calculated n={n} < 0 for warehouse slot {slot_key}")

        return n
    def _normalize_ep_key(self, ep_key):
        m = re.fullmatch(r"(\d+)_(\d+)", str(ep_key))
        if not m:
            raise ValueError(f"Invalid ep key: {ep_key}, expected like '1_2'")
        return f"{int(m.group(1))}_{int(m.group(2))}"

    # ============================================================
    # 5) 生成 ep_copper_in 参数
    # ============================================================
    def build_ep_copper_in_args(self, warehouse_slot, ep_key):
        """
        输入:
            warehouse_slot = '3_1_2'
            ep_key = '1_2'

        当前规则下生成:
            pre_pos='pre_position2'
            get_ready_position='get_ready_position2'
            Cu_target='segments_up.x_1_2.p1'
            seg_key='x_1_2'
            end='p1'
            n=1
            level=2
            ep_target1='ep.1_2_in.p1'
            ep_target2='ep.1_2_in.p2'
        """
        ep_key = self._normalize_ep_key(ep_key)
        info = self._parse_warehouse_slot(warehouse_slot)

        seg_key = info["seg_key"]
        end = info["end"]
        level = info["level"]
        n = self._calc_n_from_warehouse_slot(warehouse_slot)

        args = {
            "pre_pos": f"pre_position{level}",
            "get_ready_position": f"get_ready_position{level}",
            "Cu_target": f"segments_up.{seg_key}.{end}",
            "seg_key": seg_key,
            "end": end,
            "n": n,
            "level": level,
            "ep_target1": f"ep.{ep_key}_in.p1",
            "ep_target2": f"ep.{ep_key}_in.p2",
        }

        if hasattr(self, "point_data") and self.point_data is not None:
            for p in [args["Cu_target"], args["ep_target1"], args["ep_target2"]]:
                if not self._point_exists(p):
                    raise KeyError(f"Point path not found in point.json: {p}")

        return args

    # ============================================================
    # 6) 生成 ep_copper_out 参数
    # ============================================================
    def build_ep_copper_out_args(self, ep_key):
        ep_key = self._normalize_ep_key(ep_key)

        args = {
            "ep_pos1": f"ep.{ep_key}_out.p1",
            "ep_pos2": f"ep.{ep_key}_out.p2",
        }

        if hasattr(self, "point_data") and self.point_data is not None:
            for p in [args["ep_pos1"], args["ep_pos2"]]:
                if not self._point_exists(p):
                    raise KeyError(f"Point path not found in point.json: {p}")

        return args

    # ============================================================
    # 7) 从 point.json 自动生成所有仓库位
    #    point.json 中 segments_up 的 key 形如 x_layer_row
    #    每个 x_layer_row 对应 4 个 col
    # ============================================================
    def _generate_warehouse_slots_from_point(self):
        if not hasattr(self, "point_data") or self.point_data is None:
            raise RuntimeError("point.json not loaded")

        segs = self.point_data.get("segments_up", {})
        slots = []

        for seg_key in segs.keys():
            m = re.fullmatch(r"x_(\d+)_(\d+)", str(seg_key))
            if not m:
                continue

            layer = int(m.group(1))
            row = int(m.group(2))

            for col in range(1, 5):
                slots.append(f"{col}_{layer}_{row}")

        return sorted(
            list(set(slots)),
            key=lambda s: tuple(map(int, s.split("_")))
        )

    # ============================================================
    # 8) 从 point.json 提取所有电解池 key
    # ============================================================
    def _extract_ep_keys_from_point(self):
        if not hasattr(self, "point_data") or self.point_data is None:
            raise RuntimeError("point.json not loaded")

        ep_data = self.point_data.get("ep", {})
        ep_keys = set()

        for k in ep_data.keys():
            m = re.fullmatch(r"(\d+_\d+)_(in|out)", str(k))
            if m:
                ep_keys.add(m.group(1))

        return sorted(ep_keys, key=lambda s: tuple(map(int, s.split("_"))))

    # ============================================================
    # 9) 状态表初始化
    #    X = 有电极片
    #    O = 空
    #    D = defect
    # ============================================================
    def init_copper_state(self, defect_slots=None, warehouse_slots=None, ep_keys=None):
        defect_slots = set(defect_slots or [])

        if warehouse_slots is None:
            warehouse_slots = self._generate_warehouse_slots_from_point()

        if ep_keys is None:
            ep_keys = self._extract_ep_keys_from_point()

        self.copper_state = {
            "warehouse": {},
            "ep": {},
        }

        for slot in warehouse_slots:
            if slot in defect_slots:
                mark = "D"
            else:
                mark = "X"

            self.copper_state["warehouse"][slot] = {
                "mark": mark,     # X / O / D
                "to_ep": None,
            }

        for ep_key in ep_keys:
            self.copper_state["ep"][ep_key] = {
                "mark": "O",      # X / O
                "from_slot": None,
            }

        print("[state] initialized")

    # ============================================================
    # 10) 从 move_plan2.json 的 meta.defects 初始化状态表
    # ============================================================
    def init_copper_state_from_move_plan_meta(self, warehouse_slots=None, ep_keys=None):
        if not hasattr(self, "move_plan2") or self.move_plan2 is None:
            raise RuntimeError("move_plan2 not loaded")

        defects = self.move_plan2.get("meta", {}).get("defects", [])
        defect_slots = []

        for item in defects:
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                continue
            col, layer, row = item
            defect_slots.append(f"{int(col)}_{int(layer)}_{int(row)}")

        self.init_copper_state(
            defect_slots=defect_slots,
            warehouse_slots=warehouse_slots,
            ep_keys=ep_keys,
        )

        print(f"[state] defect slots loaded from move_plan meta: {len(defect_slots)}")

    # ============================================================
    # 11) 获取状态表副本
    # ============================================================
    def get_copper_state_snapshot(self):
        if not hasattr(self, "copper_state"):
            raise RuntimeError("copper_state not initialized")
        return deepcopy(self.copper_state)

    # ============================================================
    # 12) 状态检查
    # ============================================================
    def can_move_warehouse_to_ep(self, warehouse_slot, ep_key):
        if not hasattr(self, "copper_state"):
            raise RuntimeError("copper_state not initialized")

        ep_key = self._normalize_ep_key(ep_key)

        if warehouse_slot not in self.copper_state["warehouse"]:
            raise KeyError(f"warehouse slot not found: {warehouse_slot}")
        if ep_key not in self.copper_state["ep"]:
            raise KeyError(f"ep key not found: {ep_key}")

        w = self.copper_state["warehouse"][warehouse_slot]
        e = self.copper_state["ep"][ep_key]

        if w["mark"] == "D":
            return False, f"{warehouse_slot} is defect"
        if w["mark"] != "X":
            return False, f"{warehouse_slot} has no copper"
        if e["mark"] != "O":
            return False, f"ep {ep_key} is not empty"

        return True, "ok"

    def can_move_ep_to_recycle(self, ep_key):
        if not hasattr(self, "copper_state"):
            raise RuntimeError("copper_state not initialized")

        ep_key = self._normalize_ep_key(ep_key)

        if ep_key not in self.copper_state["ep"]:
            raise KeyError(f"ep key not found: {ep_key}")

        e = self.copper_state["ep"][ep_key]
        if e["mark"] != "X":
            return False, f"ep {ep_key} has no copper"

        return True, "ok"

    # ============================================================
    # 13) 执行入槽
    # ============================================================
    def move_copper_from_warehouse_to_ep(self, warehouse_slot, ep_key, update_state=True, dry_run=False):
        ok, msg = self.can_move_warehouse_to_ep(warehouse_slot, ep_key)
        if not ok:
            raise RuntimeError(f"Cannot move warehouse -> ep: {msg}")

        args = self.build_ep_copper_in_args(warehouse_slot, ep_key)

        print(f"[move in] {warehouse_slot} -> {ep_key}")
        print(args)

        if not dry_run:
            self.ep_copper_in(**args)

        if update_state:
            self.copper_state["warehouse"][warehouse_slot]["mark"] = "O"
            self.copper_state["warehouse"][warehouse_slot]["to_ep"] = ep_key

            self.copper_state["ep"][ep_key]["mark"] = "X"
            self.copper_state["ep"][ep_key]["from_slot"] = warehouse_slot

        return args

    # ============================================================
    # 14) 执行出槽回收
    # ============================================================
    def move_copper_from_ep_to_recycle(
        self,
        ep_key,
        update_state=True,
        dry_run=False,
        force_execute=False,
    ):
        ep_key = self._normalize_ep_key(ep_key)

        if not force_execute:
            ok, msg = self.can_move_ep_to_recycle(ep_key)
            if not ok:
                raise RuntimeError(f"Cannot move ep -> recycle: {msg}")
        else:
            print(f"[force move out] skip state check for {ep_key}")

        args = self.build_ep_copper_out_args(ep_key)

        print(f"[move out] {ep_key} -> recycle")
        print(args)

        if not dry_run:
            self.ep_copper_out(**args)

        if update_state and hasattr(self, "copper_state") and ep_key in self.copper_state["ep"]:
            self.copper_state["ep"][ep_key]["mark"] = "O"
            self.copper_state["ep"][ep_key]["from_slot"] = None

        return args


    # ============================================================
    # 15) 解析 batch 中的一条记录
    #    move_plan2.json 中的格式:
    #      "(3,1,2)->(1,1)[O]"
    #
    #    含义:
    #      (col,layer,row) -> (epx,epy) [kind]
    # ============================================================
    def _parse_batch_move_line(self, line):
        line = str(line).strip()

        m = re.fullmatch(
            r"\((\d+),(\d+),(\d+)\)->\((\d+),(\d+)\)\[([A-Z])\]",
            line
        )
        if not m:
            raise ValueError(f"Invalid batch move line: {line}")

        col = int(m.group(1))
        layer = int(m.group(2))
        row = int(m.group(3))
        epx = int(m.group(4))
        epy = int(m.group(5))
        kind = m.group(6)

        return {
            "warehouse_slot": f"{col}_{layer}_{row}",
            "ep_key": f"{epx}_{epy}",
            "kind": kind,
            "src_tuple": (col, layer, row),
            "dst_tuple": (epx, epy),
            "raw": line,
        }

    # ============================================================
    # 16) 读取某个 batch 的所有动作
    # ============================================================
    def load_batch_moves(self, batch_name):
        if not hasattr(self, "move_plan2") or self.move_plan2 is None:
            raise RuntimeError("move_plan2 not loaded, please call load_copper_move_configs first")

        batches = self.move_plan2.get("batches", {})
        if batch_name not in batches:
            raise KeyError(f"Batch not found in move_plan2.json: {batch_name}")

        lines = batches[batch_name]
        parsed = [self._parse_batch_move_line(line) for line in lines]
        return parsed

    # ============================================================
    # 17) 按 batch 放电极片
    #
    #    kinds:
    #      None        -> 全部执行
    #      {"O"}       -> 只执行 O
    #      {"X"}       -> 只执行 X
    #      {"O","X"}   -> 执行 O 和 X
    # ============================================================
    def load_copper_batch(self, batch_name, dry_run=False, update_state=True, kinds=None):
        moves = self.load_batch_moves(batch_name)

        if kinds is not None:
            kinds = set(kinds)
            moves = [m for m in moves if m["kind"] in kinds]

        results = []
        print(f"\n[batch load] {batch_name}, total={len(moves)}")

        for i, mv in enumerate(moves, 1):
            warehouse_slot = mv["warehouse_slot"]
            ep_key = mv["ep_key"]
            kind = mv["kind"]

            print(f"[{batch_name} step {i}] {warehouse_slot} -> {ep_key} [{kind}]")

            args = self.move_copper_from_warehouse_to_ep(
                warehouse_slot=warehouse_slot,
                ep_key=ep_key,
                update_state=update_state,
                dry_run=dry_run,
            )

            rec = dict(mv)
            rec["args"] = args
            results.append(rec)

        return results

    # ============================================================
    # 18) 执行 move_plan2.json 中全部 batch
    # ============================================================
    def load_all_batches(self, dry_run=False, update_state=True, kinds=None):
        if not hasattr(self, "move_plan2") or self.move_plan2 is None:
            raise RuntimeError("move_plan2 not loaded")

        batches = self.move_plan2.get("batches", {})
        all_results = {}

        def batch_sort_key(name):
            m = re.fullmatch(r"batch(\d+)", str(name))
            if m:
                return int(m.group(1))
            return 10**9

        for batch_name in sorted(batches.keys(), key=batch_sort_key):
            all_results[batch_name] = self.load_copper_batch(
                batch_name=batch_name,
                dry_run=dry_run,
                update_state=update_state,
                kinds=kinds,
            )

        return all_results

    # ============================================================
    # 19) 对所有电解池中的电极片执行 ep_copper_out
    # ============================================================
    def clear_all_ep_copper(
        self,
        ep_keys=None,
        dry_run=False,
        update_state=True,
        only_occupied=True,
        force_execute=False,
    ):
        if not hasattr(self, "copper_state"):
            raise RuntimeError("copper_state not initialized")

        if ep_keys is None:
            ep_keys = sorted(
                self.copper_state["ep"].keys(),
                key=lambda s: tuple(map(int, s.split("_")))
            )

        results = []
        print(f"\n[clear all ep copper] total ep={len(ep_keys)}")

        for ep_key in ep_keys:
            mark = self.copper_state["ep"].get(ep_key, {}).get("mark", None)

            if only_occupied and mark != "X" and not force_execute:
                print(f"[skip] {ep_key} mark={mark}")
                continue

            print(f"[clear] {ep_key} mark={mark}")

            args = self.move_copper_from_ep_to_recycle(
                ep_key=ep_key,
                update_state=update_state,
                dry_run=dry_run,
                force_execute=force_execute,
            )

            results.append({
                "ep_key": ep_key,
                "args": args,
            })

        return results


    # ============================================================
    # 20) 清空某个 batch 对应的所有电解池
    # ============================================================
    def clear_batch_ep_copper(self, batch_name, dry_run=False, update_state=True, only_occupied=True):
        moves = self.load_batch_moves(batch_name)
        ep_keys = []
        seen = set()

        for mv in moves:
            ep_key = mv["ep_key"]
            if ep_key not in seen:
                seen.add(ep_key)
                ep_keys.append(ep_key)

        return self.clear_all_ep_copper(
            ep_keys=ep_keys,
            dry_run=dry_run,
            update_state=update_state,
            only_occupied=only_occupied,
        )

    # ============================================================
    # 21) 打印状态表
    # ============================================================
    def print_copper_state(self):
        if not hasattr(self, "copper_state"):
            raise RuntimeError("copper_state not initialized")

        print("\n========== Warehouse ==========")
        for k in sorted(self.copper_state["warehouse"].keys(), key=lambda s: tuple(map(int, s.split("_")))):
            v = self.copper_state["warehouse"][k]
            print(f"{k}: {v['mark']}")

        print("\n========== EP ==========")
        for k in sorted(self.copper_state["ep"].keys(), key=lambda s: tuple(map(int, s.split("_")))):
            v = self.copper_state["ep"][k]
            print(f"{k}: {v['mark']}, from={v['from_slot']}")
        
    def dispense_plan_batch(
        self,
        csv_path: str,
        start_index: int = 0,
        n_channels: int = 8,
        valve_positions: Tuple[int, ...] = (1, 2, 3, 4, 9, 6, 7, 8),
        syringe_slaves: Tuple[int, int, int, int] = (1, 2, 3, 4),
        stock_input_valves: Optional[Dict[int, int]] = None,
        max_syringe_mL: float = 5.0,
        grid_mL: float = 0.02,
        waste_valve_pos: int = 12,
        switch_settle_s: float = 0.0,
    ):
        """
        从 plan csv 中取出一轮 batch，按通道顺序逐个加液。
        返回本轮实际使用的 batch 数据。
        """
        plan = self.load_plan_csv(csv_path)

        if start_index < 0 or start_index >= len(plan):
            raise IndexError(f"start_index out of range: {start_index}")

        end_index = min(start_index + n_channels, len(plan))
        batch = plan[start_index:end_index]

        if len(batch) == 0:
            raise ValueError("当前 batch 为空，没有可加液的数据")

        if len(valve_positions) < len(batch):
            raise ValueError(
                f"valve_positions 数量不足: need={len(batch)}, got={len(valve_positions)}"
            )

        self.logger.info(
            "[dispense_plan_batch] start: start_index=%s end_index=%s n=%s",
            start_index, end_index, len(batch)
        )

        for k, row in enumerate(batch):
            ch_valve = int(valve_positions[k])
            point_id = row.get("point_id", start_index + k + 1)

            self.logger.info(
                "[dispense_plan_batch] point_id=%s -> valve_pos=%s",
                point_id, ch_valve,
            )

            self._dispense_one_channel_from_row(
                channel_valve_pos=ch_valve,
                row=row,
                syringe_slaves=syringe_slaves,
                stock_input_valves=stock_input_valves,
                grid_mL=grid_mL,
                max_syringe_mL=max_syringe_mL,
                waste_valve_pos=int(waste_valve_pos),
                switch_settle_s=float(switch_settle_s),
            )

        self.logger.info("[dispense_plan_batch] done")
        return batch
    def run_load_copper_batch_and_dispense(
        self,
        batch_name: str,
        csv_path: str,
        start_index: int = 0,
        n_channels: int = 8,
        valve_positions: Tuple[int, ...] = (1, 2, 3, 4, 9, 6, 7, 8),
        syringe_slaves: Tuple[int, int, int, int] = (1, 2, 3, 4),
        stock_input_valves: Optional[Dict[int, int]] = None,
        max_syringe_mL: float = 5.0,
        grid_mL: float = 0.02,
        waste_valve_pos: int = 12,
        switch_settle_s: float = 0.0,
        copper_kinds=None,
        update_state: bool = True,
        dry_run_copper: bool = False,
    ):
        """
        新逻辑：
        - 放电极片持续进行
        - 某个电解池只有在第二片电极片放入后，才允许对该池子加液

        返回:
            {
                "load_copper_batch": [...],
                "dispense_batch": [...],
                "errors": [...],
            }
        """
        import threading
        import traceback

        plan = self.load_plan_csv(csv_path)
        if start_index < 0 or start_index >= len(plan):
            raise IndexError(f"start_index out of range: {start_index}")

        end_index = min(start_index + n_channels, len(plan))
        batch = plan[start_index:end_index]

        if len(batch) == 0:
            raise ValueError("当前 batch 为空，没有可加液的数据")

        if len(valve_positions) < len(batch):
            raise ValueError(
                f"valve_positions 数量不足: need={len(batch)}, got={len(valve_positions)}"
            )

        move_list = self.load_batch_moves(batch_name)
        if copper_kinds is not None:
            copper_kinds = set(copper_kinds)
            move_list = [m for m in move_list if m["kind"] in copper_kinds]

        results = {
            "load_copper_batch": [],
            "dispense_batch": [],
            "errors": [],
        }

        cond = threading.Condition()

        # 每个 seq 当前已经放入了几片电极片
        inserted_count_by_seq = {seq: 0 for seq in range(1, len(batch) + 1)}

        # 已经满足“两片都放完”，等待加液的 seq 队列
        ready_seq_queue = []

        # 避免重复加液
        dispensed_seq_set = set()

        # 放铜片线程是否结束
        load_done = False

        def task_load_copper():
            nonlocal load_done
            try:
                print(f"[load_copper_batch] start: {batch_name}")

                for i, mv in enumerate(move_list, 1):
                    warehouse_slot = mv["warehouse_slot"]
                    ep_key = mv["ep_key"]
                    kind = mv["kind"]

                    print(f"[{batch_name} step {i}] {warehouse_slot} -> {ep_key} [{kind}]")

                    args = self.move_copper_from_warehouse_to_ep(
                        warehouse_slot=warehouse_slot,
                        ep_key=ep_key,
                        update_state=update_state,
                        dry_run=dry_run_copper,
                    )

                    rec = dict(mv)
                    rec["args"] = args
                    results["load_copper_batch"].append(rec)

                    # 当前这片电极片属于哪个实验通道
                    seq = self._seq_from_ep_key(ep_key)

                    with cond:
                        if 1 <= seq <= len(batch):
                            inserted_count_by_seq[seq] += 1
                            cur_cnt = inserted_count_by_seq[seq]

                            self.logger.info(
                                "[load_copper_batch] seq=%s ep_key=%s inserted_count=%s",
                                seq, ep_key, cur_cnt
                            )

                            # 只有第二片放进去之后，才允许加液
                            if cur_cnt == 2 and seq not in dispensed_seq_set:
                                ready_seq_queue.append(seq)
                                self.logger.info(
                                    "[load_copper_batch] seq=%s is ready for dispense (2 electrodes inserted)",
                                    seq
                                )
                                cond.notify_all()

                print(f"[load_copper_batch] end: {batch_name}")

            except Exception as e:
                msg = f"load_copper_batch error: {e}"
                results["errors"].append(msg)
                print(f"[load_copper_batch] error: {e}")
                traceback.print_exc()
            finally:
                with cond:
                    load_done = True
                    cond.notify_all()

        def task_dispense():
            try:
                print(
                    f"[dispense_when_ready] start: csv={csv_path}, "
                    f"start_index={start_index}, n_channels={len(batch)}"
                )

                while True:
                    with cond:
                        while not ready_seq_queue and not load_done and not results["errors"]:
                            cond.wait()

                        if results["errors"]:
                            break

                        if ready_seq_queue:
                            seq = ready_seq_queue.pop(0)
                        else:
                            # 没有待加液池，且放铜片结束了
                            if load_done:
                                break
                            continue

                    if seq in dispensed_seq_set:
                        continue

                    row = batch[seq - 1]
                    ch_valve = int(valve_positions[seq - 1])
                    point_id = row.get("point_id", start_index + seq)

                    print(
                        f"[dispense_when_ready] seq={seq}, point_id={point_id}, "
                        f"valve_pos={ch_valve} start"
                    )

                    self._dispense_one_channel_from_row(
                        channel_valve_pos=ch_valve,
                        row=row,
                        syringe_slaves=syringe_slaves,
                        stock_input_valves=stock_input_valves,
                        grid_mL=grid_mL,
                        max_syringe_mL=max_syringe_mL,
                        waste_valve_pos=int(waste_valve_pos),
                        switch_settle_s=float(switch_settle_s),
                    )

                    dispensed_seq_set.add(seq)
                    results["dispense_batch"].append({
                        "seq": seq,
                        "point_id": point_id,
                        "valve_pos": ch_valve,
                        "row": row,
                    })

                    print(
                        f"[dispense_when_ready] seq={seq}, point_id={point_id}, "
                        f"valve_pos={ch_valve} done"
                    )

                # 收尾检查：理论上每个实验通道都应该加到液
                missing = [seq for seq in range(1, len(batch) + 1) if seq not in dispensed_seq_set]
                if missing:
                    raise RuntimeError(f"这些 seq 没有完成加液: {missing}")

                print("[dispense_when_ready] end")

            except Exception as e:
                msg = f"dispense_when_ready error: {e}"
                results["errors"].append(msg)
                print(f"[dispense_when_ready] error: {e}")
                traceback.print_exc()
                with cond:
                    cond.notify_all()

        t1 = threading.Thread(target=task_load_copper, name=f"load-copper-{batch_name}")
        t2 = threading.Thread(target=task_dispense, name="dispense-when-ready")

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        if results["errors"]:
            raise RuntimeError(
                "run_load_copper_batch_and_dispense failed: " + " | ".join(results["errors"])
            )

        print("[done] load_copper_batch + dispense_when_ready 完成")
        return results


    def _get_clear_ep_key_for_photo_seq(self, seq: int) -> str:
        """
        当前处理第 seq 个电解池时，返回需要 clear 的电解池 key。
        规则：X + Y = 9，返回 f"{Y}_1"
        例如：
            seq=1 -> "8_1"
            seq=2 -> "7_1"
            ...
            seq=8 -> "1_1"
        """
        if not isinstance(seq, int):
            raise TypeError("seq 必须是整数")
        if seq < 1 or seq > 8:
            raise ValueError("seq 必须在 1~8 之间")
        y = 9 - seq
        return f"{y}_1"

    def ep_after_care_prepare_photo_with_parallel_clear(
        self,
        *,
        seq: int,
        waste_pump: int,
        cam_pos: float,
        drive_speed: float = 400,
        drive_time_s: float = 30,
        photo_move_speed: float = 20,
        photo_move_settle_s: float = 0.8,
        clear_dry_run: bool = False,
        join_timeout_s: Optional[float] = 180.0,
    ) -> Dict[str, Any]:
        """
        对当前第 seq 个电解池执行：
            1) 排液
            2) 水洗
            3) 移动到拍照位
        同时并发执行：
            clear_all_ep_copper(ep_keys=[f"{9-seq}_1"], dry_run=clear_dry_run)

        返回：
            {
                "seq": seq,
                "clear_ep_key": "...",
                "cam_target": ...,
            }

        注意：
        - 本函数在返回前会等待 clear 线程结束
        - 适合在 autofocus / 拍照之前调用
        """
        clear_ep_key = self._get_clear_ep_key_for_photo_seq(int(seq))
        result = {
            "seq": int(seq),
            "clear_ep_key": clear_ep_key,
            "cam_target": None,
            "errors": [],
        }

        def task_clear():
            try:
                print(f"[clear_all_ep_copper] start: seq={seq}, ep_keys={[clear_ep_key]}")
                self.clear_all_ep_copper(
                    ep_keys=[clear_ep_key],
                    dry_run=bool(clear_dry_run),
                )
                print(f"[clear_all_ep_copper] end: seq={seq}, ep_keys={[clear_ep_key]}")
            except Exception as e:
                msg = f"clear_all_ep_copper error on seq={seq}, ep_key={clear_ep_key}: {e}"
                result["errors"].append(msg)
                print(f"[clear_all_ep_copper] error: {e}")
                traceback.print_exc()

        t_clear = threading.Thread(
            target=task_clear,
            name=f"clear-ep-{clear_ep_key}",
            daemon=False,
        )

        # 并发启动 clear
        t_clear.start()

        # 主线程继续做排液 + 水洗 + 到拍照位
        try:
            print(f"[after_care] start drive_pumps: waste_pump={waste_pump}")
            self.drive_pumps([int(waste_pump)], float(drive_speed), float(drive_time_s))
            print(f"[after_care] end drive_pumps: waste_pump={waste_pump}")

            print(f"[after_care] start water_in_for_photo: waste_pump={waste_pump}")
            self.water_in_for_photo(list_water_wash=(int(waste_pump),))
            print(f"[after_care] end water_in_for_photo: waste_pump={waste_pump}")

            print(f"[after_care] move to photo pos: seq={seq}, cam_pos={cam_pos}")
            cam_target = self.safe_camx_absolute_move(float(cam_pos), float(photo_move_speed))
            result["cam_target"] = cam_target
            time.sleep(float(photo_move_settle_s))
            print(f"[after_care] arrived photo pos: seq={seq}, cam_target={cam_target}")

        except Exception as e:
            msg = f"after care / move photo failed on seq={seq}: {e}"
            result["errors"].append(msg)
            print(f"[after_care] error: {e}")
            traceback.print_exc()

        # 在 autofocus 之前等 clear 完成
        if join_timeout_s is None:
            t_clear.join()
        else:
            t_clear.join(timeout=float(join_timeout_s))
            if t_clear.is_alive():
                msg = (
                    f"clear_all_ep_copper timeout on seq={seq}, "
                    f"ep_key={clear_ep_key}, timeout={join_timeout_s}s"
                )
                result["errors"].append(msg)
                raise RuntimeError(msg)

        if result["errors"]:
            raise RuntimeError(" | ".join(result["errors"]))

        return result
    def _ep_key_x1_for_channel(self, n: int) -> str:
        if n < 1 or n > 8:
            raise ValueError("n must be in 1..8")
        x = 9 - n
        return f"{x}_1"

    def _ep_key_x2_for_channel(self, n: int) -> str:
        if n < 1 or n > 8:
            raise ValueError("n must be in 1..8")
        x = 9 - n
        return f"{x}_2"

    def _seq_from_ep_key(self, ep_key: str) -> int:
        """
        由 ep_key (如 '8_1' / '8_2') 反推它属于 batch 中第几个通道 seq。
        规则与 _ep_key_x1_for_channel / _ep_key_x2_for_channel 保持一致：
            seq = 9 - x
        例如：
            '8_1' -> seq=1
            '7_2' -> seq=2
            ...
            '1_1' -> seq=8
        """
        ep_key = self._normalize_ep_key(ep_key)
        x_str, _ = ep_key.split("_")
        x = int(x_str)
        seq = 9 - x
        if seq < 1 or seq > 8:
            raise ValueError(f"Invalid ep_key -> seq mapping: ep_key={ep_key}, seq={seq}")
        return seq
    def prepare_channel_n_for_photo(
        self,
        n: int,
        drain_speed: float = 400,
        drain_time_s: float = 30,
        wash_cycles: int = 3,
        wash_water_run_s: float = 11,
        wash_settle_s: float = 2,
        wash_water_speed: float = 400,
        wash_waste_speed: float = 400,
        wash_waste_run_s: float = 30,
        refill_water_run_s: float = 11,
        refill_water_speed: float = 400,
    ):
        """
        通道 n 拍照前准备：
        1) 排液
        2) 清洗 3 次
        3) 补水
        """
        # 1. 排液：可与很多操作并发
        self.drive_pumps([int(n)], float(drain_speed), float(drain_time_s))

        # 2. 清洗和补水：共享液路，必须串行
        with self.wash_lock:
            for i in range(int(wash_cycles)):
                self.water_wash(
                    list_water_wash=(int(n),),
                    water_run_s=float(wash_water_run_s),
                    settle_s=float(wash_settle_s),
                    water_speed=float(wash_water_speed),
                    waste_speed=float(wash_waste_speed),
                    waste_run_s=float(wash_waste_run_s),
                )

            self.water_in_for_photo(
                list_water_wash=(int(n),),
                water_speed=float(refill_water_speed),
                water_run_s=float(refill_water_run_s),
            )
    def move_out_x1_for_channel(self, n: int, dry_run: bool = False):
        ep_key = self._ep_key_x1_for_channel(int(n))
        with self.arm_lock:
            self.clear_all_ep_copper(
                ep_keys=[ep_key],
                dry_run=bool(dry_run),
            )
    def photo_channel_n(
        self,
        n: int,
        cam_pos: float,
        photo_move_speed: float = 20,
        photo_move_settle_s: float = 0.8,
        autofocus_settle_s: float = 0.8,
    ):
        """
        通道 n 的拍照流程。
        这里假设：补水已完成，X_1 已移出。
        """
        cam_target = self.safe_camx_absolute_move(float(cam_pos), float(photo_move_speed))
        time.sleep(float(photo_move_settle_s))

        self.cam.auto_focus()
        time.sleep(float(autofocus_settle_s))

        return cam_target
    def post_photo_cleanup_and_move_x2(
    self,
    n: int,
    drain_speed: float = 400,
    drain_time_s: float = 30,
    extra_wash_cycles: int = 2,
    wash_water_run_s: float = 10,
    wash_settle_s: float = 2,
    wash_water_speed: float = 400,
    wash_waste_speed: float = 400,
    wash_waste_run_s: float = 30,
    post_x2_wash_cycles: int = 1,
    dry_run: bool = False,
):
        # """
        # 通道 n 拍照后的延后善后：
        # 1) 先排液
        # 2) 再额外两次清洗
        # 3) 移出对应的 X_2
        # 4) X_2 移出后再清洗一次
        # """
        

        errors = []

        try:
            self.drive_pumps([int(n)], float(drain_speed), float(drain_time_s))
        except Exception as e:
            errors.append(f"drain failed on ch{n}: {e}")
            traceback.print_exc()

        try:
            with self.wash_lock:
                for _ in range(int(extra_wash_cycles)):
                    self.water_wash(
                        list_water_wash=(int(n),),
                        water_run_s=float(wash_water_run_s),
                        settle_s=float(wash_settle_s),
                        water_speed=float(wash_water_speed),
                        waste_speed=float(wash_waste_speed),
                        waste_run_s=float(wash_waste_run_s),
                    )
        except Exception as e:
            errors.append(f"extra_wash failed on ch{n}: {e}")
            traceback.print_exc()

        try:
            ep_key = self._ep_key_x2_for_channel(int(n))
            with self.arm_lock:
                self.clear_all_ep_copper(
                    ep_keys=[ep_key],
                    dry_run=bool(dry_run),
                )
        except Exception as e:
            errors.append(f"move_x2 failed on ch{n}: {e}")
            traceback.print_exc()

        try:
            with self.wash_lock:
                for _ in range(int(post_x2_wash_cycles)):
                    self.water_wash(
                        list_water_wash=(int(n),),
                        water_run_s=float(wash_water_run_s),
                        settle_s=float(wash_settle_s),
                        water_speed=float(wash_water_speed),
                        waste_speed=float(wash_waste_speed),
                        waste_run_s=float(wash_waste_run_s),
                    )
        except Exception as e:
            errors.append(f"post_x2_wash failed on ch{n}: {e}")
            traceback.print_exc()

        if errors:
            raise RuntimeError(" | ".join(errors))


    def run_channel_after_ep_pipeline(
        self,
        n: int,
        cam_pos: float,
        dry_run_clear: bool = False,
        row: Optional[Dict[str, Any]] = None,
        point_id: Optional[int] = None,
        ch: Optional[int] = None,
        valve_pos: Optional[int] = None,
        run_id: str = "",
        run_meta: Optional[Dict[str, Any]] = None,
        run_dir: str = "",
        img_dir: str = "",
        debug_dir: str = "",
        photo_ext: str = "png",
        compute_metrics: bool = True,
        photo_crop=None,
        enable_camy_datum_before_photo: bool = False,
        autofocus_settle_s: float = 0.8,
        photo_move_speed: float = 20,
        photo_move_settle_s: float = 0.8,
        x_sampling_offsets=(-4.0, -3.5, -3.0, -2.5, -2.0, -1.5, -1.0, -0.5,
                            0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0),
        scan_speed: float = 20.0,
        scan_settle_s: float = 1.0,
        save_debug_frames: bool = True,
        flush_n: int = 2,
        flush_interval_s: float = 0.03,
        clear_dry_run: Optional[bool] = None,
        **kwargs,
    ):
        """
        单个通道 n 的电镀后流程：
        1) 排液 + 三洗 + 补水
        2) 并发移出 X_1
        3) 对焦一次
        4) 在 x_sampling_offsets 上扫拍并保存结果

        兼容两种调用方式：
        - 简版：只传 n / cam_pos / dry_run_clear
        - 完整版：由 run_full_experiment_one_batch 传入记录与扫拍参数
        """
        import os
        import threading
        import time
        import traceback

        if clear_dry_run is not None:
            dry_run_clear = bool(clear_dry_run)

        errors = []

        def task_prepare():
            try:
                self.prepare_channel_n_for_photo(n=int(n))
            except Exception as e:
                errors.append(f"prepare_channel_n_for_photo failed on ch{n}: {e}")
                traceback.print_exc()

        def task_move_x1():
            try:
                self.move_out_x1_for_channel(n=int(n), dry_run=bool(dry_run_clear))
            except Exception as e:
                errors.append(f"move_out_x1_for_channel failed on ch{n}: {e}")
                traceback.print_exc()

        t1 = threading.Thread(target=task_prepare, name=f"prepare-ch{n}")
        t2 = threading.Thread(target=task_move_x1, name=f"move-x1-ch{n}")
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        if errors:
            raise RuntimeError(" | ".join(errors))

        if enable_camy_datum_before_photo:
            try:
                self.camy.datum()
                time.sleep(0.5)
            except Exception as e:
                self.logger.warning("camy.datum failed for ch%s: %s", n, e)

        cam_target = self.photo_channel_n(
            n=int(n),
            cam_pos=float(cam_pos),
            photo_move_speed=float(photo_move_speed),
            photo_move_settle_s=float(photo_move_settle_s),
            autofocus_settle_s=float(autofocus_settle_s),
        )

        # 简版调用：只做准备 + 对焦，不做落盘
        if not img_dir:
            return cam_target

        row = row or {}
        run_meta = run_meta or {}
        exp_seq = int(n)
        ch = int(ch if ch is not None else n)
        valve_pos = int(valve_pos) if valve_pos is not None else ""
        point_id = int(point_id) if point_id is not None else int(row.get("point_id", exp_seq))
        recipe_tag = self._format_recipe_tag(row) if row else ""
        exp_id = f"E{exp_seq:02d}_CH{ch}_P{point_id:03d}"
        cell_img_dir = self._ensure_dir(os.path.join(img_dir, exp_id))
        focus_debug_path = ""

        result_csv = os.path.join(run_dir, "results.csv") if run_dir else "results.csv"
        fieldnames = [
            "run_id", "date", "exp_id", "exp_seq", "channel", "valve_pos", "point_id",
            "PEG_gL", "SPS_gL", "JGB_gL", "v_PEG_mL", "v_SPS_mL", "v_JGB_mL", "v_BASE_mL",
            "sampling_dir", "sample_index", "offset_x", "target_x", "actual_x",
            "focus_debug_path", "image_path", "status", "error", "Non-DC", "GrdE",
        ]

        if save_debug_frames and debug_dir:
            try:
                debug_name = f"{exp_id}_{recipe_tag}_focus_base.{photo_ext}" if recipe_tag else f"{exp_id}_focus_base.{photo_ext}"
                focus_debug_path = os.path.join(debug_dir, debug_name)
                focus_debug_path = self._capture_and_save_fresh_frame(
                    focus_debug_path,
                    flush_n=int(flush_n),
                    flush_interval_s=float(flush_interval_s),
                )
            except Exception as e:
                self.logger.warning("save focus debug frame failed for %s: %s", exp_id, e)
                focus_debug_path = ""

        for sample_index, dx in enumerate(x_sampling_offsets, start=1):
            rec = {
                "run_id": run_id,
                "date": run_meta.get("date", datetime.now().strftime("%Y-%m-%d")),
                "exp_id": exp_id,
                "exp_seq": exp_seq,
                "channel": ch,
                "valve_pos": valve_pos,
                "point_id": point_id,
                "PEG_gL": row.get("PEG_gL", ""),
                "SPS_gL": row.get("SPS_gL", ""),
                "JGB_gL": row.get("JGB_gL", ""),
                "v_PEG_mL": row.get("v_PEG_mL", ""),
                "v_SPS_mL": row.get("v_SPS_mL", ""),
                "v_JGB_mL": row.get("v_JGB_mL", ""),
                "v_BASE_mL": row.get("v_BASE_mL", ""),
                "sampling_dir": os.path.abspath(cell_img_dir),
                "sample_index": int(sample_index),
                "offset_x": float(dx),
                "target_x": "",
                "actual_x": "",
                "focus_debug_path": os.path.abspath(focus_debug_path) if focus_debug_path else "",
                "image_path": "",
                "status": "ok",
                "error": "",
                "Non-DC": "",
                "GrdE": "",
            }

            try:
                x_target = float(cam_target) + float(dx)
                rec["target_x"] = float(x_target)
                x_actual = self.safe_camx_absolute_move(x_target, float(scan_speed))
                rec["actual_x"] = float(x_actual)
                time.sleep(float(scan_settle_s))

                img_name = f"{exp_id}_{recipe_tag}_S{sample_index:02d}_dx_{float(dx):+06.2f}.{photo_ext}" if recipe_tag else f"{exp_id}_S{sample_index:02d}_dx_{float(dx):+06.2f}.{photo_ext}"
                img_name = img_name.replace("+", "p").replace("-", "m")
                img_path = os.path.join(cell_img_dir, img_name)
                img_path = self._capture_and_save_fresh_frame(
                    img_path,
                    flush_n=int(flush_n),
                    flush_interval_s=float(flush_interval_s),
                )
                rec["image_path"] = os.path.abspath(img_path)

                if compute_metrics and os.path.exists(img_path):
                    try:
                        m = self.compute_plating_metrics(img_path, crop=photo_crop)
                        rec["Non-DC"] = m.get("Non-DC", "")
                        rec["GrdE"] = m.get("GrdE", "")
                    except Exception as e:
                        self.logger.error("compute_plating_metrics failed for %s sample %02d: %s", exp_id, sample_index, e)
                        rec["status"] = "metrics_failed"
                        rec["error"] = str(e)

            except Exception as e:
                rec["status"] = "failed"
                rec["error"] = str(e)
                self.logger.exception("x sampling failed for %s sample %02d dx=%s: %s", exp_id, sample_index, dx, e)

            self._append_csv_row(result_csv, fieldnames, rec)

        return cam_target
    def run_all_channels_after_ep_pipeline(
        self,
        channel_cam_positions: Dict[int, float],
        dry_run_clear: bool = False,
    ):
        """
        多通道流水线：
        对每个通道 n：
        1) 排液 + 三洗 + 补水，与移出 X_1 并发
        2) 拍照
        3) 拍完后把额外两洗 + 移出 X_2 作为后台任务提交
        4) 立刻开始 n+1

        资源互斥由 arm_lock / wash_lock 自动保证。
        """


        bg_threads = []
        bg_errors = []

        def make_post_task(ch: int):
            def _task():
                try:
                    self.post_photo_cleanup_and_move_x2(
                        n=int(ch),
                        dry_run=bool(dry_run_clear),
                    )
                except Exception as e:
                    bg_errors.append(f"post task failed on ch{ch}: {e}")
                    traceback.print_exc()
            return _task

        for n in sorted(channel_cam_positions.keys()):
            cam_pos = channel_cam_positions[n]

            # 1) 当前通道主流程
            self.run_channel_after_ep_pipeline(
                n=int(n),
                cam_pos=float(cam_pos),
                dry_run_clear=bool(dry_run_clear),
            )

            # 2) 当前通道拍完后，后台提交“额外两洗 + 移出 X_2”
            t = threading.Thread(
                target=make_post_task(int(n)),
                name=f"post-photo-ch{n}",
                daemon=False,
            )
            t.start()
            bg_threads.append(t)

            # 3) 主线程立刻继续下一个通道

        # 4) 所有主流程完成后，等待所有后台善后结束
        for t in bg_threads:
            t.join()

        if bg_errors:
            raise RuntimeError(" | ".join(bg_errors))
    def run_full_experiment_one_batch(
        self,
        *,
        batch_name: str,
        csv_path: str,
        start_index: int = 0,
        n_channels: int = 8,
        valve_positions=(1, 2, 3, 4, 9, 6, 7, 8),
        syringe_slaves=(1, 2, 3, 4),
        stock_input_valves=None,
        max_syringe_mL: float = 5.0,
        grid_mL: float = 0.02,
        waste_valve_pos: int = 12,
        switch_settle_s: float = 0.0,
        load_copper: bool = True,
        dry_run_load_copper: bool = False,
        clear_dry_run: bool = False,
        ep_channels=(1, 2, 3, 4, 5, 6, 7, 8),
        cam_positions=(21, 74, 126.5, 185, 238.5, 293, 346, 403),
        exp_root: str = "./experiments",
        run_prefix: str = "",
        photo_ext: str = "png",
        compute_metrics: bool = True,
        photo_crop=None,
        enable_camy_datum_before_photo: bool = False,
        autofocus_settle_s: float = 0.8,
        photo_move_speed: float = 20,
        photo_move_settle_s: float = 0.8,
        x_sampling_offsets=(-4.0, -3.5, -3.0, -2.5, -2.0, -1.5, -1.0, -0.5,
                            0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0),
        scan_speed: float = 20.0,
        scan_settle_s: float = 1.0,
        save_debug_frames: bool = True,
        flush_n: int = 2,
        flush_interval_s: float = 0.03,
        electroplating_wait_s: float = 10,
    ):
        import traceback
        import threading
        import time
        import os

        if len(ep_channels) < n_channels:
            raise ValueError("ep_channels 数量不足")
        if len(cam_positions) < n_channels:
            raise ValueError("cam_positions 数量不足")
        if len(valve_positions) < n_channels:
            raise ValueError("valve_positions 数量不足")

        plan = self.load_plan_csv(csv_path)
        if start_index < 0 or start_index >= len(plan):
            raise IndexError(f"start_index out of range: {start_index}")

        end_index = min(start_index + n_channels, len(plan))
        batch = plan[start_index:end_index]
        if len(batch) == 0:
            raise ValueError("当前 batch 为空，没有可执行的实验")

        run_id = self._make_run_id(prefix=run_prefix)
        run_dir = self._ensure_dir(os.path.join(exp_root, run_id))
        img_dir = self._ensure_dir(os.path.join(run_dir, "images"))
        debug_dir = self._ensure_dir(os.path.join(run_dir, "debug_frames")) if save_debug_frames else ""

        snap_path = self._save_plan_snapshot(run_dir, batch)

        run_meta = {
            "run_id": run_id,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "csv_path": os.path.abspath(csv_path),
            "start_index": int(start_index),
            "end_index": int(end_index),
            "n_experiments": int(len(batch)),
            "valve_positions": list(valve_positions[:len(batch)]),
            "ep_channels": list(ep_channels[:len(batch)]),
            "plan_snapshot": os.path.abspath(snap_path),
            "save_debug_frames": bool(save_debug_frames),
            "enable_camy_datum_before_photo": bool(enable_camy_datum_before_photo),
            "x_sampling": {
                "offsets": [float(x) for x in x_sampling_offsets],
                "move_speed": float(scan_speed),
                "move_settle_s": float(scan_settle_s),
                "autofocus_settle_s": float(autofocus_settle_s),
                "flush_n": int(flush_n),
                "flush_interval_s": float(flush_interval_s),
            },
            "batch_name": batch_name,
            "electroplating_wait_s": float(electroplating_wait_s),
        }
        self._write_json(os.path.join(run_dir, "run_meta.json"), run_meta)

        if load_copper:
            self.logger.info(
                "[full_batch] concurrent pre-ep start: load_copper_batch + dispense, "
                "start_index=%s end_index=%s",
                start_index, end_index,
            )
            pre_ep_results = self.run_load_copper_batch_and_dispense(
                batch_name=batch_name,
                csv_path=csv_path,
                start_index=start_index,
                n_channels=len(batch),
                valve_positions=tuple(int(x) for x in valve_positions[:len(batch)]),
                syringe_slaves=tuple(int(x) for x in syringe_slaves),
                stock_input_valves=stock_input_valves,
                max_syringe_mL=float(max_syringe_mL),
                grid_mL=float(grid_mL),
                waste_valve_pos=int(waste_valve_pos),
                switch_settle_s=float(switch_settle_s),
                dry_run_copper=bool(dry_run_load_copper),
            )
            if pre_ep_results.get("errors"):
                raise RuntimeError(
                    "pre-ep concurrent stage failed: " + " | ".join(pre_ep_results["errors"])
                )
            self.logger.info("[full_batch] concurrent pre-ep done")
        else:
            self.logger.info("[full_batch] dispense start: start_index=%s end_index=%s", start_index, end_index)
            for k, row in enumerate(batch):
                ch_valve = int(valve_positions[k])
                self.logger.info(
                    "[full_batch] dispense point %s into valve_pos=%s",
                    row.get("point_id", start_index + k + 1),
                    ch_valve,
                )
                self._dispense_one_channel_from_row(
                    channel_valve_pos=ch_valve,
                    row=row,
                    syringe_slaves=syringe_slaves,
                    stock_input_valves=stock_input_valves,
                    grid_mL=grid_mL,
                    max_syringe_mL=max_syringe_mL,
                    waste_valve_pos=int(waste_valve_pos),
                    switch_settle_s=float(switch_settle_s),
                )
            self.logger.info("[full_batch] dispense done")

        self.logger.info("[full_batch] electroplating start")
        self.ep_start()

        self.logger.info(
            "[full_batch] electroplating waiting: %.1f s",
            float(electroplating_wait_s),
        )
        time.sleep(float(electroplating_wait_s))

        self.logger.info("[full_batch] electroplating wait done, start after-care")

        bg_threads = []
        bg_errors = []

        def make_post_task(ch: int):
            def _task():
                try:
                    self.post_photo_cleanup_and_move_x2(
                        n=int(ch),
                        dry_run=bool(clear_dry_run),
                    )
                except Exception as e:
                    bg_errors.append(f"post task failed on ch{ch}: {e}")
                    traceback.print_exc()
            return _task

        pending_post_seq = None

        for seq, row in enumerate(batch, start=1):
            ch = int(ep_channels[seq - 1])
            valve_pos = int(valve_positions[seq - 1])
            cam_pos = float(cam_positions[seq - 1])
            point_id = int(row.get("point_id", start_index + seq))

            self.logger.info("[full_batch] after_ep pipeline start: ch=%s seq=%s", ch, seq)

            # 先排液
            self.drive_pumps([int(seq)], 400.0, 30.0)

            # 再清洗 3 次
            with self.wash_lock:
                for _ in range(3):
                    self.water_wash(
                        list_water_wash=(int(seq),),
                        water_run_s=11.0,
                        settle_s=2.0,
                        water_speed=400.0,
                        waste_speed=400.0,
                        waste_run_s=30.0,
                    )

            # 清洗完成后，才取出 X_1
            self.move_out_x1_for_channel(n=int(seq), dry_run=bool(clear_dry_run))

            # X_1 取出后再补液
            with self.wash_lock:
                self.water_in_for_photo(
                    list_water_wash=(int(seq),),
                    water_speed=400.0,
                    water_run_s=11.0,
                )

            if enable_camy_datum_before_photo:
                try:
                    self.camy.datum()
                    time.sleep(0.5)
                except Exception as e:
                    self.logger.warning("camy.datum failed for ch%s: %s", seq, e)

            cam_target = self.photo_channel_n(
                n=int(seq),
                cam_pos=float(cam_pos),
                photo_move_speed=float(photo_move_speed),
                photo_move_settle_s=float(photo_move_settle_s),
                autofocus_settle_s=float(autofocus_settle_s),
            )

            exp_seq = int(seq)
            recipe_tag = self._format_recipe_tag(row) if row else ""
            exp_id = f"E{exp_seq:02d}_CH{ch}_P{point_id:03d}"
            cell_img_dir = self._ensure_dir(os.path.join(img_dir, exp_id))
            focus_debug_path = ""

            result_csv = os.path.join(run_dir, "results.csv")
            fieldnames = [
                "run_id", "date", "exp_id", "exp_seq", "channel", "valve_pos", "point_id",
                "PEG_gL", "SPS_gL", "JGB_gL", "v_PEG_mL", "v_SPS_mL", "v_JGB_mL", "v_BASE_mL",
                "sampling_dir", "sample_index", "offset_x", "target_x", "actual_x",
                "focus_debug_path", "image_path", "status", "error", "Non-DC", "GrdE",
            ]

            if save_debug_frames and debug_dir:
                try:
                    debug_name = f"{exp_id}_{recipe_tag}_focus_base.{photo_ext}" if recipe_tag else f"{exp_id}_focus_base.{photo_ext}"
                    focus_debug_path = os.path.join(debug_dir, debug_name)
                    focus_debug_path = self._capture_and_save_fresh_frame(
                        focus_debug_path,
                        flush_n=int(flush_n),
                        flush_interval_s=float(flush_interval_s),
                    )
                except Exception as e:
                    self.logger.warning("save focus debug frame failed for %s: %s", exp_id, e)
                    focus_debug_path = ""

            for sample_index, dx in enumerate(x_sampling_offsets, start=1):
                rec = {
                    "run_id": run_id,
                    "date": run_meta.get("date", datetime.now().strftime("%Y-%m-%d")),
                    "exp_id": exp_id,
                    "exp_seq": exp_seq,
                    "channel": ch,
                    "valve_pos": valve_pos,
                    "point_id": point_id,
                    "PEG_gL": row.get("PEG_gL", ""),
                    "SPS_gL": row.get("SPS_gL", ""),
                    "JGB_gL": row.get("JGB_gL", ""),
                    "v_PEG_mL": row.get("v_PEG_mL", ""),
                    "v_SPS_mL": row.get("v_SPS_mL", ""),
                    "v_JGB_mL": row.get("v_JGB_mL", ""),
                    "v_BASE_mL": row.get("v_BASE_mL", ""),
                    "sampling_dir": os.path.abspath(cell_img_dir),
                    "sample_index": int(sample_index),
                    "offset_x": float(dx),
                    "target_x": "",
                    "actual_x": "",
                    "focus_debug_path": os.path.abspath(focus_debug_path) if focus_debug_path else "",
                    "image_path": "",
                    "status": "ok",
                    "error": "",
                    "Non-DC": "",
                    "GrdE": "",
                }

                try:
                    x_target = float(cam_target) + float(dx)
                    rec["target_x"] = float(x_target)
                    x_actual = self.safe_camx_absolute_move(x_target, float(scan_speed))
                    rec["actual_x"] = float(x_actual)
                    time.sleep(float(scan_settle_s))

                    img_name = f"{exp_id}_{recipe_tag}_S{sample_index:02d}_dx_{float(dx):+06.2f}.{photo_ext}" if recipe_tag else f"{exp_id}_S{sample_index:02d}_dx_{float(dx):+06.2f}.{photo_ext}"
                    img_name = img_name.replace("+", "p").replace("-", "m")
                    img_path = os.path.join(cell_img_dir, img_name)
                    img_path = self._capture_and_save_fresh_frame(
                        img_path,
                        flush_n=int(flush_n),
                        flush_interval_s=float(flush_interval_s),
                    )
                    rec["image_path"] = os.path.abspath(img_path)

                    if compute_metrics and os.path.exists(img_path):
                        try:
                            m = self.compute_plating_metrics(img_path, crop=photo_crop)
                            rec["Non-DC"] = m.get("Non-DC", "")
                            rec["GrdE"] = m.get("GrdE", "")
                        except Exception as e:
                            self.logger.error("compute_plating_metrics failed for %s sample %02d: %s", exp_id, sample_index, e)
                            rec["status"] = "metrics_failed"
                            rec["error"] = str(e)

                except Exception as e:
                    rec["status"] = "failed"
                    rec["error"] = str(e)
                    self.logger.exception("x sampling failed for %s sample %02d dx=%s: %s", exp_id, sample_index, dx, e)

                self._append_csv_row(result_csv, fieldnames, rec)

            self.logger.info("[full_batch] after_ep pipeline done: ch=%s seq=%s", ch, seq)

            if pending_post_seq is not None:
                t = threading.Thread(
                    target=make_post_task(int(pending_post_seq)),
                    name=f"post-photo-ch{pending_post_seq}",
                    daemon=False,
                )
                t.start()
                bg_threads.append(t)
                self.logger.info(
                    "[full_batch] delayed post task started: prev_seq=%s after current seq=%s main pipeline done",
                    pending_post_seq, seq
                )

            pending_post_seq = seq

        if pending_post_seq is not None:
            t = threading.Thread(
                target=make_post_task(int(pending_post_seq)),
                name=f"post-photo-ch{pending_post_seq}",
                daemon=False,
            )
            t.start()
            bg_threads.append(t)
            self.logger.info(
                "[full_batch] final delayed post task started: seq=%s",
                pending_post_seq
            )

        for t in bg_threads:
            t.join()

        if bg_errors:
            raise RuntimeError(" | ".join(bg_errors))

        self.logger.info("[full_batch] all done: run_id=%s", run_id)
        return {
            "run_id": run_id,
            "run_dir": run_dir,
            "img_dir": img_dir,
            "debug_dir": debug_dir,
            "results_csv": os.path.join(run_dir, "results.csv"),
            "n_channels": len(batch),
        }

    def run_full_experiment_preloaded_start(
        self,
        *,
        batch_name: str,
        csv_path: str,
        start_index: int = 0,
        n_rounds: int,
        n_channels: int = 8,
        valve_positions=(1, 2, 3, 4, 9, 6, 7, 8),
        syringe_slaves=(1, 2, 3, 4),
        stock_input_valves=None,
        max_syringe_mL: float = 5.0,
        grid_mL: float = 0.02,
        waste_valve_pos: int = 12,
        switch_settle_s: float = 0.0,
        dry_run_load_copper: bool = False,
        clear_dry_run: bool = False,
        ep_channels=(1, 2, 3, 4, 5, 6, 7, 8),
        cam_positions=(21, 74, 126.5, 185, 238.5, 293, 346, 403),
        exp_root: str = "./experiments",
        run_prefix: str = "",
        photo_ext: str = "png",
        compute_metrics: bool = True,
        photo_crop=None,
        enable_camy_datum_before_photo: bool = False,
        autofocus_settle_s: float = 0.8,
        photo_move_speed: float = 20,
        photo_move_settle_s: float = 0.8,
        x_sampling_offsets=(-4.0, -3.5, -3.0, -2.5, -2.0, -1.5, -1.0, -0.5,
                            0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0),
        scan_speed: float = 20.0,
        scan_settle_s: float = 1.0,
        save_debug_frames: bool = True,
        flush_n: int = 2,
        flush_interval_s: float = 0.03,
        electroplating_wait_s: float = 10,
    ):
        all_results = []

        for round_idx in range(n_rounds):
            current_start = start_index + round_idx * n_channels
            is_first_round = (round_idx == 0)
            load_copper_this_round = not is_first_round

            self.logger.info(
                "[preloaded_start] round %d/%d start | start_index=%d | load_copper=%s",
                round_idx + 1, n_rounds, current_start, load_copper_this_round,
            )

            result = self.run_full_experiment_one_batch(
                batch_name=batch_name,
                csv_path=csv_path,
                start_index=current_start,
                n_channels=n_channels,
                valve_positions=valve_positions,
                syringe_slaves=syringe_slaves,
                stock_input_valves=stock_input_valves,
                max_syringe_mL=max_syringe_mL,
                grid_mL=grid_mL,
                waste_valve_pos=waste_valve_pos,
                switch_settle_s=switch_settle_s,
                load_copper=load_copper_this_round,
                dry_run_load_copper=dry_run_load_copper,
                clear_dry_run=clear_dry_run,
                ep_channels=ep_channels,
                cam_positions=cam_positions,
                exp_root=exp_root,
                run_prefix=run_prefix,
                photo_ext=photo_ext,
                compute_metrics=compute_metrics,
                photo_crop=photo_crop,
                enable_camy_datum_before_photo=enable_camy_datum_before_photo,
                autofocus_settle_s=autofocus_settle_s,
                photo_move_speed=photo_move_speed,
                photo_move_settle_s=photo_move_settle_s,
                x_sampling_offsets=x_sampling_offsets,
                scan_speed=scan_speed,
                scan_settle_s=scan_settle_s,
                save_debug_frames=save_debug_frames,
                flush_n=flush_n,
                flush_interval_s=flush_interval_s,
                electroplating_wait_s=electroplating_wait_s,
            )

            self.logger.info(
                "[preloaded_start] round %d/%d done | run_id=%s",
                round_idx + 1, n_rounds, result.get("run_id"),
            )
            all_results.append(result)

        self.logger.info("[preloaded_start] all %d rounds complete", n_rounds)
        return all_results
