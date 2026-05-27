from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from flask_cors import CORS
import os
import json
import threading
import time
from collections import deque
from werkzeug.utils import secure_filename
from uuid import uuid4
import websocket

app = Flask(__name__)
app.secret_key = 'super_secret_lab_key'
CORS(app, resources={r"/api/*": {"origins": "*"}})  # 允许所有来源访问 /api 路由
ACCESS_CODE = 'autochem2026'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
FOLDER_DB = os.path.join(BASE_DIR, 'thread_folders.json')
BRIDGE_WS_URL = os.environ.get('CODEX_BRIDGE_WS_URL', 'ws://127.0.0.1:4500')
BRIDGE_IDLE_TTL_SECONDS = int(os.environ.get('CODEX_BRIDGE_IDLE_TTL_SECONDS', '900'))
BRIDGE_RPC_TIMEOUT_SECONDS = int(os.environ.get('CODEX_BRIDGE_RPC_TIMEOUT_SECONDS', '90'))

bridge_clients = {}
bridge_clients_lock = threading.Lock()


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class BridgeClient:
    def __init__(self, client_id, ws_url):
        self.client_id = client_id
        self.ws_url = ws_url
        self.ws = None
        self.ws_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.events_cond = threading.Condition()
        self.pending = {}
        self.events = deque()
        self.reader_thread = None
        self.next_id = 1
        self.closed = False
        self.initialized = False
        self.last_active = time.time()

    def touch(self):
        self.last_active = time.time()

    def is_idle(self, now_ts):
        return (now_ts - self.last_active) > BRIDGE_IDLE_TTL_SECONDS

    def is_connected(self):
        return self.ws is not None and not self.closed

    def ensure_connected(self):
        with self.ws_lock:
            if self.ws is not None and not self.closed:
                return

            self.closed = False
            self.initialized = False
            try:
                self.ws = websocket.create_connection(self.ws_url, timeout=8, suppress_origin=True)
            except Exception as exc:
                self.ws = None
                self.closed = True
                raise BridgeError('APP_SERVER_UNAVAILABLE', f'Cannot connect to app-server at {self.ws_url}: {exc}')

            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()

    def close(self):
        with self.ws_lock:
            self.closed = True
            self.initialized = False
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None
        self._fail_pending('UPSTREAM_DISCONNECTED', 'Bridge connection closed')

    def _fail_pending(self, code, message):
        with self.pending_lock:
            pending_items = list(self.pending.values())
            self.pending.clear()
        for item in pending_items:
            item['response'] = {'id': item['id'], 'error': {'code': code, 'message': message}}
            item['event'].set()

    def _push_event(self, msg):
        with self.events_cond:
            self.events.append(msg)
            self.events_cond.notify_all()

    def _reader_loop(self):
        while True:
            if self.closed:
                break
            raw = None
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException:
                break
            except Exception:
                break

            if raw is None:
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if isinstance(msg, dict) and 'id' in msg:
                pending = None
                with self.pending_lock:
                    pending = self.pending.pop(msg['id'], None)
                if pending:
                    pending['response'] = msg
                    pending['event'].set()
                    continue

            self._push_event(msg)

        with self.ws_lock:
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None
            self.closed = True
            self.initialized = False
        self._fail_pending('UPSTREAM_DISCONNECTED', 'app-server websocket disconnected')
        self._push_event({'method': 'bridge/disconnected', 'params': {'clientId': self.client_id}})

    def _send(self, payload):
        self.ensure_connected()
        with self.send_lock:
            if self.closed or self.ws is None:
                raise BridgeError('APP_SERVER_UNAVAILABLE', 'Bridge websocket is not connected')
            try:
                self.ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as exc:
                self.closed = True
                raise BridgeError('UPSTREAM_SEND_FAILED', f'Failed to send payload: {exc}')

    def send_notify(self, method, params):
        self.touch()
        payload = {'method': method, 'params': params if params is not None else {}}
        self._send(payload)

    def send_rpc(self, method, params, timeout_sec):
        self.touch()
        with self.pending_lock:
            request_id = self.next_id
            self.next_id += 1
            event = threading.Event()
            pending_slot = {'id': request_id, 'event': event, 'response': None}
            self.pending[request_id] = pending_slot

        try:
            self._send({'method': method, 'id': request_id, 'params': params if params is not None else {}})
        except Exception:
            with self.pending_lock:
                self.pending.pop(request_id, None)
            raise

        if not event.wait(timeout_sec):
            with self.pending_lock:
                self.pending.pop(request_id, None)
            raise BridgeError('RPC_TIMEOUT', f'RPC timeout after {timeout_sec}s for method {method}')

        return pending_slot['response']

    def poll_events(self, timeout_sec=25):
        self.touch()
        self.ensure_connected()
        timeout_sec = max(1, min(timeout_sec, 30))
        deadline = time.time() + timeout_sec
        with self.events_cond:
            while not self.events:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self.events_cond.wait(timeout=remaining)

            events = list(self.events)
            self.events.clear()
            return events


