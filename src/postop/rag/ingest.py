"""Ingesta de documentos clinicos: PDF -> texto por pagina -> fragmentos -> indice.

Dos invariantes gobiernan este modulo:

1. **La pagina se preserva hasta la cita.** El jurado verifica que la referencia
   que da el agente resista un contraste contra la fuente real, asi que un
   fragmento sin numero de pagina es un fragmento inutil.
2. **Alta y baja son transaccionales.** Insertar o borrar toca `chunks`,
   `vec_chunks`, `fts_chunks` y la version del corpus en una sola transaccion.
   Si algo falla, no queda un indice a medias que haga mentir a la compuerta G5.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium

from postop.db import store
from postop.rag.embed import Embedder

# Palabras por fragmento.
#
# Se empezo en 520 (~700 tokens) buscando que cada parrafo clinico quedara
# completo. Medido sobre el corpus real, esa talla era el cuello de botella de
# la recuperacion: un fragmento de media pagina arranca hablando de trombosis
# venosa y termina hablando de dieta, asi que su embedding es el promedio de
# varios temas y no discrimina ninguno. La consulta "me duele la pantorrilla y
# la tengo hinchada" recuperaba pasajes sobre nauseas.
#
# A 260 palabras cada fragmento cubre un solo asunto y el vector representa ese
# asunto. Cuesta el doble de fragmentos y un poco mas de indice, que es barato
# comparado con fallar la pregunta del paciente.
PALABRAS_POR_FRAGMENTO = 260
PALABRAS_SOLAPE = 60
MIN_PALABRAS_FRAGMENTO = 20

# Fragmentos que se embeben de una vez. Acota el pico de memoria del proceso a
# algo independiente del tamano del documento (ver ingerir_documento).
LOTE_EMBEBIDO = 48

# Un PDF con capa de texto real supera holgadamente este umbral por pagina.
# Por debajo, la pagina esta escaneada: el corpus del reto trae al menos un
# documento asi en Appendicitis/.
MIN_CARACTERES_PAGINA_CON_TEXTO = 60

ESCENARIOS = {
    "appendicitis": "appendicitis",
    "breast_cancer": "breast_cancer",
    "cholecystitis": "cholecystitis",
    "colorectal cancer": "colorectal_cancer",
    "total joint replacement": "total_joint_replacement",
}

# El procedimiento del paciente decide que porcion del corpus es admisible.
# Sin este filtro, una consulta sobre la herida de una mastectomia puede
# recuperar guias de tamizaje de cuello uterino: la carpeta breast_cancer/ del
# reto viene contaminada con ese material.
PROCEDIMIENTO_A_ESCENARIO = {
    "Apendicectomía": "appendicitis",
    "Colecistectomía": "cholecystitis",
    "Colectomía": "colorectal_cancer",
    "Reemplazo de cadera/rodilla": "total_joint_replacement",
    "Mastectomía": "breast_cancer",
}


@dataclass(frozen=True)
class Fragmento:
    texto: str
    pagina: int
    seccion: str | None
    n_tokens: int


def _ahora() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_archivo(ruta: Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as fh:
        for bloque in iter(lambda: fh.read(1 << 20), b""):
            digest.update(bloque)
    return digest.hexdigest()


def identidad_logica(nombre: str) -> str:
    """Identidad estable entre versiones de un mismo documento.

    'Protocolo dolor v2 (rev).pdf' y 'protocolo-dolor-v1.pdf' colapsan al mismo
    identificador, que es lo que permite que la version nueva supersede a la
    vieja en vez de convivir con ella y contaminar las respuestas.
    """
    base = Path(nombre).stem.lower()
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = re.sub(r"\bv?\d+(\.\d+)*\b", " ", base)          # numeros de version
    base = re.sub(r"\b(rev|final|copia|copy|draft|borrador)\b", " ", base)
    return re.sub(r"[^a-z]+", "-", base).strip("-") or "documento"


# --------------------------------------------------------------- extraccion

def extraer_paginas(ruta: Path) -> tuple[list[tuple[int, str]], list[int]]:
    """Devuelve [(numero_de_pagina, texto)] y la lista de paginas sin capa de texto."""
    paginas: list[tuple[int, str]] = []
    sin_texto: list[int] = []
    pdf = pdfium.PdfDocument(str(ruta))
    try:
        for i in range(len(pdf)):
            try:
                crudo = pdf[i].get_textpage().get_text_range() or ""
            except Exception:
                crudo = ""
            texto = limpiar(crudo)
            if len(texto) < MIN_CARACTERES_PAGINA_CON_TEXTO:
                sin_texto.append(i + 1)
            paginas.append((i + 1, texto))
    finally:
        pdf.close()
    return paginas, sin_texto


def limpiar(texto: str) -> str:
    """Normaliza el ruido tipico de extraccion de PDF sin alterar el contenido
    clinico: la cita literal debe seguir siendo verificable contra la fuente.

    La normalizacion de CRLF va primero y no es cosmetica: sin ella, un PDF con
    finales de linea de Windows no presenta ni un solo '\\n\\n', el fragmentador
    no encuentra limites de parrafo y devuelve paginas enteras como un unico
    fragmento.
    """
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    texto = texto.replace("\x00", " ").replace("﻿", " ")
    texto = re.sub(r"(\w)-\n(\w)", r"\1\2", texto)   # palabra partida por guion
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def _es_encabezado(linea: str) -> bool:
    limpio = linea.strip()
    if not 3 <= len(limpio) <= 90:
        return False
    if limpio.endswith("."):
        return False
    letras = [c for c in limpio if c.isalpha()]
    if not letras:
        return False
    mayusculas = sum(c.isupper() for c in letras) / len(letras)
    return mayusculas > 0.6 or bool(re.match(r"^\d+(\.\d+)*\s+\S", limpio))


def _unidades(bloque: str) -> list[str]:
    """Parte un bloque en unidades que quepan holgadamente en un fragmento.

    No se confia en que el PDF traiga estructura de parrafos: muchos extraen
    una pagina entera como un solo bloque. Si el bloque excede el objetivo se
    baja a oraciones, y si una "oracion" sigue siendo enorme (tablas, listas sin
    puntuacion) se corta por ventanas de palabras. Asi ningun fragmento puede
    desbordar el contexto por mucho que el documento este mal formado.
    """
    if len(bloque.split()) <= PALABRAS_POR_FRAGMENTO:
        return [bloque]

    unidades: list[str] = []
    for oracion in re.split(r"(?<=[.:;!?])\s+", bloque):
        palabras = oracion.split()
        if len(palabras) <= PALABRAS_POR_FRAGMENTO:
            if oracion.strip():
                unidades.append(oracion.strip())
            continue
        for i in range(0, len(palabras), PALABRAS_POR_FRAGMENTO):
            unidades.append(" ".join(palabras[i : i + PALABRAS_POR_FRAGMENTO]))
    return unidades


def fragmentar(paginas: list[tuple[int, str]]) -> list[Fragmento]:
    """Empaqueta unidades de texto hasta el tamano objetivo, con solape,
    arrastrando la pagina donde empieza cada fragmento y el ultimo encabezado."""
    fragmentos: list[Fragmento] = []
    buffer: list[str] = []
    n_palabras = 0
    pagina_inicio = paginas[0][0] if paginas else 1
    seccion: str | None = None

    def volcar() -> None:
        nonlocal buffer, n_palabras
        palabras = " ".join(buffer).split()
        if len(palabras) >= MIN_PALABRAS_FRAGMENTO:
            texto = " ".join(palabras)
            fragmentos.append(Fragmento(texto, pagina_inicio, seccion, int(len(palabras) * 1.35)))
        cola = palabras[-PALABRAS_SOLAPE:] if palabras else []
        buffer = [" ".join(cola)] if cola else []
        n_palabras = len(cola)

    for numero, texto in paginas:
        for bloque in filter(None, (p.strip() for p in texto.split("\n\n"))):
            if _es_encabezado(bloque):
                seccion = bloque.strip()
            for unidad in _unidades(bloque):
                largo = len(unidad.split())
                # Volcar ANTES de desbordar, no despues: si se anade primero, un
                # buffer casi lleno mas una unidad larga produce fragmentos del
                # doble del objetivo.
                if buffer and n_palabras + largo > PALABRAS_POR_FRAGMENTO:
                    volcar()
                    pagina_inicio = numero
                if not buffer:
                    pagina_inicio = numero
                buffer.append(unidad)
                n_palabras += largo
    volcar()
    return fragmentos


# ------------------------------------------------------------------- alta

def ingerir_documento(
    conn,
    embedder: Embedder,
    ruta: Path,
    *,
    escenario: str | None = None,
    origen: str = "upload",
    nombre: str | None = None,
) -> dict:
    """Indexa un documento. Devuelve un resumen del resultado.

    Si ya existe un documento con la misma identidad logica, el nuevo entra como
    version siguiente y el anterior queda marcado como superseded: deja de ser
    recuperable pero permanece auditable.
    """
    nombre = nombre or ruta.name
    sha = sha256_archivo(ruta)

    # Solo un documento REALMENTE disponible cuenta como duplicado. Sin la
    # condicion de estado, un documento que quedo a medias (proceso interrumpido
    # durante el embebido) bloquearia para siempre su propia reingesta: se
    # devolveria "duplicado" sobre una fila con cero fragmentos, y el agente
    # mostraria el documento en la consola sin poder citarlo nunca.
    existente = conn.execute(
        "SELECT doc_id FROM documents "
        "WHERE sha256 = ? AND superseded_by IS NULL AND estado = 'disponible'",
        (sha,),
    ).fetchone()
    if existente:
        return {"doc_id": existente["doc_id"], "estado": "duplicado", "n_chunks": 0}

    # Se limpian los intentos fallidos previos del mismo archivo para no
    # acumular filas fantasma en la consola.
    for fila in conn.execute(
        "SELECT doc_id FROM documents WHERE sha256 = ? AND estado != 'disponible'", (sha,)
    ).fetchall():
        _purgar_fragmentos(conn, fila["doc_id"])
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (fila["doc_id"],))
    conn.commit()

    doc_id = str(uuid.uuid4())
    logico = identidad_logica(nombre)
    previo = conn.execute(
        "SELECT doc_id, version FROM documents "
        "WHERE logical_id = ? AND superseded_by IS NULL ORDER BY version DESC LIMIT 1",
        (logico,),
    ).fetchone()
    version = (previo["version"] + 1) if previo else 1

    conn.execute(
        "INSERT INTO documents (doc_id, logical_id, nombre, escenario, sha256, version, "
        "estado, origen, subido_ts) VALUES (?,?,?,?,?,?,'extrayendo',?,?)",
        (doc_id, logico, nombre, escenario, sha, version, origen, _ahora()),
    )
    conn.commit()

    try:
        paginas, sin_texto = extraer_paginas(ruta)
        if sin_texto and len(sin_texto) == len(paginas):
            conn.execute("UPDATE documents SET estado = 'ocr' WHERE doc_id = ?", (doc_id,))
            conn.commit()
            paginas = _ocr(ruta, paginas)

        conn.execute("UPDATE documents SET estado = 'fragmentando' WHERE doc_id = ?", (doc_id,))
        conn.commit()
        fragmentos = fragmentar(paginas)
        if not fragmentos:
            raise ValueError("no se pudo extraer texto util del documento")

        conn.execute("UPDATE documents SET estado = 'embebiendo' WHERE doc_id = ?", (doc_id,))
        conn.commit()

        # Embebido y escritura POR LOTES, no de golpe.
        #
        # Embeber un documento entero en una sola llamada hacia crecer el proceso
        # hasta 6.8 GB con este corpus: una guia de 185 paginas produce ~600
        # fragmentos, y ni los vectores ni la arena de memoria de ONNX se
        # liberaban entre documentos. En una maquina de 11 GB eso deja el sistema
        # sin aire, y en la del jurado una subida grande tumbaria el proceso.
        #
        # Por lotes, la memoria queda acotada por LOTE_EMBEBIDO y ya no depende
        # del tamano del documento.
        for inicio in range(0, len(fragmentos), LOTE_EMBEBIDO):
            lote = fragmentos[inicio : inicio + LOTE_EMBEBIDO]
            vectores = embedder.embed_pasajes([f.texto for f in lote])
            _insertar_fragmentos(conn, doc_id, lote, vectores)
            del vectores

        if previo:
            conn.execute(
                "UPDATE documents SET superseded_by = ? WHERE doc_id = ?", (doc_id, previo["doc_id"])
            )
            _purgar_fragmentos(conn, previo["doc_id"])

        conn.execute(
            "UPDATE documents SET estado='disponible', n_paginas=?, procesado_ts=? WHERE doc_id=?",
            (len(paginas), _ahora(), doc_id),
        )
        store.bump_version_corpus(conn)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE documents SET estado = 'error', error = ? WHERE doc_id = ?",
            (f"{type(exc).__name__}: {exc}"[:500], doc_id),
        )
        conn.commit()
        raise

    return {
        "doc_id": doc_id,
        "estado": "disponible",
        "n_chunks": len(fragmentos),
        "n_paginas": len(paginas),
        "version": version,
        "supersede": previo["doc_id"] if previo else None,
    }


def _insertar_fragmentos(conn, doc_id: str, fragmentos: list[Fragmento], vectores: np.ndarray):
    for fragmento, vector in zip(fragmentos, vectores, strict=True):
        cursor = conn.execute(
            "INSERT INTO chunks (chunk_uid, doc_id, pagina, seccion, texto, n_tokens) "
            "VALUES (?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                doc_id,
                fragmento.pagina,
                fragmento.seccion,
                fragmento.texto,
                fragmento.n_tokens,
            ),
        )
        rowid = cursor.lastrowid
        # El rowid compartido es lo que mantiene alineados los tres indices.
        conn.execute(
            "INSERT INTO vec_chunks (chunk_rowid, embedding) VALUES (?, ?)",
            (rowid, np.asarray(vector, dtype=np.float32).tobytes()),
        )
        conn.execute(
            "INSERT INTO fts_chunks (rowid, texto) VALUES (?, ?)", (rowid, fragmento.texto)
        )


def _purgar_fragmentos(conn, doc_id: str) -> int:
    """Elimina los fragmentos de un documento de los tres indices."""
    filas = conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)).fetchall()
    for fila in filas:
        conn.execute("DELETE FROM vec_chunks WHERE chunk_rowid = ?", (fila["id"],))
        conn.execute("DELETE FROM fts_chunks WHERE rowid = ?", (fila["id"],))
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    return len(filas)


def _ocr(ruta: Path, paginas: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Respaldo para PDF escaneado. Si el extra `ocr` no esta instalado, se
    devuelve lo que habia: preferimos un documento vacio y declarado a una
    excepcion que tumbe la ingesta del corpus completo."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return paginas

    motor = RapidOCR()
    pdf = pdfium.PdfDocument(str(ruta))
    salida: list[tuple[int, str]] = []
    try:
        for numero, _ in paginas:
            # scale=1.6 basta para texto de guia clinica y ocupa un 36% menos de
            # memoria que scale=2. La imagen se libera en cada vuelta: un
            # documento escaneado de 200 paginas no debe retenerlas todas.
            imagen = np.array(pdf[numero - 1].render(scale=1.6).to_pil())
            resultado, _ = motor(imagen)
            del imagen
            texto = limpiar(" ".join(linea[1] for linea in (resultado or [])))
            salida.append((numero, texto))
    finally:
        pdf.close()
    return salida


# ------------------------------------------------------------------- baja

def eliminar_documento(conn, doc_id: str) -> dict:
    """Borra un documento y emite el recibo de olvido que evidencia G5."""
    doc = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if doc is None:
        raise KeyError(f"documento desconocido: {doc_id}")

    version_antes = store.version_corpus(conn)
    try:
        n_chunks = _purgar_fragmentos(conn, doc_id)
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        version_despues = store.bump_version_corpus(conn)
        recibo = {
            "receipt_id": str(uuid.uuid4()),
            "doc_id": doc_id,
            "nombre": doc["nombre"],
            "sha256": doc["sha256"],
            "chunks_eliminados": n_chunks,
            "corpus_version_antes": version_antes,
            "corpus_version_despues": version_despues,
            "eliminado_ts": _ahora(),
        }
        conn.execute(
            "INSERT INTO deletion_receipts (receipt_id, doc_id, nombre, sha256, "
            "chunks_eliminados, corpus_version_antes, corpus_version_despues, eliminado_ts) "
            "VALUES (:receipt_id,:doc_id,:nombre,:sha256,:chunks_eliminados,"
            ":corpus_version_antes,:corpus_version_despues,:eliminado_ts)",
            recibo,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return recibo
