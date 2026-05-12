FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

RUN useradd -r -s /bin/false -u 1001 appuser

WORKDIR /app

COPY --from=builder /install /usr/local

COPY app.py docker_registry.py ./
COPY templates/ templates/
COPY static/    static/

RUN mkdir -p /tmp/docker_dl && chown appuser /tmp/docker_dl

USER appuser

EXPOSE 5000

CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "4", \
     "--timeout", "3600", \
     "--worker-class", "sync", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