def cleanup_idle_bridge_clients():
    now_ts = time.time()
    stale_clients = []
    with bridge_clients_lock:
        for client_id, client in list(bridge_clients.items()):
            if client.is_idle(now_ts):
                stale_clients.append((client_id, client))
                bridge_clients.pop(client_id, None)
    for _, client in stale_clients:
        client.close()


def get_bridge_client(client_id):
    cleanup_idle_bridge_clients()
    with bridge_clients_lock:
        client = bridge_clients.get(client_id)
        if client is None:
            client = BridgeClient(client_id=client_id, ws_url=BRIDGE_WS_URL)
            bridge_clients[client_id] = client
    client.touch()
    return client


def require_login():
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401
    return None


def build_unique_filepath(filename):
    safe_name = secure_filename(filename)
    name, ext = os.path.splitext(safe_name)
    candidate = safe_name
    filepath = os.path.join(UPLOAD_FOLDER, candidate)

    while os.path.exists(filepath):
        candidate = f"{name}_{uuid4().hex[:8]}{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, candidate)

    return candidate, filepath


def ensure_folder_db_shape(data):
    if not isinstance(data, dict):
        data = {}

    folders = data.get('folders') if isinstance(data.get('folders'), list) else []
    mapping = data.get('mapping') if isinstance(data.get('mapping'), dict) else {}
    thread_names = data.get('thread_names') if isinstance(data.get('thread_names'), dict) else {}
    return {
        'folders': folders,
        'mapping': mapping,
        'thread_names': thread_names,
    }


