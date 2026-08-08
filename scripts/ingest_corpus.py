"""Indexa el corpus clinico del reto en el indice local.

    .venv/bin/python scripts/ingest_corpus.py [--limite N] [--reset]

El indice resultante (data/knowledge.db) se versiona en el repositorio: el
jurado debe poder levantar la solucion sin esperar a que se reindexen 107 PDFs,
que es lo que consumiria el presupuesto de 15 minutos de la compuerta G2.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from postop.config import config  # noqa: E402
from postop.db import store  # noqa: E402
from postop.rag import ingest  # noqa: E402
from postop.rag.embed import crear_embedder  # noqa: E402


def _pico_memoria_mb() -> float:
    """Pico de RSS del proceso. Se reporta porque la ingesta ya tuvo una
    regresion de memoria (embebia el documento entero de una vez y llegaba a
    6.8 GB); tenerlo a la vista impide que vuelva a pasar inadvertida."""
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limite", type=int, default=0, help="indexar solo N documentos")
    parser.add_argument("--reset", action="store_true", help="borrar el indice antes de empezar")
    args = parser.parse_args()

    textos = config.dataset_absoluta / "textos"
    if not textos.is_dir():
        print(f"No encuentro el corpus en {textos}. Corre `make dataset` primero.")
        return 1

    if args.reset and config.db_absoluta.exists():
        config.db_absoluta.unlink()
        for sufijo in ("-wal", "-shm"):
            Path(str(config.db_absoluta) + sufijo).unlink(missing_ok=True)

    print(f"Cargando embedder {config.embed_model} ...", flush=True)
    embedder = crear_embedder(config.embed_backend, config.embed_model)
    print(f"  dim={embedder.dim}", flush=True)

    conn = store.conectar(config.db_absoluta)
    store.inicializar(conn, embedder.dim)
    store.verificar_dimension(conn, embedder.dim)

    pdfs = sorted(textos.rglob("*.pdf"))
    if args.limite:
        pdfs = pdfs[: args.limite]

    inicio = time.time()
    total_chunks = ok = fallidos = duplicados = 0

    for i, pdf in enumerate(pdfs, 1):
        carpeta = pdf.parent.name
        escenario = ingest.ESCENARIOS.get(carpeta.lower(), carpeta.lower().replace(" ", "_"))
        try:
            resultado = ingest.ingerir_documento(
                conn, embedder, pdf, escenario=escenario, origen="corpus"
            )
            if resultado["estado"] == "duplicado":
                duplicados += 1
                marca = "dup"
            else:
                ok += 1
                total_chunks += resultado["n_chunks"]
                marca = f"{resultado['n_chunks']:4d} chunks"
            print(f"[{i:3d}/{len(pdfs)}] {marca:14s} {escenario:24s} {pdf.name[:60]}", flush=True)
        except Exception as exc:  # noqa: BLE001 - un PDF roto no debe tumbar el corpus
            fallidos += 1
            print(f"[{i:3d}/{len(pdfs)}] FALLO  {type(exc).__name__}: {exc} :: {pdf.name}", flush=True)

    minutos = (time.time() - inicio) / 60
    print(
        f"\nIndexados {ok} documentos ({total_chunks} chunks), "
        f"{duplicados} duplicados, {fallidos} fallidos en {minutos:.1f} min"
    )
    print(f"Pico de memoria del proceso: {_pico_memoria_mb():.0f} MB")
    print(f"Version del corpus: {store.version_corpus(conn)}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
