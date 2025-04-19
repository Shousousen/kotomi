import os
import uuid
import threading
import queue
import requests
from flask import Flask, request, jsonify, send_from_directory, render_template
from uuid import UUID
import webbrowser
from pydub.utils import mediainfo
import yaml

# 設定ファイルの読み込み
def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

config = load_config()

# 設定を適用
VOICEVOX_BASE_URL = config["voicevox"]["base_url"]
AUDIO_FOLDER = config["audio"]["folder"]
DEFAULT_AUDIO_DURATION = config["audio"]["default_duration"]
MAX_AUDIO_FILES = config["audio"].get("max_files", 100)  # 音声ファイルの上限を設定
DEBUG_MODE = config["app"]["debug"]
APP_URL = config["app"]["url"]

app = Flask(__name__, static_folder="static", template_folder="templates")
os.makedirs(AUDIO_FOLDER, exist_ok=True)
app.debug = DEBUG_MODE

synthesis_queue = queue.Queue()
SESSIONS = {}  # session_id: {speaker, queue}

def synthesis_worker():
    while True:
        try:
            text, speaker, session_id, style_name = synthesis_queue.get()
            filename = synthesize_and_save(text, speaker, style_name)
            manage_audio_files()  # 音声ファイルの管理を実行
            if session_id in SESSIONS:
                SESSIONS[session_id]["queue"].put(filename)
            synthesis_queue.task_done()
        except Exception as e:
            print("[エラー] 音声合成中に例外発生:", e)
            synthesis_queue.task_done()

def synthesize_and_save(text, speaker_id, style_name=None):
    query_res = requests.post(
        f"{VOICEVOX_BASE_URL}/audio_query",
        params={"text": text, "speaker": speaker_id}
    )
    query_res.raise_for_status()
    audio_query = query_res.json()

    # スタイル名が指定されていない場合、最もIDの若いスタイルを選択
    if style_name is None:
        speakers_res = requests.get(f"{VOICEVOX_BASE_URL}/speakers")
        speakers_res.raise_for_status()
        speakers = speakers_res.json()

        for speaker in speakers:
            if speaker["id"] == speaker_id:
                style_name = speaker["styles"][0]["name"]  # 最もIDの若いスタイルを選択
                break

    synthesis_res = requests.post(
        f"{VOICEVOX_BASE_URL}/synthesis",
        params={"speaker": speaker_id},
        headers={"Content-Type": "application/json", "Accept": "audio/wav"},
        json={**audio_query, "style": style_name}  # スタイル名をリクエストボディに含める
    )
    synthesis_res.raise_for_status()
    audio_data = synthesis_res.content

    filename = f"{uuid.uuid4().hex}.wav"
    filepath = os.path.join(AUDIO_FOLDER, filename)
    with open(filepath, "wb") as f:
        f.write(audio_data)

    print(f"[INFO] 音声ファイル生成完了: {filename}")
    return filename

@app.route("/")
def index():
    return render_template("index.html", sessions=SESSIONS)

@app.route("/session/create", methods=["GET"])
def init_session():
    try:
        res = requests.get(f"{VOICEVOX_BASE_URL}/speakers")
        res.raise_for_status()
        speakers = res.json()
    except Exception as e:
        print("[エラー] 話者の取得に失敗:", e)
        speakers = []
    return render_template("input.html", speakers=speakers)

@app.route("/speakers", methods=["GET"])
def get_speakers():
    try:
        res = requests.get(f"{VOICEVOX_BASE_URL}/speakers")
        res.raise_for_status()
        speakers = res.json()
        return jsonify(speakers)
    except requests.exceptions.RequestException as e:
        print(f"[エラー] 話者情報の取得に失敗しました: {e}")
        return jsonify([])  # 失敗時は空のリストを返す

@app.route("/session/create", methods=["POST"])
def create_session():
    speaker_uuid = request.form.get("speaker")
    try:
        # UUID形式の検証
        UUID(speaker_uuid, version=4)
    except ValueError:
        return "無効な話者IDです", 400

    session_id = uuid.uuid4().hex[:8]
    SESSIONS[session_id] = {
        "speaker_uuid": speaker_uuid,  # UUIDを保存
        "queue": queue.Queue()
    }
    return render_template("session_created.html", session_id=session_id, speaker=speaker_uuid)

@app.route("/session/<session_id>/player")
def session_player(session_id):
    if session_id not in SESSIONS:
        return "セッションが存在しません", 404
    return render_template("player.html", session_id=session_id)

# Load default session from config
DEFAULT_SESSION_ID = config.get("default_session", {}).get("id")
DEFAULT_SPEAKER_UUID = config.get("default_session", {}).get("speaker_uuid")

if DEFAULT_SESSION_ID and DEFAULT_SPEAKER_UUID:
    SESSIONS[DEFAULT_SESSION_ID] = {
        "speaker_uuid": DEFAULT_SPEAKER_UUID,
        "queue": queue.Queue(),
        "is_default": True  # Mark this session as the default
    }
    print(f"[INFO] Default session initialized: {DEFAULT_SESSION_ID}")

