"""Configuracion unica del sistema, leida de variables de entorno.

Todos los valores por defecto apuntan al camino 100% local y gratuito: la
solucion debe levantar sin una sola credencial.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Compuerta G3: el modelo de lenguaje debe pertenecer a una de las familias
# permitidas por el reto. El material del reto lo define por FAMILIA y no por
# version exacta, porque los proveedores retiran snapshots sin aviso:
#
#   - Google Gemini, gama Flash          (nube, nivel gratuito)
#   - Meta Llama via Groq                (nube, nivel gratuito)
#   - Meta Llama serie 3.x, 1B-3B        (local, CPU)
#   - Microsoft Phi Mini serie 3.5+      (local, CPU)
#
# La validacion se hace por prefijo de familia, no contra una lista de
# identificadores: fijar versiones exactas romperia el arranque en cuanto Ollama
# publique un llama3.3:3b, que seguiria siendo perfectamente valido.
#
# Esto NO es decorativo: validar() falla el arranque si se configura cualquier
# otra cosa, para que un despiste no cueste la descalificacion.
FAMILIAS_PERMITIDAS: dict[str, tuple[str, ...]] = {
    "Meta Llama (local, serie 3.x)": ("llama3.", "llama-3."),
    "Microsoft Phi Mini (local, serie 3.5+)": ("phi3.5", "phi-3.5", "phi4-mini", "phi3."),
    "Google Gemini gama Flash (nube)": ("gemini-", "gemini/"),
    "Meta Llama via Groq (nube)": ("groq/llama", "llama-3.3-", "llama3.3"),
}


def familia_de(modelo: str) -> str | None:
    """Devuelve la familia permitida a la que pertenece el modelo, o None."""
    nombre = modelo.strip().lower()
    for familia, prefijos in FAMILIAS_PERMITIDAS.items():
        if any(nombre.startswith(p) for p in prefijos):
            return familia
    return None

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
            if familia_de(modelo) is None:
                raise ValueError(
                    f"LLM '{modelo}' no pertenece a ninguna familia permitida por el reto "
                    f"(compuerta G3, que descalifica la entrega). "
                    f"Familias admitidas: {', '.join(FAMILIAS_PERMITIDAS)}"
                )
        if self.triage_profile not in {"conservative", "optimal"}:
            raise ValueError(
                f"TRIAGE_PROFILE='{self.triage_profile}' invalido: usa 'conservative' u 'optimal'"
            )


config = Config()
