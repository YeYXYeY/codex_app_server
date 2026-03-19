from screen_match_tools import locate_center_on_screen_multiscale
from utils import AtomicObject


import pyautogui


import time


def mouseMove(img, timeout_second, stopListener:AtomicObject[bool]):
    """
    鼠标悬停（移动但不点击）
    """
    start_time = time.time()
    while True:
        if timeout_second > 0 and (time.time() - start_time > timeout_second):
            print(f"等待图片 {img} 超时 ({timeout_second}秒)")
            return False

        if stopListener.value:
            print("操作已被外部停止")
            return False
        try:
            location = locate_center_on_screen_multiscale(img, confidence=0.8)
            if location is not None:
                pyautogui.moveTo(location.x, location.y, duration=0.2)
                return True
            if timeout_second <= 0:
                break
        except pyautogui.ImageNotFoundException:
            pass

        time.sleep(0.1)

    return False


def mouseClick(clickTimes, lOrR, img, timeout_second, stopListener:AtomicObject[bool]):
    """
    timeout_second: 超时时间(秒)，0 表示禁用超时
    """
    start_time = time.time()
    while True:
        if timeout_second > 0 and (time.time() - start_time > timeout_second):
            print(f"等待图片 {img} 超时 ({timeout_second}秒)")
            return False

        if stopListener.value:
            print("操作已被外部停止")
            return False

        try:
            location = locate_center_on_screen_multiscale(img, confidence=0.8)
            if location is not None:
                pyautogui.click(location.x, location.y, clicks=clickTimes, interval=0.2, duration=0.1, button=lOrR)
                return True
            if timeout_second <= 0:
                break
        except pyautogui.ImageNotFoundException:
            pass

        time.sleep(0.1)

    return False