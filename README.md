# Python Docker Image Downloader

A web-based tool that lets you download Docker images as `.tar` files directly from any registry, **no Docker daemon required** on your machine. Load the downloaded file with `docker load -i image.tar`.

Useful when Docker Hub (or another registry) is blocked, or when you need to transfer images to an air-gapped system.

---

## How it works

```
Browser  →  Flask  →  Docker Registry HTTP API v2
                        ├── GET /v2/<repo>/manifests/<tag>   (manifest)
                        └── GET /v2/<repo>/blobs/<digest>    (layers + config)
                     ← assembles docker-save .tar
         ←  streams .tar to browser
```

1. **Manifest fetch**, The backend authenticates (Bearer token) and pulls the image manifest. Multi-arch images return a *manifest list*; the backend picks the matching platform.
2. **Blob downloads**, Each layer blob (gzip or zstd) is downloaded to a server-side temp directory and decompressed.
3. **Archive creation**, Files are packaged into the standard `docker save` tar format (`manifest.json` + `<config>.json` + `<layer_id>/layer.tar`).
4. **Streaming response**, The `.tar` is streamed to the browser with a `Content-Length` header so the progress bar works.

---

## Quick start (local Python)

Requires [uv](https://docs.astral.sh/uv/), `pip install uv` or `winget install astral-sh.uv`.

```bash
# 1. Sync dependencies into a managed virtual environment
uv sync

# 2. (Optional) copy & edit environment variables
cp .env.example .env

# 3. Run the development server
uv run python app.py
```

Open <http://localhost:5000> in your browser.

---

## Quick start (Docker Compose)

```bash
cp .env.example .env      # edit if you want Docker Hub credentials
docker compose up --build
```

Open <http://localhost:5000>.

---

## Project structure

```
.
├── app.py                          # Flask routes
├── docker_registry/
│   ├── __init__.py                 # Public API re-exports
│   ├── client.py                   # DockerRegistry class (orchestration)
│   ├── auth.py                     # Bearer-token authentication
│   ├── manifest.py                 # Manifest fetch + platform resolution
│   ├── blob.py                     # Blob download + layer decompression
│   ├── archive.py                  # docker-save tar creation
│   ├── parser.py                   # Image reference parsing + validation
│   ├── constants.py                # Shared constants & media types
│   └── exceptions.py               # DockerRegistryError
├── pyproject.toml                  # uv / PEP 517 project metadata
├── .python-version                 # Pins Python version for uv
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── templates/
│   └── index.html                  # Markup only, links style.css + script.js
└── static/
    ├── style.css
    ├── script.js
    └── logo.svg
```

---

## API endpoints

| Method | Path | Query params | Description |
|--------|------|-------------|-------------|
| `GET` | `/` | N/A | Web UI |
| `GET` | `/platforms` | `name` | Returns available OS/arch platforms for a multi-arch image |
| `GET` | `/image` | `name`, `os`, `arch`, `variant` | Downloads image as `docker save` `.tar` |

---

## Supported registries

| Registry | Example |
|----------|---------|
| Docker Hub (official) | `ubuntu:24.04` |
| Docker Hub (user) | `nginx:alpine` |
| GitHub Container Registry | `ghcr.io/owner/repo:tag` |
| Google Container Registry | `gcr.io/project/image:tag` |
| Amazon ECR (public) | `public.ecr.aws/library/python:3.12` |
| Any OCI-compliant registry | `registry.example.com/myapp:1.0` |

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Listening port |
| `DEBUG` | `false` | Flask debug mode |
| `DOCKER_HUB_USERNAME` | _(empty)_ | Docker Hub username (raises pull rate limit) |
| `DOCKER_HUB_PASSWORD` | _(empty)_ | Docker Hub password / access token |

---

## Notes

- **Docker Hub rate limits**: anonymous pulls are limited to ~100 per 6 hours per server IP. Set `DOCKER_HUB_USERNAME` / `DOCKER_HUB_PASSWORD` to raise it to 200 (free account) or unlimited (paid).
- **Disk space**: layers are written to a temp directory during packaging. Make sure the server has enough free space for the largest image you expect to download (~2–3× the compressed size).
- **Security**: SSRF protection blocks private/loopback IP registries. Input validation rejects malformed image names.

---

## License

See [LICENSE](LICENSE).
