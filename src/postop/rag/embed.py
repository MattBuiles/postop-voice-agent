"""Embeddings multilingues para recuperacion cross-lingual.

El problema real de este corpus: mezcla espanol e ingles, asi que la consulta
coloquial del paciente ("me salio pus en la herida") tiene que recuperar
documentos en ingles ("purulent discharge from the surgical site"). Eso exige un
modelo con espacio vectorial compartido entre idiomas.

Se midieron tres candidatos sobre 10 consultas coloquiales contra 18 pasajes
(10 clinicos relevantes en ambos idiomas, 8 distractores plausibles):

    modelo                                    top1   top3   MRR    torch
    BAAI/bge-m3                               6/10   9/10   0.758  si (~2.5 GB)
    paraphrase-multilingual-mpnet-base-v2     6/10   8/10   0.733  no
    intfloat/multilingual-e5-large            5/10   6/10   0.608  no

BGE-M3 (el que sugiere el reto) gana, pero su ventaja sobre mpnet cae dentro del
ruido de una muestra de 10 consultas, y solo es accesible via
`sentence-transformers`, que arrastra torch. La compuerta G2 cronometra el
levantamiento completo en 15 minutos, y torch puede consumir ese presupuesto
entero en una conexion mediana.

De ahi el default: mpnet en ONNX. BGE-M3 queda a un EMBED_BACKEND=bge-m3 de
distancia (extra `bge`) para quien quiera pagar el costo de instalacion.

Ningun modelo denso resuelve esto solo: incluso el mejor falla 4 de 10 consultas.
Por eso la recuperacion es hibrida (ver retrieve.py), no puramente vectorial.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class Embedder(Protocol):
    """Frontera que permite intercambiar el motor sin tocar el resto del RAG."""

    dim: int
    nombre: str

    def embed_pasajes(self, textos: list[str]) -> np.ndarray: ...

    def embed_consulta(self, texto: str) -> np.ndarray: ...


class FastEmbedEmbedder:
    """ONNX, sin torch.

    Los prefijos 'query:'/'passage:' son obligatorios para la familia E5 (fue
    entrenada con ellos) y perjudiciales para el resto, que nunca los vio. Por
    eso se aplican segun el modelo y no de forma incondicional.
    """

    def __init__(self, modelo: str) -> None:
        from fastembed import TextEmbedding

        self.nombre = modelo
        self._motor = TextEmbedding(model_name=modelo)
        self._usa_prefijos_e5 = "e5" in modelo.lower()
        # La dimension se sondea en vez de leerse de un atributo: fastembed la
        # ha movido de sitio entre versiones y un sondeo es estable.
        self.dim = len(next(iter(self._motor.embed(["dim"]))))

    def embed_pasajes(self, textos: list[str]) -> np.ndarray:
        if self._usa_prefijos_e5:
            textos = [f"passage: {t}" for t in textos]
        vectores = self._motor.embed(textos)
        return _normalizar(np.array(list(vectores), dtype=np.float32))

    def embed_consulta(self, texto: str) -> np.ndarray:
        if self._usa_prefijos_e5:
            texto = f"query: {texto}"
        vector = next(iter(self._motor.embed([texto])))
        return _normalizar(np.array([vector], dtype=np.float32))[0]


class SentenceTransformersEmbedder:
    """Backend alterno con BGE-M3, el modelo sugerido por el reto."""

    def __init__(self, modelo: str = "BAAI/bge-m3") -> None:
        from sentence_transformers import SentenceTransformer

        self.nombre = modelo
        self._motor = SentenceTransformer(modelo, device="cpu")
        self.dim = self._motor.get_sentence_embedding_dimension()

    def embed_pasajes(self, textos: list[str]) -> np.ndarray:
        return _normalizar(
            self._motor.encode(textos, batch_size=8, show_progress_bar=False).astype(np.float32)
        )

    def embed_consulta(self, texto: str) -> np.ndarray:
        return _normalizar(self._motor.encode([texto]).astype(np.float32))[0]


def _normalizar(matriz: np.ndarray) -> np.ndarray:
    """L2. Con vectores unitarios la distancia L2 de sqlite-vec es monotona
    respecto del coseno, asi que ordenar por distancia equivale a ordenar por
    similitud coseno sin calcularla aparte."""
    normas = np.linalg.norm(matriz, axis=1, keepdims=True)
    return matriz / np.clip(normas, 1e-12, None)


def crear_embedder(backend: str, modelo: str) -> Embedder:
    if backend == "fastembed":
        return FastEmbedEmbedder(modelo)
    if backend == "bge-m3":
        return SentenceTransformersEmbedder("BAAI/bge-m3")
    raise ValueError(f"EMBED_BACKEND desconocido: {backend!r} (usa 'fastembed' o 'bge-m3')")
