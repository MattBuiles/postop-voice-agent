.PHONY: help install dataset index run up down eval metrics test lint verify

help:
	@echo "make install   instala dependencias (uv)"
	@echo "make dataset   clona el corpus del reto (133 MB)"
	@echo "make index     reconstruye el indice vectorial desde los PDFs (~12 min)"
	@echo "make run       levanta la aplicacion en http://localhost:8080"
	@echo "make up        levanta todo con docker compose"
	@echo "make eval      corre el arnes de evaluacion (triaje + inyeccion)"
	@echo "make metrics   recalcula las metricas del README desde los logs"
	@echo "make verify    comprueba las 5 compuertas del reto"

install:
	uv sync
	@echo "Descargando voz de Piper..."
	@mkdir -p models/piper
	@test -f models/piper/es_MX-claude-high.onnx || curl -sL -o models/piper/es_MX-claude-high.onnx \
		https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/claude/high/es_MX-claude-high.onnx
	@test -f models/piper/es_MX-claude-high.onnx.json || curl -sL -o models/piper/es_MX-claude-high.onnx.json \
		https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/claude/high/es_MX-claude-high.onnx.json

dataset:
	@test -d challenge-data || git clone --depth 1 \
		https://github.com/TechSphere2026/ParticipantArtifacts.git challenge-data

index: dataset
	PYTHONPATH=src .venv/bin/python scripts/ingest_corpus.py --reset

run:
	PYTHONPATH=src .venv/bin/uvicorn postop.main:app --host 0.0.0.0 --port 8080

up:
	docker compose up

down:
	docker compose down

eval:
	PYTHONPATH=src .venv/bin/python eval/run_triage_eval.py --json eval/results/triage.json
	PYTHONPATH=src .venv/bin/python eval/run_injection_eval.py --json eval/results/injection.json

metrics:
	PYTHONPATH=src .venv/bin/python scripts/actualizar_metricas.py

test:
	PYTHONPATH=src .venv/bin/pytest -q

lint:
	.venv/bin/ruff check src eval scripts

verify:
	PYTHONPATH=src .venv/bin/python scripts/verificar_compuertas.py
