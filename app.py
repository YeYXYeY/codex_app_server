from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from flask_cors import CORS
import os
import json
from werkzeug.utils import secure_filename
from uuid import uuid4

app = Flask(__name__)
app.secret_key = 'super_secret_lab_key'
CORS(app, resources={r"/api/*": {"origins": "*"}})  # 允许所有来源访问 /api 路由
ACCESS_CODE = 'autochem2026'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
FOLDER_DB = os.path.join(BASE_DIR, 'thread_folders.json')


def build_unique_filepath(filename):
    safe_name = secure_filename(filename)
    name, ext = os.path.splitext(safe_name)
    candidate = safe_name
    filepath = os.path.join(UPLOAD_FOLDER, candidate)

    while os.path.exists(filepath):
        candidate = f"{name}_{uuid4().hex[:8]}{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, candidate)

    return candidate, filepath


def load_folders():
    if os.path.exists(FOLDER_DB):
        try:
            with open(FOLDER_DB, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if 'thread_names' not in data:
                    data['thread_names'] = {}
                return data
        except json.JSONDecodeError:
            pass
    return {"folders": [], "mapping": {}, "thread_names": {}}


def save_folders(data):
    with open(FOLDER_DB, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
