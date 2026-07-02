import os
import sys
import subprocess
import threading
import queue
import time
import sqlite3
import signal
import json
import shutil
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, send_file
from werkzeug.utils import secure_filename
from io import BytesIO
import zipfile

# ============================================================
#  CONFIG
# ============================================================
APP_SECRET = "your-secret-key-here-change-in-production"
DB_PATH = "panel.db"
PID_FILE = "bot.pid"
LOG_FILE = "bot.log"
BOT_FILES_DIR = "botfiles"
LOGIN_FILE = "login.json"

# ============================================================
#  USER MANAGEMENT - login.json
# ============================================================
def get_login_credentials():
    """Get login credentials from login.json"""
    if os.path.exists(LOGIN_FILE):
        try:
            with open(LOGIN_FILE, 'r') as f:
                data = json.load(f)
                return data.get('username', '1234'), data.get('password', '1234')
        except:
            pass
    return '1234', '1234'

def save_login_credentials(username, password):
    """Save login credentials to login.json"""
    with open(LOGIN_FILE, 'w') as f:
        json.dump({'username': username, 'password': password}, f, indent=2)

# ============================================================
#  FLASK APP
# ============================================================
app = Flask(__name__)
app.secret_key = APP_SECRET
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

if not os.path.exists(BOT_FILES_DIR):
    os.makedirs(BOT_FILES_DIR)

# ============================================================
#  DATABASE SETUP
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('main_file', 'bot.py')")
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# ============================================================
#  BOT PROCESS MANAGEMENT
# ============================================================
bot_process = None
bot_logs = queue.Queue(maxsize=5000)
bot_running = False
bot_status = "Stopped"
bot_status_message = ""
bot_start_time = None

class LogEntry:
    def __init__(self, message, level="INFO"):
        self.message = message
        self.level = level
        self.timestamp = datetime.now().strftime("%H:%M:%S")

def clear_logs():
    while not bot_logs.empty():
        try:
            bot_logs.get_nowait()
        except queue.Empty:
            break

def add_log(message, level="INFO"):
    """Add log entry"""
    entry = LogEntry(message, level)
    try:
        bot_logs.put_nowait(entry)
    except queue.Full:
        try:
            bot_logs.get_nowait()
            bot_logs.put_nowait(entry)
        except:
            pass
    
    # File log with timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_entry = f"[{timestamp}] [{level}] {message}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(file_entry + "\n")
    except:
        pass

def read_output(pipe, is_stderr=False):
    for line in iter(pipe.readline, b''):
        if line:
            try:
                msg = line.decode("utf-8", errors="replace").strip()
                if msg:
                    add_log(msg, "ERR" if is_stderr else "OUT")
            except:
                pass
    pipe.close()

def kill_existing_bot():
    """Kill any existing bot process from PID file"""
    global bot_process, bot_running
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, 0)
                if sys.platform == "win32":
                    subprocess.call(['taskkill', '/F', '/PID', str(pid)])
                else:
                    os.kill(pid, signal.SIGKILL)
                add_log(f"Killed old bot process (PID {pid})", "WARN")
            except OSError:
                pass
            os.remove(PID_FILE)
        except:
            pass
    
    if bot_process is not None:
        try:
            if bot_process.poll() is None:
                if sys.platform == "win32":
                    bot_process.kill()
                else:
                    os.kill(bot_process.pid, signal.SIGKILL)
        except:
            pass
        bot_process = None
    
    bot_running = False

def install_requirements():
    req_file = os.path.join(BOT_FILES_DIR, "requirements.txt")
    if not os.path.exists(req_file):
        add_log("No requirements.txt found", "WARN")
        return True
    add_log("Installing requirements from requirements.txt...", "INFO")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req_file],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            add_log("Requirements installed successfully", "INFO")
            for line in result.stdout.splitlines():
                if line.strip():
                    add_log(f"pip: {line.strip()}", "OUT")
            return True
        else:
            add_log(f"Failed to install requirements (code {result.returncode})", "ERROR")
            for line in result.stderr.splitlines():
                if line.strip():
                    add_log(f"pip error: {line.strip()}", "ERR")
            return False
    except subprocess.TimeoutExpired:
        add_log("Requirements installation timed out", "ERROR")
        return False
    except Exception as e:
        add_log(f"Error installing requirements: {str(e)}", "ERROR")
        return False

