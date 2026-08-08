"""Traza estructurada y metricas.

La rubrica exige reportar en el README la latencia P50/P95, el consumo de tokens
por turno y por llamada, las invocaciones al modelo y las consultas al RAG, y
**contrasta lo reportado contra los logs de la sesion**. Reportar numeros que no
se sostienen penaliza mas que no reportarlos.

Por eso los numeros del README no se escriben a mano: `make metrics` los calcula
desde estos JSONL. Si el README y los logs discrepan, es porque alguien edito el
README a mano, que es exactamente lo que se quiere impedir.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


def ahora() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Traza:
    """Escribe un evento por linea. Un archivo por llamada."""

    def __init__(self, directorio: Path) -> None:
        self.directorio = directorio
        directorio.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def escribir(self, call_id: str, evento: str, payload: dict) -> None:
        linea = json.dumps(
            {"ts": ahora(), "call_id": call_id, "evento": evento, **payload},
            ensure_ascii=False,
            default=str,
        )
        with self._lock:
            with (self.directorio / f"session-{call_id}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(linea + "\n")


@dataclass
class Cronometro:
    """Mide las etapas de un turno. El instante cero es el fin del habla del
    paciente, que es exactamente donde la rubrica manda empezar a contar."""

    marcas: dict[str, float] = field(default_factory=dict)
    _inicio: float = 0.0

    def arrancar(self) -> None:
        import time

        self._inicio = time.perf_counter()

    def marcar(self, etapa: str) -> None:
        import time

        self.marcas[etapa] = (time.perf_counter() - self._inicio) * 1000

    @property
    def total_ms(self) -> float:
        return max(self.marcas.values(), default=0.0)

    def to_dict(self) -> dict:
        return {k: round(v, 1) for k, v in self.marcas.items()}


def percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    indice = min(int(len(ordenados) * p), len(ordenados) - 1)
    return ordenados[indice]


def agregar(directorio: Path) -> dict:
    """Recalcula las metricas obligatorias a partir de todos los JSONL."""
    latencias: list[float] = []
    por_etapa: dict[str, list[float]] = {}
    tokens_entrada: list[int] = []
    tokens_salida: list[int] = []
    invocaciones_turno: list[int] = []
    rag_por_llamada: dict[str, int] = {}
    tokens_por_llamada: dict[str, list[int]] = {}
    llamadas: set[str] = set()

    for archivo in sorted(directorio.glob("session-*.jsonl")):
        for linea in archivo.read_text(encoding="utf-8").splitlines():
            if not linea.strip():
                continue
            try:
                evento = json.loads(linea)
            except json.JSONDecodeError:
                continue
            call_id = evento.get("call_id", "?")
            llamadas.add(call_id)

            if evento.get("evento") == "turno_agente":
                # Solo los turnos hablados cuentan para la latencia reportada.
                # La rubrica la define como "desde que el paciente termina de
                # hablar hasta que empieza a sonar el audio del agente", asi que
                # incluir los turnos del camino de texto (que no tienen ni STT ni
                # espera de audio) falsearia la mediana hacia abajo.
                es_voz = evento.get("modo", "voz") == "voz"
                if es_voz and (total := evento.get("latencia_total_ms")) is not None:
                    latencias.append(float(total))
                if es_voz:
                    for etapa, ms in (evento.get("latencias") or {}).items():
                        por_etapa.setdefault(etapa, []).append(float(ms))
                tokens = evento.get("tokens") or {}
                entrada, salida = tokens.get("entrada", 0), tokens.get("salida", 0)
                tokens_entrada.append(entrada)
                tokens_salida.append(salida)
                invocaciones_turno.append(tokens.get("invocaciones", 0))
                acumulado = tokens_por_llamada.setdefault(call_id, [0, 0])
                acumulado[0] += entrada
                acumulado[1] += salida
            elif evento.get("evento") == "rag_consulta":
                rag_por_llamada[call_id] = rag_por_llamada.get(call_id, 0) + 1

    n = max(1, len(tokens_entrada))
    n_llamadas = max(1, len(llamadas))
    return {
        "n_llamadas": len(llamadas),
        "n_turnos": len(tokens_entrada),
        "latencia_ms": {
            "p50": round(percentil(latencias, 0.50), 1),
            "p95": round(percentil(latencias, 0.95), 1),
            "n": len(latencias),
        },
        "latencia_por_etapa_ms": {
            etapa: {"p50": round(percentil(v, 0.50), 1), "p95": round(percentil(v, 0.95), 1)}
            for etapa, v in sorted(por_etapa.items())
        },
        "tokens_por_turno": {
            "entrada_medio": round(sum(tokens_entrada) / n, 1),
            "salida_medio": round(sum(tokens_salida) / n, 1),
        },
        "tokens_por_llamada": {
            "entrada_medio": round(
                sum(v[0] for v in tokens_por_llamada.values()) / n_llamadas, 1
            ),
            "salida_medio": round(
                sum(v[1] for v in tokens_por_llamada.values()) / n_llamadas, 1
            ),
        },
        "invocaciones_modelo_por_turno": round(sum(invocaciones_turno) / n, 2),
        "consultas_rag_por_llamada": round(
            sum(rag_por_llamada.values()) / n_llamadas, 2
        ),
    }


# Precios de referencia para la extrapolacion que exige la rubrica cuando la
# solucion corre local. USD por millon de tokens, consultados el 7-ago-2026.
PRECIOS_REFERENCIA = {
    "local (Llama 3.2 3B en Ollama)": (0.0, 0.0),
    "modelo pequeno de nube (referencia)": (0.10, 0.40),
    "modelo grande de nube (referencia)": (3.00, 15.00),
}


def costo_por_llamada(metricas: dict) -> dict[str, float]:
    entrada = metricas["tokens_por_llamada"]["entrada_medio"]
    salida = metricas["tokens_por_llamada"]["salida_medio"]
    return {
        nombre: round(entrada / 1e6 * p_in + salida / 1e6 * p_out, 6)
        for nombre, (p_in, p_out) in PRECIOS_REFERENCIA.items()
    }