def load_folders():
    if os.path.exists(FOLDER_DB):
        try:
            with open(FOLDER_DB, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return ensure_folder_db_shape(data)
        except json.JSONDecodeError:
            pass
    return ensure_folder_db_shape({})


def save_folders(data):
    with open(FOLDER_DB, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.route('/api/bridge/rpc', methods=['POST'])
def bridge_rpc():
    auth_error = require_login()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    client_id = (data.get('client_id') or '').strip()
    method = (data.get('method') or '').strip()
    params = data.get('params', {})

    if not client_id or not method:
        return jsonify({'error': {'code': 'BAD_REQUEST', 'message': 'client_id and method are required'}}), 400

    client = get_bridge_client(client_id)
    try:
        response = client.send_rpc(method=method, params=params, timeout_sec=BRIDGE_RPC_TIMEOUT_SECONDS)
        return jsonify(response)
    except BridgeError as exc:
        return jsonify({'error': {'code': exc.code, 'message': exc.message}})


@app.route('/api/bridge/notify', methods=['POST'])
def bridge_notify():
    auth_error = require_login()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    client_id = (data.get('client_id') or '').strip()
    method = (data.get('method') or '').strip()
    params = data.get('params', {})

    if not client_id or not method:
        return jsonify({'error': {'code': 'BAD_REQUEST', 'message': 'client_id and method are required'}}), 400

    client = get_bridge_client(client_id)
    try:
        client.send_notify(method=method, params=params)
        return jsonify({'status': 'ok'})
    except BridgeError as exc:
        return jsonify({'error': {'code': exc.code, 'message': exc.message}})


@app.route('/api/bridge/events', methods=['GET'])
def bridge_events():
    auth_error = require_login()
    if auth_error:
        return auth_error

    client_id = (request.args.get('client_id') or '').strip()
    if not client_id:
        return jsonify({'error': {'code': 'BAD_REQUEST', 'message': 'client_id is required'}}), 400

    try:
        timeout_sec = float(request.args.get('timeout', 25))
    except (TypeError, ValueError):
        timeout_sec = 25

    client = get_bridge_client(client_id)
    try:
        events = client.poll_events(timeout_sec=timeout_sec)
        return jsonify({'events': events, 'connected': client.is_connected()})
    except BridgeError as exc:
        return jsonify({'events': [], 'connected': False, 'error': {'code': exc.code, 'message': exc.message}})


@app.route('/api/bridge/disconnect', methods=['POST'])
def bridge_disconnect():
    auth_error = require_login()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    client_id = (data.get('client_id') or '').strip()
    if not client_id:
        return jsonify({'status': 'ok'})

    client = None
    with bridge_clients_lock:
        client = bridge_clients.pop(client_id, None)
    if client:
        client.close()

    return jsonify({'status': 'ok'})


# ================= 文件夹管理 API =================
@app.route('/api/folders', methods=['GET'])
def get_folders():
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401
    return jsonify(load_folders())  # 确保返回 JSON 响应


@app.route('/api/folders/create', methods=['POST'])
def create_folder():
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401
    name = request.json.get('folder_name', '').strip()
    db = load_folders()
    if name and name not in db['folders']:
        db['folders'].append(name)
        save_folders(db)
    return jsonify({'status': 'ok'})  # 确保返回 JSON 响应


@app.route('/api/folders/move', methods=['POST'])
def move_folder():
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401
    data = request.json
    thread_id = data.get('thread_id')
    folder_name = data.get('folder_name', '').strip()
    db = load_folders()
    if folder_name:
        if folder_name not in db['folders']:
            db['folders'].append(folder_name)
        db['mapping'][thread_id] = folder_name
        if folder_name == '__deleted__':
            db['thread_names'].pop(thread_id, None)
    else:
        db['mapping'].pop(thread_id, None)
    save_folders(db)
    return jsonify({'status': 'ok'})  # 确保返回 JSON 响应


# [新增] 文件夹重命名
@app.route('/api/folders/rename', methods=['POST'])
def rename_folder():
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401
    data = request.json
    old_name = data.get('old_name', '').strip()
    new_name = data.get('new_name', '').strip()
    db = load_folders()
    if (
        old_name
        and new_name
        and old_name in db['folders']
        and new_name not in db['folders']
    ):
        # 更新文件夹列表
        db['folders'] = [new_name if f == old_name else f for f in db['folders']]
        # 更新所有属于该文件夹的对话映射
        for tid, fname in db['mapping'].items():
            if fname == old_name:
                db['mapping'][tid] = new_name
        save_folders(db)
    return jsonify({'status': 'ok'})  # 确保返回 JSON 响应


# [新增] 文件夹删除
@app.route('/api/folders/delete', methods=['POST'])
def delete_folder():
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401
    folder_name = request.json.get('folder_name', '').strip()
    db = load_folders()
    if folder_name in db['folders']:
        db['folders'].remove(folder_name)
        # 将该文件夹下的所有对话移出到根目录
        db['mapping'] = {k: v for k, v in db['mapping'].items() if v != folder_name}
        save_folders(db)
    return jsonify({'status': 'ok'})  # 确保返回 JSON 响应


# [新增] 聊天会话重命名本地覆盖
@app.route('/api/threads/rename', methods=['POST'])
def rename_thread_local():
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401
    data = request.json
    thread_id = data.get('thread_id')
    new_name = data.get('new_name', '').strip()
    db = load_folders()

    if thread_id:
        if new_name:
            db['thread_names'][thread_id] = new_name
        else:
            db['thread_names'].pop(thread_id, None)
        save_folders(db)
    return jsonify({'status': 'ok'})


@app.route('/api/threads/auto-title', methods=['POST'])
def auto_title_thread():
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401

    data = request.json or {}
    thread_id = (data.get('thread_id') or '').strip()
    if not thread_id:
        return jsonify({'error': 'thread_id is required'}), 400

    return jsonify({'status': 'skipped', 'source': 'disabled', 'title': ''})


# ================= 页面与附件 API =================
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ACCESS_CODE:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            error = '口令错误'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401
    files = request.files.getlist('files')
    if not files:
        single_file = request.files.get('file')
        if single_file:
            files = [single_file]

    valid_files = [file for file in files if file and file.filename]
    if not valid_files:
        return jsonify({'error': '未选择文件'}), 400

    image_exts = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
    uploaded_files = []

    for file in valid_files:
        filename, filepath = build_unique_filepath(file.filename)
        file.save(filepath)
        uploaded_files.append(
            {
                'message': '成功',
                'filename': filename,
                'filepath': filepath,
                'url': url_for('uploaded_file', filename=filename),
                'isImage': filename.lower().endswith(image_exts),
            }
        )

    return jsonify({'message': '成功', 'files': uploaded_files})  # 确保返回 JSON 响应


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    if not session.get('logged_in'):
        return jsonify({'error': '未授权'}), 401
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
