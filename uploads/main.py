import subprocess
import json
import threading
import sys
import time
import os


class CodexChatClient:
    def __init__(self):
        self.req_id = 0
        self.thread_id = None
        self.turn_in_progress = False

        print("[系统]: 正在启动 Codex app-server 子进程...")

        # 启动 Codex app-server 子进程
        # 注意：加入了 shell=True 以修复 Windows 上的 FileNotFoundError [WinError 2]
        # 启动 Codex app-server 子进程
        try:
            self.process = subprocess.Popen(
                ["codex", "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",  # <--- 添加这一行强制使用 UTF-8 解码
                bufsize=1,
                shell=True,
            )
        except Exception as e:
            print(f"[致命错误]: 无法启动 Codex 进程。详情: {e}")
            sys.exit(1)

        # 启动后台线程读取服务器输出
        self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader_thread.start()

    def send_request(self, method, params):
        """发送带有 ID 的请求 [cite: 18]"""
        self.req_id += 1
        msg = {"method": method, "id": self.req_id, "params": params}
        self.process.stdin.write(json.dumps(msg) + "\n")
        self.process.stdin.flush()
        return self.req_id

    def send_notification(self, method, params):
        """发送没有 ID 的通知 [cite: 23]"""
        msg = {"method": method, "params": params}
        self.process.stdin.write(json.dumps(msg) + "\n")
        self.process.stdin.flush()

    def _read_stdout(self):
        """后台持续读取并解析服务器的 JSONL 输出 [cite: 12]"""
        for line in iter(self.process.stdout.readline, ''):
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                self._handle_message(msg)
            except json.JSONDecodeError:
                print(f"\n[解析错误]: {line}")

    def _handle_message(self, msg):
        """处理服务器响应和事件流"""
        # 1. 处理请求的响应 (通过 id 匹配) [cite: 20]
        if "id" in msg and "result" in msg:
            result = msg["result"]
            # 捕获 thread/start 的响应 [cite: 150]
            if "thread" in result and "id" in result["thread"] and not self.thread_id:
                self.thread_id = result["thread"]["id"]
                print(f"\n[系统]: 已创建对话线程 ({self.thread_id})")

        # 2. 处理服务器推送的通知 (无 id，有 method) [cite: 23]
        elif "method" in msg:
            method = msg["method"]
            params = msg.get("params", {})

            # 捕获流式文本增量并打印，实现打字机效果 [cite: 654]
            if method == "item/agentMessage/delta":
                # 具体 delta 字段可能因版本而异，这里假设在 params 的 text 或 delta 中
                delta_text = params.get("delta", params.get("text", ""))
                print(delta_text, end="", flush=True)

            # 轮次完成，解除阻塞 [cite: 622]
            elif method == "turn/completed":
                print("\n")  # 换行收尾
                self.turn_in_progress = False

            # 处理错误 [cite: 663]
            elif method == "turn/failed":
                error_info = params.get('error', {})
                error_msg = (
                    error_info.get('message', '未知错误')
                    if isinstance(error_info, dict)
                    else error_info
                )
                print(f"\n[错误]: {error_msg}")
                self.turn_in_progress = False

    def start(self):
        """执行生命周期"""
        # 1. 初始化握手：必须在连接后立即发送 initialize [cite: 89]
        print("[系统]: 正在初始化连接...")
        self.send_request(
            "initialize",
            {
                "clientInfo": {
                    "name": "python_cli",
                    "title": "Python Custom Chat",
                    "version": "1.0.0",
                }
            },
        )
        time.sleep(0.5)  # 等待服务器处理
        self.send_notification(
            "initialized", {}
        )  # 发出 initialized 通知完成握手 [cite: 89]

        # 2. 启动新对话线程 (Thread) [cite: 82]
        self.send_request(
            "thread/start", {"model": "gpt-5.1-codex"}
        )  # 可按需修改模型 [cite: 251]

        # 等待线程 ID 分配
        timeout = 5
        start_time = time.time()
        while not self.thread_id:
            if time.time() - start_time > timeout:
                print(
                    "\n[系统]: 无法获取 Thread ID，可能是 Codex 没有正确响应，请检查环境变量或授权状态。"
                )
                self.process.kill()
                return
            time.sleep(0.1)

        # 3. 进入多轮对话交互循环
        print("==================================================")
        print("Codex CLI 已就绪！输入 'exit' 或 'quit' 退出。")
        print("==================================================")

        while True:
            try:
                user_input = input("\n你: ")
                if user_input.lower() in ['exit', 'quit']:
                    break
                if not user_input.strip():
                    continue

                print("Codex: ", end="", flush=True)
                self.turn_in_progress = True

                # 开启新一轮交互 (Turn) [cite: 83]
                self.send_request(
                    "turn/start",
                    {
                        "threadId": self.thread_id,
                        "input": [{"type": "text", "text": user_input}],  # [cite: 413]
                    },
                )

                # 阻塞主线程，等待轮次完成 (由后台读取线程更改状态)
                while self.turn_in_progress:
                    time.sleep(0.1)

            except KeyboardInterrupt:
                # 处理 Ctrl+C 中断轮次
                if self.turn_in_progress:
                    print("\n[系统]: 正在中断生成...")
                    self.send_request(
                        "turn/interrupt",
                        {
                            "threadId": self.thread_id,  # [cite: 496]
                        },
                    )
                    self.turn_in_progress = False
                else:
                    break

        print("\n[系统]: 连接已关闭。")
        self.process.kill()


if __name__ == "__main__":
    client = CodexChatClient()
    client.start()
