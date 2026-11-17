import os
import logging

logging.basicConfig(level=logging.INFO)

try:
    from app import app
    logging.info("main.py loaded - using locked app.py only")
except Exception:
    logging.exception("main.py: importing app.py failed; serving degraded /api/health only")
    from flask import Flask, jsonify

    app = Flask(__name__)

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"status": "degraded", "error": "app import failed"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT") or 5000))
