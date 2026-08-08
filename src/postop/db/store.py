"""Acceso a SQLite: conexion, esquema y las dos tablas virtuales de busqueda.

El indice denso (sqlite-vec) y el lexico (FTS5) comparten el rowid de `chunks`.
Esa decision es la que permite que borrar un documento sea una sola transaccion
sobre un solo archivo, sin ningun almacen externo que pueda quedar desincronizado.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

ESQUEMA = Path(__file__).with_name("schema.sql")


def conectar(db_path: Path, *, solo_lectura: bool = False) -> sqlite3.Connection:
    """Abre la base con la extension vectorial cargada."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if solo_lectura and db_path.exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(db_path, check_same_thread=False)

    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def inicializar(conn: sqlite3.Connection, dim: int) -> None:
    """Crea el esquema y las tablas virtuales. Idempotente.

    `dim` es la dimension del modelo de embeddings en uso; cambiar de modelo
    exige reindexar, y `verificar_dimension` lo detecta en vez de fallar con un
    error opaco de sqlite-vec.
    """
    conn.executescript(ESQUEMA.read_text(encoding="utf-8"))

    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
        f"  chunk_rowid INTEGER PRIMARY KEY,"
        f"  embedding float[{dim}]"
        f")"
    )
    # FTS5 autonomo (no external-content): asi un DELETE por rowid es directo y
    # entra en la misma transaccion que el borrado vectorial.
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5("
        "  texto,"
        "  tokenize = 'unicode61 remove_diacritics 2'"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS index_meta ("
        "  clave TEXT PRIMARY KEY, valor TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO index_meta (clave, valor) VALUES ('embed_dim', ?)", (str(dim),)
    )
    conn.commit()


def verificar_dimension(conn: sqlite3.Connection, dim: int) -> None:
    """Evita el modo de fallo silencioso de mezclar embeddings de dos modelos."""
    fila = conn.execute("SELECT valor FROM index_meta WHERE clave = 'embed_dim'").fetchone()
    if fila and int(fila["valor"]) != dim:
        raise RuntimeError(
            f"El indice fue construido con embeddings de {fila['valor']} dimensiones "
            f"y el modelo actual produce {dim}. Corre `make reindex` o vuelve al modelo anterior."
        )


def version_corpus(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT version FROM corpus_state WHERE id = 1").fetchone()["version"])


def bump_version_corpus(conn: sqlite3.Connection) -> int:
    """Invalida el cache de recuperacion. Se llama en toda alta y toda baja."""
    conn.execute("UPDATE corpus_state SET version = version + 1 WHERE id = 1")
    return version_corpus(conn)


def registrar_traza(
    conn: sqlite3.Connection, evento: str, payload_json: str, ts: str, call_id: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO traces (call_id, evento, payload_json, ts) VALUES (?, ?, ?, ?)",
        (call_id, evento, payload_json, ts),
    )
