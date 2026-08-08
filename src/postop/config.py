"""Configuracion unica del sistema, leida de variables de entorno.

Todos los valores por defecto apuntan al camino 100% local y gratuito: la
solucion debe levantar sin una sola credencial.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Compuerta G3: el modelo de lenguaje debe ser uno de la lista permitida por el
# reto. Esta constante NO es decorativa: config.validar() falla el arranque si
# se configura cualquier otro, para que un despiste no cueste la descalificacion.
MODELOS_PERMITIDOS = {
    "llama3.2:1b",
    "llama3.2:3b",
    "llama3.2",  # alias de Ollama, resuelve a 3b
    "phi3.5",
    "phi3.5:3.8b-mini-instruct-q4_K_M",
}

RAIZ = Path(__file__).resolve().parents[2]


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_model: str = "llama3.2:3b"
    llm_base_url: str = "http://localhost:11434"
    llm_extractor_model: str = ""

    embed_backend: str = "fastembed"
    embed_model: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"

    asr_backend: str = "faster-whisper"
    asr_model: str = "small"
    tts_voice: str = "es_MX-claude-high"

    triage_profile: str = "conservative"

    db_path: Path = Path("data/knowledge.db")
    log_dir: Path = Path("logs")
    dataset_dir: Path = Path("challenge-data/dataset")

    @property
    def db_absoluta(self) -> Path:
        return self.db_path if self.db_path.is_absolute() else RAIZ / self.db_path

    @property
    def logs_absoluta(self) -> Path:
        return self.log_dir if self.log_dir.is_absolute() else RAIZ / self.log_dir

    @property
    def dataset_absoluta(self) -> Path:
        return self.dataset_dir if self.dataset_dir.is_absolute() else RAIZ / self.dataset_dir

    @property
    def modelo_extractor(self) -> str:
        """Modelo usado para extraer slots. Permite la arquitectura de dos niveles
        (1B extrae, 3B genera) sin obligar a ella."""
        return self.llm_extractor_model or self.llm_model

    def validar(self) -> None:
        """Falla ruidosamente si la configuracion violaria una compuerta."""
        for modelo in {self.llm_model, self.modelo_extractor}:
            if modelo not in MODELOS_PERMITIDOS:
                raise ValueError(
                    f"LLM '{modelo}' esta fuera de la lista permitida por el reto "
                    f"(compuerta G3, que descalifica la entrega). "
                    f"Permitidos: {sorted(MODELOS_PERMITIDOS)}"
                )
        if self.triage_profile not in {"conservative", "optimal"}:
            raise ValueError(
                f"TRIAGE_PROFILE='{self.triage_profile}' invalido: usa 'conservative' u 'optimal'"
            )


config = Config()
