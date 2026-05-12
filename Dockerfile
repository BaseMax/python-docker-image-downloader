# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies into a separate prefix to keep the final image lean
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Security: run as non-root
RUN useradd -r -s /bin/false -u 1001 appuser

WORKDIR /app

# Copy installed packages from build stage
COPY --from=builder /install /usr/local

# Copy application source
COPY app.py docker_registry.py ./
COPY templates/ templates/
COPY static/    static/

# Temp directory used for image downloads — give the app user write access
RUN mkdir -p /tmp/docker_dl && chown appuser /tmp/docker_dl

USER appuser

EXPOSE 5000

# gunicorn: 4 sync workers, 1-hour timeout (large images can take a while)
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "4", \
     "--timeout", "3600", \
     "--worker-class", "sync", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
