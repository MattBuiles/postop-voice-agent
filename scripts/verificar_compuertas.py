"""Comprueba las cinco compuertas eliminatorias del reto contra el sistema vivo.

    make verify        (con la aplicacion corriendo en :8080)

No sustituye a la verificacion del jurado, pero convierte "creo que pasa las
compuertas" en un comando con salida binaria. Se corre antes de cada entrega.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import httpx

BASE = "http://localhost:8080"

# PDF minimo valido, generado al vuelo: sirve para probar el ciclo completo de
# alta y baja de conocimiento con material que el agente no ha visto nunca,
# que es exactamente como se verifica G5.
PDF_PRUEBA = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 260>>stream
BT /F1 11 Tf 40 740 Td (PROTOCOLO DE PRUEBA ZUMBIDO POSTOPERATORIO) Tj
0 -20 Td (El zumbido de oido tras la cirugia se maneja con reposo auditivo) Tj
0 -20 Td (durante 72 horas y control ambulatorio a los siete dias. Si el) Tj
0 -20 Td (zumbido persiste mas de una semana se solicita audiometria.) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
"""

PREGUNTA = "¿Qué se hace si el zumbido de oído persiste más de una semana?"


def resultado(ok: bool, compuerta: str, detalle: str) -> bool:
    print(f"  [{'PASA' if ok else 'FALLA'}] {compuerta}: {detalle}")
    return ok


def main() -> int:
    print("Verificando las compuertas eliminatorias contra", BASE)
    cliente = httpx.Client(base_url=BASE, timeout=180)
    aprobadas: list[bool] = []

    # --- G3: modelo declarado dentro de la lista permitida ---
    try:
        salud = cliente.get("/api/salud").json()
    except httpx.HTTPError as exc:
        print(f"  No se pudo contactar la aplicación: {exc}")
        print("  Levántala con `make run` y vuelve a intentar.")
        return 1

    from postop.config import MODELOS_PERMITIDOS

    modelo = salud["modelo_declarado"]
    aprobadas.append(resultado(
        modelo in MODELOS_PERMITIDOS, "G3 modelo permitido",
        f"{modelo} {'está' if modelo in MODELOS_PERMITIDOS else 'NO está'} en la lista del reto",
    ))

    # --- G4: la voz funciona de punta a punta ---
    # Se comprueba que exista sintetizador y que el LLM responda; el intercambio
    # hablado real lo hace el jurado con su microfono.
    aprobadas.append(resultado(
        bool(salud["tts"]) and salud["llm_disponible"], "G4 voz en tiempo real",
        f"TTS {'cargado' if salud['tts'] else 'ausente'}, "
        f"LLM {'en línea' if salud['llm_disponible'] else 'caído'}",
    ))

    # --- G5: conocimiento vivo, con material nuevo ---
    antes = cliente.post("/api/consultar", json={"pregunta": PREGUNTA}).json()
    sabia_antes = antes["respuesta"]["anclada"]

    subida = cliente.post(
        "/api/documentos",
        files={"archivo": ("protocolo_zumbido_prueba.pdf", io.BytesIO(PDF_PRUEBA), "application/pdf")},
    ).json()
    doc_id = subida.get("doc_id")
    time.sleep(1)

    durante = cliente.post("/api/consultar", json={"pregunta": PREGUNTA}).json()
    aprendio = durante["respuesta"]["anclada"]

    recibo = cliente.delete(f"/api/documentos/{doc_id}").json() if doc_id else {}
    time.sleep(1)

    despues = cliente.post("/api/consultar", json={"pregunta": PREGUNTA}).json()
    olvido = not despues["respuesta"]["anclada"]

    aprobadas.append(resultado(
        (not sabia_antes) and aprendio and olvido, "G5 conocimiento vivo",
        f"antes={'sabía' if sabia_antes else 'no sabía'} · "
        f"tras subir={'aprendió' if aprendio else 'NO aprendió'} · "
        f"tras borrar={'olvidó' if olvido else 'NO olvidó'} "
        f"({recibo.get('chunks_eliminados', 0)} fragmentos eliminados)",
    ))

    # --- G2: el indice viaja pre-construido ---
    aprobadas.append(resultado(
        salud["chunks"] > 1000, "G2 índice pre-construido",
        f"{salud['chunks']} fragmentos disponibles sin reindexar",
    ))

    # --- G1: los entregables existen en el repositorio ---
    raiz = Path(__file__).resolve().parents[1]
    faltan = [
        nombre for nombre in ("README.md", "LICENSE", "docs/ARQUITECTURA.md", ".env.example")
        if not (raiz / nombre).exists()
    ]
    aprobadas.append(resultado(
        not faltan, "G1 entregables presentes",
        "todos" if not faltan else f"faltan: {', '.join(faltan)}",
    ))

    print(f"\n{sum(aprobadas)}/{len(aprobadas)} compuertas aprobadas")
    return 0 if all(aprobadas) else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    raise SystemExit(main())