@app.route("/session/<session_id>/speak", methods=["POST"])
def session_speak(session_id):
    if session_id == "default" or session_id not in SESSIONS:
        session_id = DEFAULT_SESSION_ID  # Use default session if not specified

    data = request.get_json()
    text = data.get("text")
    style_name = data.get("style")  # スタイル名を取得
    interrupt = data.get("interrupt", False)  # 中断フラグを取得

    if not text:
        return jsonify(success=False, message="テキストが必要です"), 400

    speaker_uuid = SESSIONS[session_id]["speaker_uuid"]  # UUIDを取得

    # UUIDから対応する音声IDを取得
    try:
        res = requests.get(f"{VOICEVOX_BASE_URL}/speakers")
        res.raise_for_status()
        speakers = res.json()
        speaker_id = None
        for speaker in speakers:
            if speaker["speaker_uuid"] == speaker_uuid:
                speaker_id = speaker["styles"][0]["id"]  # 最初のスタイルのIDを使用
                break
        if speaker_id is None:
            return jsonify(success=False, message="指定された話者が見つかりません"), 400
    except requests.exceptions.RequestException as e:
        print(f"[エラー] 話者情報の取得に失敗しました: {e}")
        return jsonify(success=False, message="話者情報の取得に失敗しました"), 500

    if interrupt:
        # 再生中の音声を中断し、キューをリセット
        SESSIONS[session_id]["queue"] = queue.Queue()
        SESSIONS[session_id]["is_playing"] = False
        print(f"[INFO] 再生を中断しました: {session_id}")

    # 新しい音声タスクをキューに追加
    synthesis_queue.put((text, speaker_id, session_id, style_name))
    print(f"[INFO] 新しい音声タスクを追加しました: {session_id}")

    return jsonify(success=True, message="音声化タスクをキューに追加しました")

@app.route("/session/<session_id>/next", methods=["GET"])
def session_next(session_id):
    if session_id not in SESSIONS:
        return jsonify(filename=None), 404

    session = SESSIONS[session_id]
    q = session["queue"]

    # 再生中フラグが立っている場合は新しい音声を返さない
    if session.get("is_playing", False):
        return jsonify(filename=None)

    if not q.empty():
        filename = q.get()
        session["is_playing"] = True  # 再生中フラグを設定

        # 音声ファイルの長さを取得
        filepath = os.path.join(AUDIO_FOLDER, filename)
        try:
            audio_info = mediainfo(filepath)
            duration = float(audio_info["duration"])  # 再生時間を秒単位で取得
        except Exception as e:
            print(f"[エラー] 音声ファイルの長さ取得に失敗: {e}")
            duration = DEFAULT_AUDIO_DURATION  # デフォルトで10秒に設定

        # 再生時間後にフラグをリセット
        threading.Timer(duration, reset_playing_flag, args=[session_id]).start()

        return jsonify(filename=filename)

    return jsonify(filename=None)

def reset_playing_flag(session_id):
    if session_id in SESSIONS:
        SESSIONS[session_id]["is_playing"] = False
        print(f"[INFO] 再生中フラグをリセットしました: {session_id}")

@app.route("/audio/<filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_FOLDER, filename)

@app.route('/api/session/<session_id>/styles', methods=['GET'])
def get_session_styles(session_id):
    """Retrieve the list of styles available for a given session."""
    session = SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    # Example: Retrieve styles from the session (replace with actual logic)
    styles = session.get("styles", [])
    return jsonify({"styles": styles})

@app.route("/api/sessions", methods=["GET"])
def get_sessions():
    """セッション情報を取得するエンドポイント"""
    sessions_info = {
        session_id: {
            "speaker_uuid": session["speaker_uuid"],
            "queue_size": session["queue"].qsize(),
            "is_playing": session.get("is_playing", False)
        }
        for session_id, session in SESSIONS.items()
    }
    return jsonify(sessions_info)

@app.route("/session/<session_id>/delete", methods=["POST"])
def delete_session(session_id):
    """セッションを削除するエンドポイント"""
    if session_id in SESSIONS:
        del SESSIONS[session_id]
        print(f"[INFO] セッションを削除しました: {session_id}")
        return jsonify(success=True, message="セッションを削除しました")
    return jsonify(success=False, message="セッションが見つかりません"), 404

@app.route("/api/session/<session_id>", methods=["GET"])
def get_session_details(session_id):
    """特定のセッションの詳細情報を取得"""
    session = SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "セッションが見つかりません"}), 404

    session_details = {
        "speaker_uuid": session["speaker_uuid"],
        "queue_size": session["queue"].qsize(),
        "is_playing": session.get("is_playing", False)
    }
    return jsonify(session_details)

@app.route("/session/manage", methods=["GET"])
def manage_sessions():
    """セッション管理画面を表示するエンドポイント"""
    default_session = {k: v for k, v in SESSIONS.items() if v.get("is_default")}
    other_sessions = {k: v for k, v in SESSIONS.items() if not v.get("is_default")}
    return render_template("session_management.html", default_session=default_session, sessions=other_sessions)

def manage_audio_files():
    # キューに残っているファイルを取得
    queued_files = set()
    for session in SESSIONS.values():
        queued_files.update(list(session["queue"].queue))

    # フォルダ内のすべての音声ファイルを取得
    audio_files = sorted(
        [os.path.join(AUDIO_FOLDER, f) for f in os.listdir(AUDIO_FOLDER) if f.endswith(".wav")],
        key=os.path.getctime
    )

    # キューに含まれていないファイルのみを対象に削除を実行
    non_queued_files = [f for f in audio_files if os.path.basename(f) not in queued_files]

    while len(non_queued_files) > MAX_AUDIO_FILES:
        oldest_file = non_queued_files.pop(0)
        try:
            os.remove(oldest_file)
            print(f"[INFO] 古い音声ファイルを削除しました: {oldest_file}")
        except Exception as e:
            print(f"[エラー] 音声ファイルの削除に失敗しました: {e}")

synthesis_thread = threading.Thread(target=synthesis_worker, daemon=True)
synthesis_thread.start()

if __name__ == "__main__":
    port = 5000
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port)
