"""
app.py – Flask web application for the Docker Image Downloader.

Endpoints
---------
GET /               → Serve the UI (templates/index.html)
GET /platforms      → Return available platforms for a multi-arch image
GET /image          → Download the image as a docker-save .tar archive
GET /logo.svg       → Serve the application logo
"""

import logging
import os
import shutil

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
)

from docker_registry import DockerRegistry, DockerRegistryError

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB request body limit

# Shared registry client (thread-safe via thread-local sessions inside the class)
registry = DockerRegistry(
    username=os.environ.get("DOCKER_HUB_USERNAME"),
    password=os.environ.get("DOCKER_HUB_PASSWORD"),
)

# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/logo.svg")
def logo():
    return send_from_directory("static", "logo.svg")


@app.route("/platforms")
def get_platforms():
    """
    Query string: ?name=<image_reference>
    Returns JSON: { "platforms": [ { "os": ..., "architecture": ..., "variant": ... }, ... ] }
    """
    image = request.args.get("name", "").strip()
    if not image:
        return jsonify({"error": "missing required name parameter"}), 400

    try:
        platforms = registry.get_platforms(image)
        return jsonify({"platforms": platforms})

    except DockerRegistryError as exc:
        logger.warning("Platform detection failed for %r: %s", image, exc)
        return jsonify({"error": str(exc)}), 400

    except Exception as exc:
        logger.exception("Unexpected error in /platforms for %r", image)
        return jsonify({"error": f"unexpected error: {exc}"}), 500


@app.route("/image")
def download_image():
    """
    Query string:
      ?name=<image>  [&os=linux] [&arch=amd64] [&variant=v8]

    Streams the image as a docker-save .tar file.
    On error (before streaming starts) returns JSON { "error": "..." }.
    """
    image = request.args.get("name", "").strip()
    os_filter = request.args.get("os", "").strip() or None
    arch_filter = request.args.get("arch", "").strip() or None
    variant_filter = request.args.get("variant", "").strip() or None

    if not image:
        return jsonify({"error": "missing required name parameter"}), 400

    # All heavy work happens here – before we start streaming –
    # so errors can still be returned as JSON responses.
    try:
        tar_path, filename, tmpdir = registry.download_image(
            image,
            os_filter=os_filter,
            arch_filter=arch_filter,
            variant_filter=variant_filter,
        )
    except DockerRegistryError as exc:
        logger.warning("Download failed for %r: %s", image, exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Unexpected error downloading %r", image)
        return jsonify({"error": f"download failed: {exc}"}), 500

    file_size = os.path.getsize(tar_path)
    logger.info("Serving %s (%d bytes)", filename, file_size)

    def generate():
        """Read the tar in 64 KB chunks; clean up temp dir when done."""
        try:
            with open(tar_path, "rb") as fh:
                while True:
                    chunk = fh.read(65_536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            logger.info("Cleaned up temp dir for %r", image)

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-tar",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(file_size),
        },
    )


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(_e):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_e):
    return jsonify({"error": "method not allowed"}), 405


@app.errorhandler(413)
def request_too_large(_e):
    return jsonify({"error": "request body too large"}), 413


@app.errorhandler(500)
def internal_error(_e):
    return jsonify({"error": "internal server error"}), 500


# ── Dev server entry-point ────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
