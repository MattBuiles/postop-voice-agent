"""Compara los tamanos de Whisper por latencia y por lo que de verdad importa.

    .venv/bin/python eval/run_asr_bench.py

Con el modelo de lenguaje en 3B, la transcripcion pasa a ser el mayor coste del
turno (1882 ms de 2597 medidos en una llamada real), asi que la eleccion de
tamano deja de ser un detalle.

La metrica principal NO es la tasa de error de palabra. Es **si el slot clinico
sale correcto pese a los errores de transcripcion**: al sistema le da igual que
Whisper escriba "no me la he tomado" o "no me la e tomado" mientras el triaje
acabe en el mismo sitio. Se reporta el WER igualmente, como referencia.

El audio de prueba se sintetiza con la voz colombiana a partir de enunciados
reales del dataset. Es audio limpio, sin ruido de fondo ni microfono malo, asi
que los aciertos absolutos son optimistas; lo que se busca aqui es el **orden
relativo** entre tamanos y su coste en milisegundos.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

AUDIO = RAIZ / "eval" / "audio_bench"
VOZ = "es-CO-SalomeNeural"

# Enunciados reales del dataset del reto, con el valor que el sistema debe
# extraer de cada uno. Se eligieron los que mas cuestan: el minimizador que
# desmiente su propia fiebre, el paciente que da un rango, el que evade.
CASOS: list[dict] = [
    {"texto": "Me tomé la temperatura y salió como en 38, pero eso debe ser del calor de acá no más, doctora",
     "slot": "fiebre_c", "esperado": 38.0},
    {"texto": "La verdad el dolor ha sido más bien un 1, apenas se nota, casi nada",
     "slot": "dolor_nrs", "esperado": 1.0},
    {"texto": "Ay, no, más o menos no más, un dolorcito ahí normal de la operación. Si acaso un 6",
     "slot": "dolor_nrs", "esperado": 6.0},
    {"texto": "No, fiebre no he sentido, me tomé la temperatura y estaba normalita, treinta y seis y algo",
     "slot": "fiebre_c", "esperado": 36.5},
    {"texto": "Se ve un poquito rojita ahí en el borde, pero nada de esas cosas de pus ni nada raro",
     "slot": "herida", "esperado": "eritema_leve"},
    {"texto": "La he visto tranquila, normalita, sin nada raro, ni rojo ni hinchada ni con secreción",
     "slot": "herida", "esperado": "normal"},
    {"texto": "Pues como poquito, se me han quitado las ganas, casi no me pasa nada",
     "slot": "apetito", "esperado": "muy_disminuido"},
    {"texto": "Un poco bajo pero ahí vamos, un poquito menos de lo normal",
     "slot": "apetito", "esperado": "levemente_disminuido"},
    {"texto": "Me muevo despacito, como es de esperarse por la cirugía, pero camino",
     "slot": "movilidad", "esperado": "limitada_esperada"},
    {"texto": "Casi no pego el ojo en toda la noche, doctor",
     "slot": "sueno", "esperado": "muy_alterado"},
]

# Por defecto NO se incluye `medium`. En la maquina objetivo del reto (8-16 GB)
# cargarlo junto al modelo de lenguaje agota la memoria: en una ejecucion real
# esto tumbo el WSL entero y obligo a reiniciar. Para probarlo hay que pedirlo a
# mano y con la aplicacion parada.
TAMANOS = ["tiny", "base", "small"]

# RAM aproximada que necesita cada tamano en int8, con holgura para el proceso.
RAM_NECESARIA_MB = {"tiny": 400, "base": 600, "small": 1200, "medium": 2600, "large-v3": 4200}
MARGEN_SEGURIDAD_MB = 1500


def ram_disponible_mb() -> int:
    """Memoria realmente disponible, no la 'libre': Linux cuenta como usada la
    cache de disco, que si se puede reclamar."""
    for linea in Path("/proc/meminfo").read_text().splitlines():
        if linea.startswith("MemAvailable:"):
            return int(linea.split()[1]) // 1024
    return 0


def hay_memoria_para(tamano: str) -> tuple[bool, str]:
    disponible = ram_disponible_mb()
    necesaria = RAM_NECESARIA_MB.get(tamano, 2000) + MARGEN_SEGURIDAD_MB
    return disponible >= necesaria, f"{disponible} MB disponibles, hacen falta ~{necesaria} MB"


async def preparar_audio() -> None:
    """Sintetiza una vez el audio de prueba y lo deja en disco."""
    import edge_tts

    AUDIO.mkdir(parents=True, exist_ok=True)
    for i, caso in enumerate(CASOS):
        destino = AUDIO / f"caso_{i:02d}.mp3"
        if destino.exists():
            continue
        trozos = []
        async for evento in edge_tts.Communicate(caso["texto"], VOZ).stream():
            if evento["type"] == "audio":
                trozos.append(evento["data"])
        destino.write_bytes(b"".join(trozos))
        print(f"  sintetizado {destino.name}")


def cargar_pcm(ruta: Path):
    """MP3 -> float32 mono 16 kHz, el formato que espera Whisper."""
    import subprocess

    import numpy as np

    salida = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(ruta),
         "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(salida, dtype=np.int16).astype(np.float32) / 32768.0


def wer(referencia: str, hipotesis: str) -> float:
    """Tasa de error de palabra, con distancia de edicion sobre palabras."""
    import re

    def normalizar(texto: str) -> list[str]:
        import unicodedata

        plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
        return re.findall(r"[a-z0-9]+", plano)

    ref, hip = normalizar(referencia), normalizar(hipotesis)
    if not ref:
        return 0.0
    d = [[0] * (len(hip) + 1) for _ in range(len(ref) + 1)]
    for i in range(len(ref) + 1):
        d[i][0] = i
    for j in range(len(hip) + 1):
        d[0][j] = j
    for i in range(1, len(ref) + 1):
        for j in range(1, len(hip) + 1):
            coste = 0 if ref[i - 1] == hip[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + coste)
    return d[len(ref)][len(hip)] / len(ref)


def transcribir_todo(tamano: str, audios: list) -> dict:
    """Solo transcripcion: sin LLM de por medio.

    Medir ambas cosas en el mismo bucle contaminaba el resultado -- el modelo de
    lenguaje y el reconocedor competian por memoria y Ollama devolvia 500. Aqui
    se mide la transcripcion aislada y la calidad de los slots se evalua despues,
    sobre el texto ya guardado.
    """
    from postop.asr.transcribir import Transcriptor

    suficiente, detalle = hay_memoria_para(tamano)
    if not suficiente:
        raise MemoryError(
            f"memoria insuficiente para '{tamano}': {detalle}. "
            f"Para el modelo de lenguaje y la aplicacion parados, o usa un tamano menor."
        )

    print(f"\n  cargando {tamano} ({detalle}) ...", flush=True)
    inicio_carga = time.perf_counter()
    transcriptor = Transcriptor(tamano, cache_dir=RAIZ / "models" / "whisper")
    ms_carga = (time.perf_counter() - inicio_carga) * 1000

    # Una transcripcion de calentamiento que no se contabiliza: la primera
    # siempre paga la inicializacion de los grafos de ONNX.
    transcriptor.transcribir(audios[0])

    latencias, errores, textos = [], [], []
    for caso, audio in zip(CASOS, audios, strict=True):
        inicio = time.perf_counter()
        transcripcion = transcriptor.transcribir(audio)
        latencias.append((time.perf_counter() - inicio) * 1000)
        errores.append(wer(caso["texto"], transcripcion.texto))
        textos.append(transcripcion.texto)

    del transcriptor
    return {
        "tamano": tamano,
        "ms_carga": round(ms_carga),
        "ms_mediana": round(statistics.median(latencias)),
        "ms_p95": round(sorted(latencias)[max(0, int(len(latencias) * 0.95) - 1)]),
        "wer": round(statistics.mean(errores), 4),
        "textos": textos,
    }


async def evaluar_slots(resultado: dict, cliente) -> dict:
    """Sobre el texto ya transcrito: ¿sale el slot clinico correcto?"""
    from postop.llm.extract import extraer

    aciertos, fallos = 0, []
    for caso, texto in zip(CASOS, resultado["textos"], strict=True):
        extraccion = await extraer(cliente, texto, caso["slot"])
        obtenido = getattr(extraccion.slots, caso["slot"])
        if obtenido == caso["esperado"]:
            aciertos += 1
        else:
            fallos.append({"slot": caso["slot"], "esperado": caso["esperado"],
                           "obtenido": obtenido, "oyo": texto})
    resultado["slots_ok"] = aciertos
    resultado["slots_total"] = len(CASOS)
    resultado["fallos"] = fallos
    return resultado


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tamanos", nargs="*", default=TAMANOS)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    from postop.config import config
    from postop.llm.client import ClienteLLM

    print("Preparando audio de prueba (voz colombiana, enunciados del dataset)...")
    await preparar_audio()
    audios = [cargar_pcm(AUDIO / f"caso_{i:02d}.mp3") for i in range(len(CASOS))]

    # Fase 1: transcribir con cada tamano, sin el modelo de lenguaje cargado.
    resultados = []
    for tamano in args.tamanos:
        try:
            resultados.append(transcribir_todo(tamano, audios))
        except Exception as exc:  # noqa: BLE001
            print(f"  {tamano}: FALLO {type(exc).__name__}: {exc}")

    # Fase 2: ya sin reconocedores en memoria, evaluar los slots.
    cliente = ClienteLLM(config.llm_base_url, config.llm_model, timeout=300)
    await cliente.precalentar([config.llm_model])
    for resultado in resultados:
        await evaluar_slots(resultado, cliente)
        for fallo in resultado["fallos"]:
            print(f"  [{resultado['tamano']}] {fallo['slot']}: esperado {fallo['esperado']} "
                  f"obtenido {fallo['obtenido']}")
            print(f"        oyó: {fallo['oyo'][:88]!r}")
    await cliente.cerrar()

    print(f"\n{'tamaño':8s} {'carga':>8s} {'mediana':>9s} {'p95':>8s} {'WER':>7s} {'slots':>8s}")
    print("-" * 52)
    for r in sorted(resultados, key=lambda x: x["ms_mediana"]):
        print(f"{r['tamano']:8s} {r['ms_carga']:7d}ms {r['ms_mediana']:8d}ms "
              f"{r['ms_p95']:7d}ms {r['wer']:6.1%} {r['slots_ok']:4d}/{r['slots_total']}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(resultados, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
