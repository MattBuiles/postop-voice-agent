"""Recuperacion hibrida: vectorial + lexica, fusionadas con RRF.

Por que hibrida y no solo densa: el benchmark de embed.py mide que el mejor
modelo dento disponible acierta 6 de 10 consultas coloquiales en el primer
puesto. El indice lexico aporta lo que el denso pierde -- terminos exactos como
"38", "purulenta" o el nombre de un procedimiento -- y el denso aporta lo que el
lexico no puede: emparejar "me sale pus" con "purulent discharge".

Reciprocal Rank Fusion se elige sobre una suma ponderada de scores porque los
dos sistemas producen escalas incomparables (distancia L2 frente a BM25). RRF
solo usa el puesto, asi que no hay que calibrar nada.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

import numpy as np

from postop.db import store
from postop.rag import lexico
from postop.rag.embed import Embedder

K_RRF = 60           # constante estandar de RRF: amortigua el peso de los primeros puestos
N_CANDIDATOS = 20    # por sistema, antes de fusionar
N_FINAL = 4          # fragmentos que llegan al contexto del modelo

# Realce para material dirigido al paciente.
#
# El corpus del reto son 107 documentos y la gran mayoria es literatura clinica
# para profesionales: series de casos, metaanalisis, guias de practica. Solo un
# punado habla con el paciente ("Guia para el paciente", "Plan de cuidado en
# casa", "Recovery Guide"). Sin este realce, "¿cuando puedo levantar peso?"
# recupera un articulo sobre ingestion de cuerpos extranos en vez del
# instructivo de alta que responde exactamente esa pregunta.
#
# El realce es modesto a proposito: reordena, no censura. Si la unica fuente de
# una respuesta es un paper, ese paper sigue llegando al contexto.
REALCE_PACIENTE = 1.6

_PATRON_PACIENTE = re.compile(
    r"gu[ií]a (para el |del )?paciente|plan de cuidado|instructivo|recomendaciones para|"
    r"cuidados en casa|patient (guide|information|education)|recovery guide|"
    r"enhancing your recovery|discharge|para pacientes y cuidadores|automanejo",
    re.I,
)


@dataclass(frozen=True)
class Pasaje:
    chunk_uid: str
    doc_id: str
    documento: str
    pagina: int
    seccion: str | None
    texto: str
    score: float
    origen: str          # denso | lexico | ambos

    def cita(self) -> str:
        return f"{self.documento} p.{self.pagina}"

    def to_dict(self) -> dict:
        return {
            "chunk_uid": self.chunk_uid,
            "documento": self.documento,
            "pagina": self.pagina,
            "seccion": self.seccion,
            "score": round(self.score, 4),
            "origen": self.origen,
            "texto": self.texto,
        }


class Recuperador:
    def __init__(self, conn: sqlite3.Connection, embedder: Embedder) -> None:
        self.conn = conn
        self.embedder = embedder
        # La version del corpus forma parte de la clave del cache. Sin esto, un
        # documento borrado podria seguir contestando desde una entrada vieja y
        # la compuerta G5 fallaria de la forma mas dificil de depurar.
        self._cache: dict[tuple[int, str, str | None], list[Pasaje]] = {}

    def buscar(self, consulta: str, *, escenario: str | None = None, n: int = N_FINAL) -> list[Pasaje]:
        clave = (store.version_corpus(self.conn), consulta.strip().lower(), escenario)
        if clave in self._cache:
            return self._cache[clave][:n]

        densos = self._buscar_denso(consulta, escenario)
        lexicos = self._buscar_lexico(consulta, escenario)
        fusionados = self._fusionar(densos, lexicos)
        self._cache[clave] = fusionados
        return fusionados[:n]

    # ------------------------------------------------------------------ densa

    def _buscar_denso(self, consulta: str, escenario: str | None) -> list[str]:
        vector = self.embedder.embed_consulta(consulta)
        # sqlite-vec no admite filtrar por una tabla externa dentro del MATCH,
        # asi que se pide de mas y se filtra despues por escenario.
        filas = self.conn.execute(
            "SELECT chunk_rowid FROM vec_chunks WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (np.asarray(vector, dtype=np.float32).tobytes(), N_CANDIDATOS * 3),
        ).fetchall()
        return self._filtrar_por_escenario([f["chunk_rowid"] for f in filas], escenario)

    # ----------------------------------------------------------------- lexica

    def _buscar_lexico(self, consulta: str, escenario: str | None) -> list[str]:
        expresion = lexico.consulta_fts(consulta)
        if not expresion:
            return []
        try:
            filas = self.conn.execute(
                "SELECT rowid FROM fts_chunks WHERE fts_chunks MATCH ? "
                "ORDER BY bm25(fts_chunks) LIMIT ?",
                (expresion, N_CANDIDATOS * 3),
            ).fetchall()
        except sqlite3.OperationalError:
            # Una expresion malformada no debe tumbar la recuperacion: se
            # degrada a solo-denso, que es peor pero sigue respondiendo.
            return []
        return self._filtrar_por_escenario([f["rowid"] for f in filas], escenario)

    # ---------------------------------------------------------------- comunes

    def _filtrar_por_escenario(self, rowids: list[int], escenario: str | None) -> list[str]:
        """Restringe al escenario quirurgico del paciente.

        No es una optimizacion: la carpeta breast_cancer/ del corpus entregado
        contiene guias de cancer de cuello uterino. Sin este filtro, una
        pregunta sobre una mastectomia puede recuperar material de otra patologia.
        """
        if not rowids:
            return []
        marcadores = ",".join("?" * len(rowids))
        if escenario:
            sql = (
                f"SELECT c.id FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
                f"WHERE c.id IN ({marcadores}) AND (d.escenario = ? OR d.escenario IS NULL "
                f"OR d.origen = 'upload')"
            )
            parametros = [*rowids, escenario]
        else:
            sql = f"SELECT c.id FROM chunks c WHERE c.id IN ({marcadores})"
            parametros = list(rowids)

        permitidos = {fila["id"] for fila in self.conn.execute(sql, parametros)}
        return [str(r) for r in rowids if r in permitidos][:N_CANDIDATOS]

    def _fusionar(self, densos: list[str], lexicos: list[str]) -> list[Pasaje]:
        puntajes: dict[str, float] = {}
        origenes: dict[str, set[str]] = {}
        for lista, nombre in ((densos, "denso"), (lexicos, "lexico")):
            for puesto, rowid in enumerate(lista):
                puntajes[rowid] = puntajes.get(rowid, 0.0) + 1.0 / (K_RRF + puesto + 1)
                origenes.setdefault(rowid, set()).add(nombre)

        if not puntajes:
            return []

        ordenados = sorted(puntajes.items(), key=lambda par: -par[1])
        rowids = [rowid for rowid, _ in ordenados]
        marcadores = ",".join("?" * len(rowids))
        filas = {
            str(fila["id"]): fila
            for fila in self.conn.execute(
                f"SELECT c.id, c.chunk_uid, c.doc_id, c.pagina, c.seccion, c.texto, d.nombre "
                f"FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
                f"WHERE c.id IN ({marcadores})",
                rowids,
            )
        }

        pasajes: list[Pasaje] = []
        for rowid, score in ordenados:
            fila = filas.get(rowid)
            if fila is None:
                continue
            marcas = origenes[rowid]
            if _PATRON_PACIENTE.search(fila["nombre"] or ""):
                score *= REALCE_PACIENTE
            pasajes.append(
                Pasaje(
                    chunk_uid=fila["chunk_uid"],
                    doc_id=fila["doc_id"],
                    documento=fila["nombre"],
                    pagina=fila["pagina"],
                    seccion=fila["seccion"],
                    texto=fila["texto"],
                    score=score,
                    origen="ambos" if len(marcas) == 2 else next(iter(marcas)),
                )
            )
        # El realce cambia el orden, asi que hay que reordenar tras aplicarlo.
        pasajes.sort(key=lambda p: -p.score)
        return pasajes

    def invalidar_cache(self) -> None:
        self._cache.clear()
