"""
app.py — Flask server with Server-Sent Events for live yt-dlp progress
"""

from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from analyser import analyse_channel_stream

app = Flask(__name__, template_folder=".")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyse", methods=["POST"])
def analyse():
    body = request.get_json(force=True) or {}
    channel_input = (body.get("channel") or "").strip()
    if not channel_input:
        return jsonify({"error": "Please provide a channel URL, @handle, or channel ID."}), 400

    def generate():
        try:
            for event in analyse_channel_stream(channel_input):
                yield event
        except Exception as e:
            yield f"event: error\ndata: {{\"msg\": \"{str(e)}\"}}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


if __name__ == "__main__":
    app.run(debug=True, threaded=True)
