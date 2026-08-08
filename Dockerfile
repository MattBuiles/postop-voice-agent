# Imagen de la aplicacion. El modelo de lenguaje vive en el servicio `ollama`
# del docker-compose, no aqui: separar los dos permite que la descarga de pesos
# corra en paralelo con el arranque de la app y no serialice los 15 minutos que
# cronometra la compuerta G2.

FROM python:3.11-slim

# ffmpeg lo necesita faster-whisper para decodificar audio.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Las dependencias van en una capa propia: cambiar codigo no reinstala todo.
COPY pyproject.toml ./
RUN uv venv /opt/venv && VIRTUAL_ENV=/opt/venv uv pip install --no-cache -r pyproject.toml

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# La voz de Piper (63 MB) se descarga en la construccion, no se versiona.
# Si se copiara desde el repositorio, un clon limpio no la tendria -- models/
# esta en .gitignore -- y `docker compose up` fallaria en la maquina del jurado,
# que es justo el escenario de la compuerta G2.
RUN mkdir -p models/piper && \
    curl -fsSL -o models/piper/es_MX-claude-high.onnx \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/claude/high/es_MX-claude-high.onnx && \
    curl -fsSL -o models/piper/es_MX-claude-high.onnx.json \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/claude/high/es_MX-claude-high.onnx.json

COPY src/ ./src/
COPY web/ ./web/
COPY scripts/ ./scripts/
COPY eval/ ./eval/
COPY data/ ./data/

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --start-period=90s --retries=10 \
    CMD curl -fs http://localhost:8080/api/salud || exit 1

CMD ["uvicorn", "postop.main:app", "--host", "0.0.0.0", "--port", "8080"]
