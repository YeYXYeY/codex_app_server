from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from recorder_protocol import append_event


class InputRecorder:
    def __init__(
        self,
        log_root: str,
        get_session_id: Callable[[], str],
        get_step_seq: Callable[[], int],
        capture_interval_sec: float = 0.8,
        capture_box_size: int = 180,
        full_frame_interval_sec: float = 1.8,
    ):
        self.log_root = log_root
        self.get_session_id = get_session_id
        self.get_step_seq = get_step_seq
        self.capture_interval_sec = float(capture_interval_sec)
        self.capture_box_size = int(capture_box_size)
        self.full_frame_interval_sec = float(full_frame_interval_sec)
        self._mouse_listener = None
        self._keyboard_listener = None
        self._running = False
        self._available = None
        self._unavailable_reason = ""
        self._capture_thread: threading.Thread | None = None
        self._capture_stop = threading.Event()

    def _detect_backend(self):
        if self._available is not None:
            return self._available

        try:
            from pynput import keyboard, mouse  # type: ignore
            self._keyboard_module = keyboard
            self._mouse_module = mouse
            self._available = True
        except Exception as e:
            self._available = False
            self._unavailable_reason = str(e)

        return self._available

    def is_available(self):
        return self._detect_backend()

    def unavailable_reason(self):
        self._detect_backend()
        return self._unavailable_reason

    def _emit(self, event_type: str, payload: dict):
        session_id = str(self.get_session_id() or "").strip()
        if not session_id:
            return

        append_event(
            log_root=self.log_root,
            session_id=session_id,
            event_type=event_type,
            step_seq=int(self.get_step_seq()),
            payload=payload,
        )

    def start(self):
        if self._running:
            return True

        if not self._detect_backend():
            return False

        keyboard = self._keyboard_module
        mouse = self._mouse_module

        def on_click(x, y, button, pressed):
            if not pressed:
                return
            self._emit("mouse_click", {
                "x": int(x),
                "y": int(y),
                "button": str(button),
            })

        def on_scroll(x, y, dx, dy):
            self._emit("mouse_scroll", {
                "x": int(x),
                "y": int(y),
                "dx": int(dx),
                "dy": int(dy),
            })

        def on_press(key):
            key_text: Optional[str] = None
            try:
                key_text = key.char
            except Exception:
                key_text = str(key)

            if not key_text:
                return

            if len(key_text) == 1:
                self._emit("keyboard_text", {"text": key_text})
            else:
                self._emit("keyboard_key", {"key": key_text})

        self._mouse_listener = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
        self._keyboard_listener = keyboard.Listener(on_press=on_press)
        self._mouse_listener.start()
        self._keyboard_listener.start()

        self._capture_stop.clear()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

        self._running = True
        return True

    def _capture_loop(self):
        try:
            import pyautogui  # type: ignore
        except Exception as e:
            self._unavailable_reason = f"cursor_capture backend unavailable: {e}"
            return

        cursor_frame_index = 0
        full_frame_index = 0
        next_cursor_at = 0.0
        next_full_at = 0.0

        while not self._capture_stop.is_set():
            session_id = str(self.get_session_id() or "").strip()
            if not session_id:
                time.sleep(max(0.1, self.capture_interval_sec))
                continue

            now = time.time()

            try:
                if now >= next_cursor_at:
                    x, y = pyautogui.position()
                    size = max(40, int(self.capture_box_size))
                    half = size // 2
                    left = max(0, int(x) - half)
                    top = max(0, int(y) - half)

                    shot = pyautogui.screenshot(region=(left, top, size, size))
                    crop_dir = os.path.join(self.log_root, session_id, "cursor_crops")
                    os.makedirs(crop_dir, exist_ok=True)
                    cursor_frame_index += 1
                    filename = f"cursor_{cursor_frame_index:06d}.png"
                    file_path = os.path.join(crop_dir, filename)
                    shot.save(file_path)

                    self._emit("cursor_crop", {
                        "x": int(x),
                        "y": int(y),
                        "left": int(left),
                        "top": int(top),
                        "width": int(size),
                        "height": int(size),
                        "file": file_path,
                    })
                    next_cursor_at = now + max(0.1, self.capture_interval_sec)

                if now >= next_full_at:
                    full = pyautogui.screenshot()
                    fw, fh = full.size
                    full_dir = os.path.join(self.log_root, session_id, "full_frames")
                    os.makedirs(full_dir, exist_ok=True)
                    full_frame_index += 1
                    full_name = f"full_{full_frame_index:06d}.png"
                    full_path = os.path.join(full_dir, full_name)
                    full.save(full_path)

                    self._emit("full_frame", {
                        "width": int(fw),
                        "height": int(fh),
                        "file": full_path,
                    })
                    next_full_at = now + max(0.3, self.full_frame_interval_sec)
            except Exception:
                pass

            self._capture_stop.wait(timeout=0.1)

    def stop(self):
        self._capture_stop.set()

        if self._capture_thread is not None:
            self._capture_thread.join(timeout=1.0)
            self._capture_thread = None

        if self._mouse_listener is not None:
            self._mouse_listener.stop()
            self._mouse_listener = None

        if self._keyboard_listener is not None:
            self._keyboard_listener.stop()
            self._keyboard_listener = None

        self._running = False

    def is_running(self):
        return self._running
