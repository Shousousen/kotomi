import os
import uuid
import threading
import queue
import requests
from flask import Flask, request, jsonify, send_from_directory, render_template
from uuid import UUID
import webbrowser

app = Flask(__name__, static_folder="static", template_folder="templates")
AUDIO_FOLDER = "static/audio"
os.makedirs(AUDIO_FOLDER, exist_ok=True)

VOICEVOX_BASE_URL = "http://localhost:10101"
synthesis_queue = queue.Queue()
SESSIONS = {}  # session_id: {speaker, queue}

def synthesis_worker():
    while True:
        try:
            text, speaker, session_id, style_name = synthesis_queue.get()
            filename = synthesize_and_save(text, speaker, style_name)
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

@app.route("/session/<session_id>/speak", methods=["POST"])
def session_speak(session_id):
    if session_id not in SESSIONS:
        return jsonify(success=False, message="無効なセッションID"), 404
    data = request.get_json()
    text = data.get("text")
    style_name = data.get("style")  # スタイル名を取得
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

    synthesis_queue.put((text, speaker_id, session_id, style_name))
    return jsonify(success=True, message="音声化タスクをキューに追加しました")

@app.route("/session/<session_id>/next", methods=["GET"])
def session_next(session_id):
    if session_id not in SESSIONS:
        return jsonify(filename=None), 404
    q = SESSIONS[session_id]["queue"]
    if not q.empty():
        filename = q.get()
        return jsonify(filename=filename)
    return jsonify(filename=None)

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

synthesis_thread = threading.Thread(target=synthesis_worker, daemon=True)
synthesis_thread.start()

if __name__ == "__main__":
    port = 5000
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port)