def start_bot():
    global bot_process, bot_running, bot_status, bot_status_message, bot_start_time
    
    kill_existing_bot()
    clear_logs()
    
    main_file = get_setting("main_file") or "bot.py"
    main_file_path = os.path.join(BOT_FILES_DIR, main_file)
    
    py_files = [f for f in os.listdir(BOT_FILES_DIR) if f.endswith(".py") and os.path.isfile(os.path.join(BOT_FILES_DIR, f))]
    
    if not os.path.exists(main_file_path):
        if len(py_files) == 1:
            main_file = py_files[0]
            set_setting("main_file", main_file)
            main_file_path = os.path.join(BOT_FILES_DIR, main_file)
            add_log(f"Auto-detected main file: {main_file}", "INFO")
        else:
            add_log(f"Main file '{main_file}' not found", "ERROR")
            bot_status = "Error"
            bot_status_message = f"Main file '{main_file}' not found"
            return False
    
    if not os.path.exists(main_file_path):
        add_log(f"Main file '{main_file}' not found in botfiles", "ERROR")
        bot_status = "Error"
        bot_status_message = f"Main file '{main_file}' not found"
        return False

    if not install_requirements():
        bot_status = "Error"
        bot_status_message = "Failed to install requirements"
        return False

    add_log(f"Starting bot: {main_file}", "INFO")
    bot_status = "Starting"
    bot_status_message = "Starting bot..."
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"=== Bot started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        
        bot_process = subprocess.Popen(
            [sys.executable, main_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=BOT_FILES_DIR,
            env=os.environ.copy(),
            text=False,
            bufsize=0
        )
        with open(PID_FILE, "w") as f:
            f.write(str(bot_process.pid))
        threading.Thread(target=read_output, args=(bot_process.stdout, False), daemon=True).start()
        threading.Thread(target=read_output, args=(bot_process.stderr, True), daemon=True).start()
        bot_running = True
        bot_start_time = datetime.now()
        bot_status = "Running"
        bot_status_message = "Bot is running"
        add_log(f"Bot started with PID {bot_process.pid}", "INFO")
        return True
    except Exception as e:
        add_log(f"Failed to start bot: {str(e)}", "ERROR")
        bot_status = "Error"
        bot_status_message = f"Error: {str(e)}"
        bot_running = False
        return False

def stop_bot():
    global bot_process, bot_running, bot_status, bot_status_message, bot_start_time
    if not bot_running or bot_process is None:
        add_log("Bot is not running", "WARN")
        bot_status = "Stopped"
        bot_status_message = "Bot is stopped"
        return False
    bot_status = "Stopping"
    bot_status_message = "Stopping bot..."
    add_log("Stopping bot...", "INFO")
    try:
        if sys.platform == "win32":
            bot_process.terminate()
        else:
            os.kill(bot_process.pid, signal.SIGTERM)
        for _ in range(30):
            if bot_process.poll() is not None:
                break
            time.sleep(0.5)
        if bot_process.poll() is None:
            add_log("Force killing process...", "WARN")
            if sys.platform == "win32":
                bot_process.kill()
            else:
                os.kill(bot_process.pid, signal.SIGKILL)
            time.sleep(0.5)
        add_log(f"Bot stopped (exit code {bot_process.returncode})", "INFO")
        bot_running = False
        bot_status = "Stopped"
        bot_status_message = "Bot stopped"
        bot_process = None
        bot_start_time = None
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        clear_logs()
        return True
    except Exception as e:
        add_log(f"Error stopping bot: {str(e)}", "ERROR")
        bot_status = "Error"
        bot_status_message = f"Error stopping: {str(e)}"
        bot_running = False
        return False

def restart_bot():
    add_log("Restarting bot...", "INFO")
    kill_existing_bot()
    time.sleep(1)
    return start_bot()

def get_bot_status():
    global bot_process, bot_running, bot_status, bot_status_message, bot_start_time
    if bot_running and bot_process is not None:
        poll = bot_process.poll()
        if poll is not None:
            add_log(f"Bot process died unexpectedly (exit code {poll})", "ERROR")
            bot_running = False
            bot_status = "Stopped"
            bot_status_message = f"Crashed with code {poll}"
            bot_process = None
            bot_start_time = None
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
    uptime = None
    if bot_start_time and bot_running:
        diff = datetime.now() - bot_start_time
        seconds = int(diff.total_seconds())
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        uptime = f"{hours}h {minutes}m {secs}s"
    return {
        "running": bot_running,
        "status": bot_status,
        "message": bot_status_message,
        "uptime": uptime,
        "pid": bot_process.pid if bot_process else None,
        "main_file": get_setting("main_file") or "bot.py"
    }

def get_recent_logs(limit=200):
    logs = []
    temp_logs = []
    while not bot_logs.empty():
        try:
            temp_logs.append(bot_logs.get_nowait())
        except queue.Empty:
            break
    for log in temp_logs:
        try:
            bot_logs.put_nowait(log)
        except queue.Full:
            break
    return temp_logs[-limit:]

def get_all_logs_text():
    """Get all logs as plain text for copying"""
    logs = []
    while not bot_logs.empty():
        try:
            logs.append(bot_logs.get_nowait())
        except queue.Empty:
            break
    for log in logs:
        try:
            bot_logs.put_nowait(log)
        except queue.Full:
            break
    return "\n".join([f"{log.timestamp} {log.message}" for log in logs])

# ============================================================
#  FILE OPERATIONS
# ============================================================
def list_files():
    files = []
    try:
        for entry in os.listdir(BOT_FILES_DIR):
            if entry.startswith("."):
                continue
            path = os.path.join(BOT_FILES_DIR, entry)
            if os.path.isfile(path):
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
                files.append({
                    "name": entry,
                    "size": size,
                    "size_display": format_size(size),
                    "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "is_python": entry.endswith(".py"),
                    "is_text": is_text_file(entry)
                })
    except:
        pass
    return sorted(files, key=lambda x: x["name"])

def format_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

def is_text_file(filename):
    text_extensions = ['.py', '.txt', '.json', '.yml', '.yaml', '.xml', '.html', '.css', '.js', '.md', '.cfg', '.conf', '.ini', '.sh', '.bash']
    return os.path.splitext(filename)[1].lower() in text_extensions

def read_file_content(filename):
    filepath = os.path.join(BOT_FILES_DIR, filename)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except:
        return None

def write_file_content(filename, content):
    filepath = os.path.join(BOT_FILES_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except:
        return False

def delete_file(filename):
    filepath = os.path.join(BOT_FILES_DIR, filename)
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            return True
        return False
    except:
        return False

def rename_file(old_name, new_name):
    old_path = os.path.join(BOT_FILES_DIR, old_name)
    new_path = os.path.join(BOT_FILES_DIR, new_name)
    try:
        if os.path.exists(old_path) and not os.path.exists(new_path):
            os.rename(old_path, new_path)
            return True
        return False
    except:
        return False

# ============================================================
#  DOWNLOAD FUNCTIONS
# ============================================================
def download_single_file(filename):
    filepath = os.path.join(BOT_FILES_DIR, filename)
    if not os.path.exists(filepath) or not os.path.isfile(filepath):
        return None
    return send_file(filepath, as_attachment=True, download_name=filename)

def download_all_files():
    files = [f for f in os.listdir(BOT_FILES_DIR) if os.path.isfile(os.path.join(BOT_FILES_DIR, f)) and not f.startswith('.')]
    if not files:
        return None
    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            filepath = os.path.join(BOT_FILES_DIR, file)
            zf.write(filepath, file)
    memory_file.seek(0)
    return send_file(
        memory_file,
        as_attachment=True,
        download_name='botfiles_backup.zip',
        mimetype='application/zip'
    )

# ============================================================
#  ROUTES
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    
    if username == "SEMY" and password == "M4X":
        session["logged_in"] = True
        session["username"] = username
        session["is_super_admin"] = True
        return redirect(url_for("dashboard"))
    
    if request.method == "POST":
        stored_user, stored_pass = get_login_credentials()
        if username == stored_user and password == stored_pass:
            session["logged_in"] = True
            session["username"] = username
            session["is_super_admin"] = False
            return redirect(url_for("dashboard"))
        else:
            return render_template_string(LOGIN_PAGE, error="Invalid username or password")
    return render_template_string(LOGIN_PAGE, error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def dashboard():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    status = get_bot_status()
    settings = {"main_file": get_setting("main_file") or "bot.py"}
    logs = get_recent_logs(200)
    return render_template_string(DASHBOARD_PAGE, status=status, settings=settings, logs=logs, active_page="dashboard")

@app.route("/files")
def files_page():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    files = list_files()
    return render_template_string(FILES_PAGE, files=files, error=None, active_page="files")

@app.route("/upload", methods=["POST"])
def upload_files():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    kill_existing_bot()
    uploaded = request.files.getlist("files")
    for f in uploaded:
        if f and f.filename:
            filename = secure_filename(f.filename)
            if filename:
                filepath = os.path.join(BOT_FILES_DIR, filename)
                f.save(filepath)
    py_files = [f for f in os.listdir(BOT_FILES_DIR) if f.endswith(".py") and os.path.isfile(os.path.join(BOT_FILES_DIR, f))]
    if len(py_files) == 1:
        set_setting("main_file", py_files[0])
        add_log(f"Auto-set main file to {py_files[0]} after upload", "INFO")
    return redirect(url_for("files_page"))

@app.route("/files/edit/<path:filename>", methods=["GET", "POST"])
def edit_file(filename):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    if ".." in filename or filename.startswith("/"):
        return "Invalid file path", 400
    if request.method == "POST":
        content = request.form.get("content", "")
        if write_file_content(filename, content):
            kill_existing_bot()
            return redirect(url_for("files_page"))
        else:
            return render_template_string(FILES_PAGE, files=list_files(), error=f"Failed to save {filename}")
    content = read_file_content(filename)
    if content is None:
        return render_template_string(FILES_PAGE, files=list_files(), error=f"Cannot read {filename} (binary or inaccessible)")
    return render_template_string(EDIT_FILE_PAGE, filename=filename, content=content, active_page="files")

@app.route("/files/delete/<path:filename>", methods=["POST"])
def delete_file_route(filename):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "Invalid path"}), 400
    success = delete_file(filename)
    return jsonify({"success": success})

@app.route("/files/rename", methods=["POST"])
def rename_file_route():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    old_name = request.form.get("old_name", "").strip()
    new_name = request.form.get("new_name", "").strip()
    if not old_name or not new_name:
        return jsonify({"error": "Missing names"}), 400
    if ".." in old_name or ".." in new_name:
        return jsonify({"error": "Invalid path"}), 400
    success = rename_file(old_name, new_name)
    return jsonify({"success": success})

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    is_super_admin = session.get("is_super_admin", False)
    
    if request.method == "POST":
        main_file = request.form.get("main_file", "").strip()
        if main_file:
            set_setting("main_file", main_file)
        
        if not is_super_admin:
            new_username = request.form.get("new_username", "").strip()
            new_password = request.form.get("new_password", "").strip()
            confirm_password = request.form.get("confirm_password", "").strip()
            
            if new_username and new_password:
                if new_password == confirm_password:
                    save_login_credentials(new_username, new_password)
                    session["username"] = new_username
                    return render_template_string(SETTINGS_PAGE, settings={"main_file": get_setting("main_file") or "bot.py"}, 
                                                files=list_files(), active_page="settings", 
                                                success="Credentials updated successfully!", is_super_admin=is_super_admin)
                else:
                    return render_template_string(SETTINGS_PAGE, settings={"main_file": get_setting("main_file") or "bot.py"}, 
                                                files=list_files(), active_page="settings", 
                                                error="Passwords do not match!", is_super_admin=is_super_admin)
        
        return redirect(url_for("settings_page"))
    
    settings = {"main_file": get_setting("main_file") or "bot.py"}
    files = list_files()
    return render_template_string(SETTINGS_PAGE, settings=settings, files=files, 
                                 active_page="settings", error=None, success=None, 
                                 is_super_admin=is_super_admin)

# ============================================================
#  DOWNLOAD ROUTES
# ============================================================

@app.route("/files/download/<path:filename>")
def download_single_file_route(filename):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    if ".." in filename or filename.startswith("/"):
        return "Invalid file path", 400
    response = download_single_file(filename)
    if response is None:
        return "File not found", 404
    return response

@app.route("/files/download_all")
def download_all_files_route():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    response = download_all_files()
    if response is None:
        return "No files to download", 404
    return response

# ---------- API ----------
@app.route("/api/status")
def api_status():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(get_bot_status())

@app.route("/api/logs")
def api_logs():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    limit = request.args.get("limit", 200, type=int)
    logs = get_recent_logs(limit)
    # Convert LogEntry objects to dict for JSON
    log_list = []
    for log in logs:
        if hasattr(log, 'message'):
            log_list.append({
                'timestamp': log.timestamp,
                'message': log.message,
                'level': log.level
            })
        else:
            log_list.append({'timestamp': '', 'message': str(log), 'level': 'INFO'})
    return jsonify({"logs": log_list})

@app.route("/api/copy_logs")
def api_copy_logs():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    logs_text = get_all_logs_text()
    return jsonify({"logs": logs_text})

@app.route("/api/start", methods=["POST"])
def api_start():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    if bot_running:
        return jsonify({"success": False, "message": "Bot is already running"})
    success = start_bot()
    return jsonify({"success": success, "message": bot_status_message})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    if not bot_running:
        return jsonify({"success": False, "message": "Bot is not running"})
    success = stop_bot()
    return jsonify({"success": success, "message": bot_status_message})

@app.route("/api/restart", methods=["POST"])
def api_restart():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    success = restart_bot()
    return jsonify({"success": success, "message": bot_status_message})

@app.route("/api/clear_logs", methods=["POST"])
def api_clear_logs():
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    clear_logs()
    return jsonify({"success": True})

# ============================================================
#  HTML TEMPLATES - Updated Console
# ============================================================

LOGIN_PAGE = '''
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Login — Bot Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#070a16;
  --panel:rgba(20,26,52,0.7);
  --border:rgba(120,140,220,0.15);
  --text:#e8ecff;
  --muted:#8892bf;
  --primary:#6366f1;
  --grad:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.card{background:var(--panel);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:16px;padding:32px 24px;box-shadow:0 10px 40px rgba(0,0,0,0.25);max-width:400px;width:100%}
h2{text-align:center;font-size:24px;margin-bottom:6px}
.subtitle{text-align:center;color:var(--muted);font-size:14px;margin-bottom:24px}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:12px 20px;border-radius:10px;border:0;cursor:pointer;font-weight:600;font-size:14px;font-family:inherit;width:100%;transition:transform .15s,box-shadow .15s}
.btn-primary{background:var(--grad);color:#fff;box-shadow:0 8px 24px rgba(99,102,241,0.35)}
.btn:hover{transform:translateY(-1px)}
input{padding:11px 13px;border-radius:10px;border:1px solid var(--border);background:rgba(10,14,32,0.7);color:var(--text);font-size:14px;font-family:inherit;width:100%;transition:border-color .15s}
input:focus{outline:0;border-color:var(--primary)}
label{display:block;margin-bottom:5px;color:#c8c8e0;font-size:13px;font-weight:500}
.form-group{margin-bottom:16px}
.error{background:rgba(255,60,60,0.15);border:1px solid rgba(255,60,60,0.3);color:#ff6b6b;padding:12px;border-radius:10px;margin-bottom:16px;text-align:center;font-size:14px}
.hint{text-align:center;color:#555577;font-size:12px;margin-top:16px}
</style>
</head><body>
<div class="card">
  <h2>🔐 Login</h2>
  <p class="subtitle">Manage your VPS <span style="color:var(--primary)"> 🔑 </span></p>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <div class="form-group">
      <label>Username</label>
      <input type="text" name="username" required autofocus>
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" name="password" required>
    </div>
    <button type="submit" class="btn btn-primary">Login</button>
  </form>
  <p class="hint">Default: 1234 / 1234</p>
</div>
</body></html>
'''

DASHBOARD_PAGE = '''
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Dashboard — Bot Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#070a16;
  --bg2:#0d1228;
  --panel:rgba(20,26,52,0.7);
  --panel-solid:#141a34;
  --border:rgba(120,140,220,0.15);
  --text:#e8ecff;
  --muted:#8892bf;
  --primary:#6366f1;
  --grad:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
  --ok:#10b981;
  --danger:#ef4444;
  --warn:#f59e0b;
}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:#a5b4fc;text-decoration:none}
a:hover{color:#c7d2fe}
.topbar{
  display:flex;justify-content:space-between;align-items:center;
  padding:12px 16px;
  background:rgba(7,10,22,0.9);backdrop-filter:blur(14px);
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:50;
}
.brand{font-weight:800;font-size:16px;letter-spacing:.3px;display:flex;align-items:center;gap:8px}
.brand .logo{width:28px;height:28px;border-radius:8px;background:var(--grad);display:grid;place-items:center;font-size:14px}
.gradient-text{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.btn{display:inline-flex;align-items:center;gap:4px;padding:6px 12px;border-radius:8px;border:0;cursor:pointer;font-weight:600;font-size:12px;font-family:inherit;transition:transform .15s}
.btn:hover{transform:scale(1.02)}
.btn-ghost{background:rgba(255,255,255,0.06);color:#e8ecff;border:1px solid var(--border)}
.btn-primary{background:var(--grad);color:#fff;box-shadow:0 4px 16px rgba(99,102,241,0.3)}
.btn-ok{background:linear-gradient(135deg,#10b981,#059669);color:#fff}
.btn-danger{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff}
.btn-sm{padding:6px 12px;font-size:12px}
.btn-copy{background:linear-gradient(135deg,#8b5cf6,#6366f1);color:#fff}
.nav{
  display:flex;gap:4px;
  padding:10px 16px;
  background:rgba(7,10,22,0.5);
  border-bottom:1px solid var(--border);
  flex-wrap:wrap;
}
.nav a{
  color:#8892bf;font-size:13px;font-weight:500;
  padding:6px 14px;border-radius:8px;transition:0.2s;
  flex:1;text-align:center;min-width:60px;
}
.nav a:hover{background:rgba(255,255,255,0.05)}
.nav a.active{
  background:var(--panel-solid);
  border:1px solid var(--border);
  color:var(--text);
}
.wrap{padding:12px 16px;max-width:800px;margin:0 auto}
.card{background:var(--panel);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:12px;padding:16px;box-shadow:0 10px 40px rgba(0,0,0,0.25)}
.card+.card{margin-top:12px}
h3{font-size:16px;font-weight:600;margin-bottom:10px}
.muted{color:var(--muted);font-size:12px}
.status-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.status-dot{width:12px;height:12px;border-radius:50%;display:inline-block;flex-shrink:0}
.status-dot.running{background:#4ade80;box-shadow:0 0 16px #4ade8044}
.status-dot.stopped{background:#f87171;box-shadow:0 0 16px #f8717144}
.status-dot.starting{background:#fbbf24;box-shadow:0 0 16px #fbbf2444}
.status-dot.error{background:#f87171;box-shadow:0 0 16px #f8717166}
.status-dot.stopping{background:#fbbf24;box-shadow:0 0 16px #fbbf2444}
.status-text{font-size:16px;font-weight:600}
.status-text .sub{font-weight:400;color:var(--muted);font-size:13px}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.controls button{flex:1;min-width:70px;justify-content:center;padding:8px 12px}
.console-body{
  max-height:350px;overflow-y:auto;
  background:#000000;
  padding:10px 12px;
  border-radius:8px;
  font-family:'JetBrains Mono',monospace;
  font-size:12px;
  line-height:1.8;
  color:#b0b0d0;
  border:1px solid var(--border);
}
.console-body .log-line{
  padding:2px 0;
  border-bottom:1px solid rgba(255,255,255,0.03);
  white-space:pre-wrap;
  word-break:break-all;
}
.console-body .log-line .time{
  color:#555577;
  margin-right:10px;
  font-size:11px;
  display:inline-block;
  min-width:55px;
}
.console-body .log-line .level-INFO{color:#4ade80;}
.console-body .log-line .level-OUT{color:#b0b0d0;}
.console-body .log-line .level-ERR{color:#ff4444;}
.console-body .log-line .level-ERROR{color:#ff4444;}
.console-body .log-line .level-WARN{color:#fbbf24;}
.console-body .log-line .level-WARNING{color:#fbbf24;}
.console-body .log-line .message{color:#e8ecff;}
.console-empty{color:#444466;text-align:center;padding:20px 0;font-size:13px}
.flex{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.flex-between{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.gap-8{gap:8px}
.mt-8{margin-top:8px}
.mb-8{margin-bottom:8px}
.text-center{text-align:center}
.text-muted{color:var(--muted);font-size:12px}
.footer{text-align:center;padding:20px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);margin-top:20px}
</style>
</head><body>

<div class="topbar">
  <div class="brand">
    <span class="logo">⚡</span>
    <span>Ultra<span class="gradient-text">VPS</span></span>
  </div>
  <div>
    {% if session.username %}
      <span class="muted" style="margin-right:8px;font-size:12px">{{session.username}}</span>
      <a href="/logout" class="btn btn-ghost btn-sm">Logout</a>
    {% else %}
      <a href="/login" class="btn btn-primary btn-sm">Login</a>
    {% endif %}
  </div>
</div>

<div class="nav">
  <a href="/" class="{% if active_page == 'dashboard' %}active{% endif %}">Dashboard</a>
  <a href="/files" class="{% if active_page == 'files' %}active{% endif %}">Files</a>
  <a href="/settings" class="{% if active_page == 'settings' %}active{% endif %}">Settings</a>
</div>

<div class="wrap">

<div class="card">
  <div class="status-row">
    <span class="status-dot {{ status.status|lower }}"></span>
    <div>
      <div class="status-text">{{ status.status }} <span class="sub">— {{ status.message }}</span></div>
      {% if status.uptime %}<div class="text-muted">⏱ Uptime: <strong>{{ status.uptime }}</strong></div>{% endif %}
    </div>
  </div>
  <div class="controls">
    <button class="btn btn-ok" onclick="controlBot('start')" {% if status.running %}disabled{% endif %}>▶ Start</button>
    <button class="btn btn-danger" onclick="controlBot('stop')" {% if not status.running %}disabled{% endif %}>⏹ Stop</button>
    <button class="btn btn-primary" onclick="controlBot('restart')">🔄 Restart</button>
  </div>
  <div class="text-muted mt-8">📄 Main: <strong>{{ status.main_file }}</strong> {% if status.pid %}· PID: <strong>{{ status.pid }}</strong>{% endif %}</div>
</div>

<div class="card">
  <div class="flex-between">
    <h3 style="margin:0;font-size:14px">📟 Console</h3>
    <div class="flex">
      <button class="btn btn-copy btn-sm" onclick="copyLogs()">📋 Copy Logs</button>
      <button class="btn btn-ghost btn-sm" onclick="clearConsole()">Clear</button>
    </div>
  </div>
  <div class="console-body" id="console-body">
    {% if logs %}
      {% for log in logs %}
      <div class="log-line">
        <span class="time">{{ log.timestamp }}</span>
        <span class="level-{{ log.level }}">[{{ log.level }}]</span>
        <span class="message">{{ log.message }}</span>
      </div>
      {% endfor %}
    {% else %}
      <div class="console-empty">⏳ No logs — start your bot</div>
    {% endif %}
  </div>
</div>

<p class="text-center text-muted" style="margin-top:6px">💡 Requirements.txt auto‑installs on start</p>
</div>

<div class="footer">© ULTRA VPS </div>

<script>
function controlBot(action) {
  const btn = document.querySelector(`.btn-${action === 'start' ? 'ok' : action === 'stop' ? 'danger' : 'primary'}`);
  if (btn && btn.disabled) return;
  fetch(`/api/${action}`, { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        setTimeout(() => location.reload(), 600);
      } else {
        alert('Failed: ' + data.message);
      }
    })
    .catch(() => alert('Network error'));
}

function clearConsole() {
  if (confirm('Clear all logs?')) {
    fetch('/api/clear_logs', { method: 'POST' }).then(() => location.reload());
  }
}

function copyLogs() {
  fetch('/api/copy_logs')
    .then(r => r.json())
    .then(data => {
      if (data.logs) {
        navigator.clipboard.writeText(data.logs).then(() => {
          alert('✅ Logs copied to clipboard!');
        }).catch(() => {
          const textarea = document.createElement('textarea');
          textarea.value = data.logs;
          document.body.appendChild(textarea);
          textarea.select();
          document.execCommand('copy');
          document.body.removeChild(textarea);
          alert('✅ Logs copied to clipboard!');
        });
      } else {
        alert('No logs to copy');
      }
    })
    .catch(() => alert('Failed to copy logs'));
}

function scrollToBottom() {
  const body = document.getElementById('console-body');
  if (body) body.scrollTop = body.scrollHeight;
}

// Update logs every 3 seconds
let lastLogCount = 0;
setInterval(() => {
  fetch('/api/logs?limit=200')
    .then(r => r.json())
    .then(data => {
      if (data.logs && data.logs.length > 0) {
        const body = document.getElementById('console-body');
        if (body) {
          const current = body.querySelectorAll('.log-line').length;
          if (data.logs.length !== current) {
            location.reload();
          } else {
            scrollToBottom();
          }
        }
      }
    })
    .catch(() => {});
}, 3000);
setTimeout(scrollToBottom, 200);
</script>
</body></html>
'''

FILES_PAGE = '''
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Files — Bot Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#070a16;
  --bg2:#0d1228;
  --panel:rgba(20,26,52,0.7);
  --panel-solid:#141a34;
  --border:rgba(120,140,220,0.15);
  --text:#e8ecff;
  --muted:#8892bf;
  --primary:#6366f1;
  --grad:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
  --danger:#ef4444;
  --ok:#10b981;
}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:#a5b4fc;text-decoration:none}
a:hover{color:#c7d2fe}
.topbar{
  display:flex;justify-content:space-between;align-items:center;
  padding:12px 16px;
  background:rgba(7,10,22,0.9);backdrop-filter:blur(14px);
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:50;
}
.brand{font-weight:800;font-size:16px;letter-spacing:.3px;display:flex;align-items:center;gap:8px}
.brand .logo{width:28px;height:28px;border-radius:8px;background:var(--grad);display:grid;place-items:center;font-size:14px}
.gradient-text{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.btn{display:inline-flex;align-items:center;gap:4px;padding:6px 12px;border-radius:8px;border:0;cursor:pointer;font-weight:600;font-size:12px;font-family:inherit;transition:transform .15s}
.btn:hover{transform:scale(1.02)}
.btn-ghost{background:rgba(255,255,255,0.06);color:#e8ecff;border:1px solid var(--border)}
.btn-primary{background:var(--grad);color:#fff;box-shadow:0 4px 16px rgba(99,102,241,0.3)}
.btn-danger{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff}
.btn-success{background:linear-gradient(135deg,#10b981,#059669);color:#fff}
.btn-sm{padding:6px 12px;font-size:12px}
.nav{
  display:flex;gap:4px;
  padding:10px 16px;
  background:rgba(7,10,22,0.5);
  border-bottom:1px solid var(--border);
  flex-wrap:wrap;
}
.nav a{
  color:#8892bf;font-size:13px;font-weight:500;
  padding:6px 14px;border-radius:8px;transition:0.2s;
  flex:1;text-align:center;min-width:60px;
}
.nav a:hover{background:rgba(255,255,255,0.05)}
.nav a.active{
  background:var(--panel-solid);
  border:1px solid var(--border);
  color:var(--text);
}
.wrap{padding:12px 16px;max-width:800px;margin:0 auto}
.card{background:var(--panel);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:12px;padding:16px;box-shadow:0 10px 40px rgba(0,0,0,0.25)}
.download-bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;padding:10px 0;border-bottom:1px solid var(--border)}
h2{font-size:18px;margin-bottom:12px}
.muted{color:var(--muted);font-size:12px}
.upload-area{border:2px dashed var(--border);border-radius:10px;padding:16px;text-align:center;margin-bottom:16px;transition:0.2s}
.upload-area:hover{border-color:var(--primary)}
.upload-area input[type="file"]{display:none}
.file-item{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);flex-wrap:wrap;gap:6px}
.file-item:last-child{border-bottom:none}
.file-item .left{display:flex;align-items:center;gap:10px;flex:1;min-width:120px}
.file-item .actions{display:flex;gap:4px;flex-wrap:wrap}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:10px;font-weight:700;letter-spacing:.3px;text-transform:uppercase}
.badge-py{background:#4ade8033;color:#4ade80}
input{padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:rgba(10,14,32,0.7);color:var(--text);font-size:14px;font-family:inherit;width:100%}
input:focus{outline:0;border-color:var(--primary)}
.error{background:rgba(255,60,60,0.15);border:1px solid rgba(255,60,60,0.3);color:#ff6b6b;padding:10px;border-radius:8px;margin-bottom:12px;font-size:13px}
.flex{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.footer{text-align:center;padding:20px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);margin-top:20px}
.modal{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:1000;align-items:center;justify-content:center;padding:20px}
.modal-content{background:#16161f;border:1px solid #2a2a3a;border-radius:16px;padding:24px;max-width:400px;width:100%}
.modal h3{margin-bottom:12px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
</style>

<div class="topbar">
  <div class="brand"><span class="logo">⚡</span><span>Ultra<span class="gradient-text">VPS</span></span></div>
  <div>
    {% if session.username %}
      <span class="muted" style="margin-right:8px;font-size:12px">{{session.username}}</span>
      <a href="/logout" class="btn btn-ghost btn-sm">Logout</a>
    {% else %}
      <a href="/login" class="btn btn-primary btn-sm">Login</a>
    {% endif %}
  </div>
</div>

<div class="nav">
  <a href="/" class="{% if active_page == 'dashboard' %}active{% endif %}">Dashboard</a>
  <a href="/files" class="{% if active_page == 'files' %}active{% endif %}">Files</a>
  <a href="/settings" class="{% if active_page == 'settings' %}active{% endif %}">Settings</a>
</div>

<div class="wrap">
<h2>📁 Files</h2>

<div class="upload-area">
  <form action="/upload" method="POST" enctype="multipart/form-data" id="uploadForm">
    <div class="flex" style="justify-content:center">
      <label for="fileInput" style="background:var(--panel-solid);padding:8px 16px;border-radius:8px;cursor:pointer">📤 Choose</label>
      <input type="file" id="fileInput" name="files" multiple onchange="updateFileCount()">
      <span id="fileCount" style="color:var(--muted);font-size:13px">No file</span>
      <button type="submit" class="btn btn-primary">⬆ Upload</button>
    </div>
  </form>
</div>

<div class="download-bar">
  <a href="/files/download_all" class="btn btn-success">📦 Download All Files</a>
</div>

{% if error %}<div class="error">{{ error }}</div>{% endif %}

<div class="card">
  {% if files %}
    {% for file in files %}
    <div class="file-item">
      <div class="left">
        <span style="font-size:18px">{% if file.is_python %}🐍{% else %}📄{% endif %}</span>
        <span style="font-weight:500;font-size:14px"><a href="/files/edit/{{ file.name }}">{{ file.name }}</a></span>
        {% if file.is_python %}<span class="badge badge-py">Python</span>{% endif %}
        <span class="muted">{{ file.size_display }}</span>
      </div>
      <div class="actions">
        <a href="/files/download/{{ file.name }}" class="btn btn-success btn-sm">⬇️</a>
        <a href="/files/edit/{{ file.name }}" class="btn btn-ghost btn-sm">✏️</a>
        <button class="btn btn-ghost btn-sm" onclick="openRename('{{ file.name }}')">📝</button>
        <button class="btn btn-danger btn-sm" onclick="deleteFile('{{ file.name }}')">🗑️</button>
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="text-center muted" style="padding:20px 0">📭 No files</div>
  {% endif %}
</div>
</div>

<div class="footer">© ULTRA VPS </div>

<div class="modal" id="renameModal" onclick="if(event.target===this)closeRename()">
  <div class="modal-content">
    <h3>📝 Rename</h3>
    <input type="text" id="renameInput" placeholder="New filename">
    <input type="hidden" id="renameOld">
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeRename()">Cancel</button>
      <button class="btn btn-primary" onclick="confirmRename()">Rename</button>
    </div>
  </div>
</div>

<script>
let renameTarget='';
function openRename(f){renameTarget=f;document.getElementById('renameOld').value=f;document.getElementById('renameInput').value=f;document.getElementById('renameModal').style.display='flex';setTimeout(()=>document.getElementById('renameInput').focus(),100)}
function closeRename(){document.getElementById('renameModal').style.display='none';renameTarget=''}
function confirmRename(){const n=document.getElementById('renameInput').value.trim();const o=document.getElementById('renameOld').value;if(!n||n===o){closeRename();return}const fd=new FormData();fd.append('old_name',o);fd.append('new_name',n);fetch('/files/rename',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{if(d.success)location.reload();else alert('Failed')}).catch(()=>alert('Error'));closeRename()}
function deleteFile(f){if(!confirm('Delete "'+f+'"?'))return;fetch('/files/delete/'+encodeURIComponent(f),{method:'POST'}).then(r=>r.json()).then(d=>{if(d.success)location.reload();else alert('Failed')}).catch(()=>alert('Error'))}
function updateFileCount(){const i=document.getElementById('fileInput');const c=i.files.length;document.getElementById('fileCount').textContent=c?c+' file'+(c>1?'s':'')+' selected':'No file'}
</script>
</body></html>
'''

EDIT_FILE_PAGE = '''
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Edit {{ filename }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#070a16;
  --panel:rgba(20,26,52,0.7);
  --border:rgba(120,140,220,0.15);
  --text:#e8ecff;
  --muted:#8892bf;
  --primary:#6366f1;
  --grad:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:#a5b4fc;text-decoration:none}
.topbar{
  display:flex;justify-content:space-between;align-items:center;
  padding:12px 16px;
  background:rgba(7,10,22,0.9);backdrop-filter:blur(14px);
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:50;
}
.brand{font-weight:800;font-size:16px;display:flex;align-items:center;gap:8px}
.brand .logo{width:28px;height:28px;border-radius:8px;background:var(--grad);display:grid;place-items:center;font-size:14px}
.gradient-text{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.btn{display:inline-flex;align-items:center;padding:6px 14px;border-radius:8px;border:0;cursor:pointer;font-weight:600;font-size:12px;font-family:inherit}
.btn-primary{background:var(--grad);color:#fff;box-shadow:0 4px 16px rgba(99,102,241,0.3)}
.btn-ghost{background:rgba(255,255,255,0.06);color:#e8ecff;border:1px solid var(--border)}
.nav{
  display:flex;gap:4px;padding:10px 16px;
  background:rgba(7,10,22,0.5);border-bottom:1px solid var(--border);
  flex-wrap:wrap;
}
.nav a{color:#8892bf;font-size:13px;font-weight:500;padding:6px 14px;border-radius:8px;flex:1;text-align:center;min-width:60px}
.nav a:hover{background:rgba(255,255,255,0.05)}
.nav a.active{background:var(--panel);border:1px solid var(--border);color:var(--text)}
.wrap{padding:12px 16px;max-width:800px;margin:0 auto}
.card{background:var(--panel);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:12px;padding:16px;box-shadow:0 10px 40px rgba(0,0,0,0.25)}
textarea{width:100%;min-height:350px;padding:12px;border-radius:8px;border:1px solid var(--border);background:rgba(10,14,32,0.7);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;line-height:1.6;resize:vertical;outline:none}
textarea:focus{outline:0;border-color:var(--primary)}
code{background:rgba(99,102,241,0.12);color:#c7d2fe;padding:2px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-size:12px}
.flex{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.footer{text-align:center;padding:20px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);margin-top:20px}
</style>
</head><body>

<div class="topbar">
  <div class="brand"><span class="logo">⚡</span><span>Ultra<span class="gradient-text">VPS</span></span></div>
  <div>
    {% if session.username %}
      <span class="muted" style="margin-right:8px;font-size:12px;color:#8892bf">{{session.username}}</span>
      <a href="/logout" class="btn btn-ghost">Logout</a>
    {% endif %}
  </div>
</div>

<div class="nav">
  <a href="/" class="{% if active_page == 'dashboard' %}active{% endif %}">Dashboard</a>
  <a href="/files" class="{% if active_page == 'files' %}active{% endif %}">Files</a>
  <a href="/settings" class="{% if active_page == 'settings' %}active{% endif %}">Settings</a>
</div>

<div class="wrap">
  <div class="flex" style="justify-content:space-between;margin-bottom:12px">
    <h2 style="font-size:18px">✏️ <code>{{ filename }}</code></h2>
    <div class="flex">
      <a href="/files" class="btn btn-ghost">Cancel</a>
      <button class="btn btn-primary" onclick="saveFile()">💾 Save</button>
    </div>
  </div>
  <div class="card" style="padding:0;overflow:hidden">
    <form method="POST" id="editForm">
      <textarea name="content" id="editorContent" spellcheck="false">{{ content }}</textarea>
    </form>
  </div>
</div>

<div class="footer">© ULTRA VPS </div>

<script>
function saveFile(){const f=document.getElementById('editForm');const d=new FormData(f);fetch(window.location.href,{method:'POST',body:d}).then(r=>{if(r.ok)window.location.href='/files';else alert('Failed')}).catch(()=>alert('Error'))}
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();saveFile()}})
</script>
</body></html>
'''

SETTINGS_PAGE = '''
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Settings — Bot Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#070a16;
  --panel:rgba(20,26,52,0.7);
  --panel-solid:#141a34;
  --border:rgba(120,140,220,0.15);
  --text:#e8ecff;
  --muted:#8892bf;
  --primary:#6366f1;
  --grad:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
  --ok:#10b981;
  --danger:#ef4444;
}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:#a5b4fc;text-decoration:none}
.topbar{
  display:flex;justify-content:space-between;align-items:center;
  padding:12px 16px;
  background:rgba(7,10,22,0.9);backdrop-filter:blur(14px);
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:50;
}
.brand{font-weight:800;font-size:16px;display:flex;align-items:center;gap:8px}
.brand .logo{width:28px;height:28px;border-radius:8px;background:var(--grad);display:grid;place-items:center;font-size:14px}
.gradient-text{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.btn{display:inline-flex;align-items:center;padding:6px 14px;border-radius:8px;border:0;cursor:pointer;font-weight:600;font-size:12px;font-family:inherit}
.btn-primary{background:var(--grad);color:#fff;box-shadow:0 4px 16px rgba(99,102,241,0.3)}
.btn-ghost{background:rgba(255,255,255,0.06);color:#e8ecff;border:1px solid var(--border)}
.nav{
  display:flex;gap:4px;padding:10px 16px;
  background:rgba(7,10,22,0.5);border-bottom:1px solid var(--border);
  flex-wrap:wrap;
}
.nav a{color:#8892bf;font-size:13px;font-weight:500;padding:6px 14px;border-radius:8px;flex:1;text-align:center;min-width:60px}
.nav a:hover{background:rgba(255,255,255,0.05)}
.nav a.active{background:var(--panel-solid);border:1px solid var(--border);color:var(--text)}
.wrap{padding:12px 16px;max-width:800px;margin:0 auto}
.card{background:var(--panel);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:12px;padding:16px;box-shadow:0 10px 40px rgba(0,0,0,0.25);max-width:500px}
.card+.card{margin-top:12px}
h2{font-size:18px;margin-bottom:12px}
h3{font-size:15px;margin-bottom:6px}
h4{color:var(--muted);font-size:13px;margin-bottom:6px}
.muted{color:var(--muted);font-size:12px}
input{padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:rgba(10,14,32,0.7);color:var(--text);font-size:14px;font-family:inherit;width:100%}
input:focus{outline:0;border-color:var(--primary)}
label{display:block;margin-bottom:4px;color:#c8c8e0;font-size:13px;font-weight:500}
.form-group{margin-bottom:14px}
.tag{display:inline-block;padding:4px 12px;margin:3px 4px 3px 0;background:var(--panel-solid);border:1px solid var(--border);border-radius:14px;font-size:12px}
.tag .py{color:#4ade80;font-size:10px;margin-left:4px}
.success{background:rgba(16,185,129,0.15);border:1px solid rgba(16,185,129,0.3);color:#4ade80;padding:10px;border-radius:8px;margin-bottom:12px;font-size:13px}
.error{background:rgba(255,60,60,0.15);border:1px solid rgba(255,60,60,0.3);color:#ff6b6b;padding:10px;border-radius:8px;margin-bottom:12px;font-size:13px}
.footer{text-align:center;padding:20px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);margin-top:20px}
</style>
</head><body>

<div class="topbar">
  <div class="brand"><span class="logo">⚡</span><span>Ultra<span class="gradient-text">VPS</span></span></div>
  <div>
    {% if session.username %}
      <span class="muted" style="margin-right:8px;font-size:12px;color:#8892bf">{{session.username}}</span>
      <a href="/logout" class="btn btn-ghost">Logout</a>
    {% else %}
      <a href="/login" class="btn btn-primary">Login</a>
    {% endif %}
  </div>
</div>

<div class="nav">
  <a href="/" class="{% if active_page == 'dashboard' %}active{% endif %}">Dashboard</a>
  <a href="/files" class="{% if active_page == 'files' %}active{% endif %}">Files</a>
  <a href="/settings" class="{% if active_page == 'settings' %}active{% endif %}">Settings</a>
</div>

<div class="wrap">
<h2>⚙️ Settings</h2>

{% if error %}<div class="error">{{ error }}</div>{% endif %}
{% if success %}<div class="success">{{ success }}</div>{% endif %}

<div class="card">
  <h3>📄 Bot Configuration</h3>
  <p class="muted" style="margin-bottom:12px">Set the main Python file to run.</p>
  <form method="POST">
    <div class="form-group">
      <label>Main File</label>
      <input type="text" name="main_file" value="{{ settings.main_file }}" list="pyfiles">
      <datalist id="pyfiles">
        {% for f in files if f.is_python %}
        <option value="{{ f.name }}">
        {% endfor %}
      </datalist>
    </div>
    <button type="submit" class="btn btn-primary" style="width:100%;justify-content:center">💾 Save</button>
  </form>
  <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
    <h4>📂 Python Files</h4>
    <div style="margin-top:6px">
      {% for f in files if f.is_python %}
      <span class="tag">{{ f.name }} <span class="py">🐍</span></span>
      {% else %}
      <span class="muted">No Python files found</span>
      {% endfor %}
    </div>
  </div>
</div>

{% if not is_super_admin %}
<div class="card">
  <h3>🔑 Change Credentials</h3>
  <p class="muted" style="margin-bottom:12px">Update your login username and password.</p>
  <form method="POST">
    <div class="form-group">
      <label>New Username</label>
      <input type="text" name="new_username" placeholder="Enter new username">
    </div>
    <div class="form-group">
      <label>New Password</label>
      <input type="password" name="new_password" placeholder="Enter new password">
    </div>
    <div class="form-group">
      <label>Confirm Password</label>
      <input type="password" name="confirm_password" placeholder="Confirm new password">
    </div>
    <button type="submit" class="btn btn-primary" style="width:100%;justify-content:center">🔒 Change Credentials</button>
  </form>
</div>
{% else %}
<div class="card" style="border:1px solid rgba(99,102,241,0.3)">
  <h3>👑 Super Admin</h3>
  <p class="muted">You are logged in as a permanent super admin. Credentials cannot be changed.</p>
  <div style="margin-top:8px;background:rgba(99,102,241,0.1);padding:8px 12px;border-radius:6px;font-family:monospace;font-size:12px;color:#a5b4fc">
    Username: SEMY · Password: M4X
  </div>
</div>
{% endif %}

</div>

<div class="footer">© ULTRA VPS </div>
</body></html>
'''

# ============================================================
#  MAIN
# ============================================================
if __name__ == "__main__":
    init_db()
    if not get_setting("main_file"):
        set_setting("main_file", "bot.py")
    if not os.path.exists(BOT_FILES_DIR):
        os.makedirs(BOT_FILES_DIR)
    
    if not os.path.exists(LOGIN_FILE):
        save_login_credentials("1234", "1234")
    
    kill_existing_bot()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)