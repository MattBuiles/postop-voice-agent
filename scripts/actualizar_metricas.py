"""Regenera la tabla de metricas del README a partir de los logs.

    make metrics

La rubrica contrasta las metricas del README contra los logs de la sesion y
advierte que reportar numeros que no se sostienen penaliza mas que no
reportarlos. La forma segura de cumplir eso no es tener cuidado al escribir el
README: es que nadie lo escriba a mano.

Este script sustituye el bloque entre los marcadores
<!-- METRICAS:INICIO --> y <!-- METRICAS:FIN --> con lo que digan los JSONL, y
estampa el commit y el numero de sesiones agregadas.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from postop.config import config  # noqa: E402
from postop.obs import traza as obs  # noqa: E402

INICIO = "<!-- METRICAS:INICIO -->"
FIN = "<!-- METRICAS:FIN -->"


def commit_actual() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=RAIZ, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "sin-commit"


def construir_tabla(metricas: dict, costos: dict[str, float]) -> str:
    latencia = metricas["latencia_ms"]
    lineas = [
        INICIO,
        "",
        f"_Generado por `make metrics` desde {metricas['n_llamadas']} llamada(s) y "
        f"{metricas['n_turnos']} turno(s) registrados en `logs/`. Commit `{commit_actual()}`._",
        "",
        "| Métrica | Valor |",
        "|---|---|",
        f"| Latencia de respuesta P50 | **{latencia['p50']:.0f} ms** |",
        f"| Latencia de respuesta P95 | **{latencia['p95']:.0f} ms** |",
        f"| Turnos medidos | {latencia['n']} |",
        f"| Tokens de entrada por turno (medio) | {metricas['tokens_por_turno']['entrada_medio']:.0f} |",
        f"| Tokens de salida por turno (medio) | {metricas['tokens_por_turno']['salida_medio']:.0f} |",
        f"| Tokens de entrada por llamada (medio) | {metricas['tokens_por_llamada']['entrada_medio']:.0f} |",
        f"| Tokens de salida por llamada (medio) | {metricas['tokens_por_llamada']['salida_medio']:.0f} |",
        f"| Invocaciones al modelo por turno | {metricas['invocaciones_modelo_por_turno']:.2f} |",
        f"| Consultas al RAG por llamada | {metricas['consultas_rag_por_llamada']:.2f} |",
        "",
    ]

    if metricas["latencia_por_etapa_ms"]:
        lineas += ["**Desglose de latencia por etapa** (ms):", "",
                   "| Etapa | P50 | P95 |", "|---|---|---|"]
        for etapa, valores in metricas["latencia_por_etapa_ms"].items():
            lineas.append(f"| {etapa} | {valores['p50']:.0f} | {valores['p95']:.0f} |")
        lineas.append("")

    lineas += [
        "**Costo estimado por llamada.** La solución corre local, así que el costo medido "
        "es cero. Se extrapola el mismo consumo de tokens a precios de API de producción "
        "para hacer la cifra comparable:",
        "",
        "| Escenario | USD por llamada |",
        "|---|---|",
    ]
    for nombre, valor in costos.items():
        lineas.append(f"| {nombre} | ${valor:.6f} |")
    lineas += [
        "",
        "_Fórmula: `(tokens_entrada / 1e6 × precio_entrada) + (tokens_salida / 1e6 × "
        "precio_salida)`, con los tokens medidos por Ollama (`prompt_eval_count` y "
        "`eval_count`), no estimados._",
        "",
        FIN,
    ]
    return "\n".join(lineas)


def main() -> int:
    metricas = obs.agregar(config.logs_absoluta)
    if metricas["n_turnos"] == 0:
        print("No hay turnos registrados en logs/. Haz al menos una llamada antes.")
        return 1

    tabla = construir_tabla(metricas, obs.costo_por_llamada(metricas))
    readme = RAIZ / "README.md"
    texto = readme.read_text(encoding="utf-8")

    if INICIO in texto and FIN in texto:
        antes = texto.split(INICIO)[0]
        despues = texto.split(FIN)[1]
        readme.write_text(antes + tabla + despues, encoding="utf-8")
    else:
        readme.write_text(texto.rstrip() + "\n\n## Métricas\n\n" + tabla + "\n", encoding="utf-8")

    print(f"README actualizado: P50={metricas['latencia_ms']['p50']:.0f} ms, "
          f"P95={metricas['latencia_ms']['p95']:.0f} ms, "
          f"{metricas['n_turnos']} turnos de {metricas['n_llamadas']} llamada(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
