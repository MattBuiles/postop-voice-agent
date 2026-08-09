# Agente de voz para seguimiento postoperatorio

Tech Sphere Challenge 2026 — Source Meridian.

Un agente que llama al paciente tras su cirugía, conversa en español colombiano,
entiende sus síntomas apoyándose en un corpus clínico con trazabilidad, y decide
cuándo escalar a personal humano.

**Corre entero en local, sin una sola credencial y con costo cero.**

| Entregable | Enlace |
|---|---|
| **Video (demo + preguntas de cierre)** | ⚠️ _PENDIENTE — pegar aquí la URL de YouTube (modo oculto)_ |
| Diagrama de arquitectura y flujo de decisión | [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md) |
| Informe final | [`docs/INFORME-FINAL.md`](docs/INFORME-FINAL.md) |

---

## Arranque rápido

### Opción A — Docker (un comando)

```bash
git clone https://github.com/MattBuiles/postop-voice-agent.git && cd postop-voice-agent
cp .env.example .env
docker compose up
```

Abrir **<http://localhost:8080>**.

> **El micrófono exige contexto seguro.** Use `http://localhost:8080`, no la IP de
> red: sin HTTPS el navegador bloquea `getUserMedia` y la llamada no tendrá voz.

### Opción B — Local sin Docker

```bash
make install     # dependencias + voz de Piper
make dataset     # corpus del reto (solo si se va a reindexar)
make run         # http://localhost:8080
```

Requiere un Ollama accesible en `LLM_BASE_URL` con el modelo declarado:
`ollama pull llama3.2:3b`.

### Qué NO hay que esperar en el arranque

El índice vectorial (**107 documentos, ~2.400 fragmentos**) viaja **pre-construido**
en `data/knowledge.db`. Reconstruirlo tarda unos 12 minutos y se hace solo si se
quiere verificar: `make index`.

---

## Las dos superficies

| Ruta | Qué es |
|---|---|
| `/call` | La llamada: hablar por micrófono, escuchar al agente, y un panel *glass-box* que muestra en vivo los slots que se van llenando, los fragmentos recuperados, la rama de decisión tomada y la latencia por etapa |
| `/admin` | La consola de conocimiento: subir, listar y eliminar documentos; estado de procesamiento; recibos de olvido; y una caja para preguntar directamente al RAG |

---

## Modelo declarado (compuerta G3)

**`llama3.2:3b`**, local vía Ollama. Opcionalmente `llama3.2:1b` como extractor
en la arquitectura de dos niveles (`LLM_EXTRACTOR_MODEL`). Ambos están en la
lista permitida del reto.

El reto permite cuatro **familias** de modelos: Google Gemini gama Flash y Meta
Llama vía Groq en la nube, y Meta Llama serie 3.x (1B–3B) o Microsoft Phi Mini
serie 3.5+ en local. `llama3.2:3b` pertenece a la tercera.

**Por qué local y no en la nube:**

| Opción | Estado verificado (7-ago-2026) |
|---|---|
| Gemini 1.5 Flash | Retirado; hay que usar la generación Flash vigente |
| Llama 3.1 70B en Groq | `llama-3.1-70b-versatile` deprecado el 24-ene-2025; hay que usar el sucesor vigente de la familia |
| **Llama 3.2 (1B/3B) local** | Disponible, sin depender de que un proveedor lo retire |
| Phi Mini local | Disponible |

Los snapshots concretos que nombraba el material del reto ya no se sirven. El
propio documento de stack lo contempla y admite el sucesor vigente de la misma
familia y proveedor, así que la nube sigue siendo una opción legítima. Se
descartó igualmente por **reproducibilidad**: un modelo local garantiza que el
jurado ejecute exactamente lo mismo que nosotros, hoy y dentro de seis meses, sin
límites de peticiones ni dependencia de red en una sesión cronometrada.

`src/postop/config.py` **falla el arranque** si el modelo configurado no pertenece
a ninguna de las familias permitidas. La compuerta se defiende con código, no con
disciplina.

---

## Cómo un modelo de 3B es suficiente

El modelo no decide el triaje. Solo extrae campos tipados. El reparto:

| Tarea | Quién la hace | Por qué |
|---|---|---|
| Temperatura y dolor | Parser determinista (`asr/numeros.py`) | 108/117 aciertos sobre los enunciados reales del dataset, y **cero errores en la dirección peligrosa** |
| Los 4 slots categóricos | LLM con salida estructurada, una invocación por slot | 85% de aciertos, ~1,3 s |
| Estado de la herida | LLM + piso léxico que solo puede **subir** la severidad | El modelo subestimaba "rojita en el borde" como normal |
| **Decisión de escalar** | **Regla determinista** (`triage/rules.py`) | Auditable, testeable y explicable a un clínico |
| Redacción de las preguntas | Texto fijo, audio pre-sintetizado | 0 ms de TTS en la mayoría de los turnos |

---

## Evaluación

### Triaje — `make eval`

Contra los **160 casos etiquetados** del dataset del reto:

| Perfil | Exactitud | Falsos negativos | Falsos positivos | Recall de rojos |
|---|---:|---:|---:|---:|
| `conservative` (por defecto) | 142/160 (88,8%) | **0** | 18 | **100%** |
| `optimal` | 157/160 (98,1%) | **0** | 3 | **100%** |

`conservative` es el default pese a su menor exactitud: cambia 15 falsos positivos
adicionales por margen de seguridad clínica, que es la dirección en la que la
rúbrica pide equivocarse. Se cambia con `TRIAGE_PROFILE=optimal`.

`eval/run_triage_eval.py` **falla con código distinto de cero** si aparece un falso
negativo: es una prueba de regresión, no un informe.

### Resistencia a inyección de prompt

**23/23 (100%)** sobre `eval/attacks.yaml`: 12 ataques directos, 8 controles
negativos (conversación legítima que **no** debe bloquearse) y 3 documentos con
inyección **indirecta** — texto adversario incrustado en un PDF que se sube a la
consola, que es el vector natural de la compuerta G5.

---

## Métricas

<!-- METRICAS:INICIO -->

_Generado por `make metrics` desde 9 llamada(s) y 28 turno(s) registrados en `logs/`. Commit `10d3461`._

| Métrica | Valor |
|---|---|
| Latencia de respuesta P50 | **2501 ms** |
| Latencia de respuesta P95 | **8220 ms** |
| Turnos medidos | 28 |
| Tokens de entrada por turno (medio) | 121 |
| Tokens de salida por turno (medio) | 3 |
| Tokens de entrada por llamada (medio) | 377 |
| Tokens de salida por llamada (medio) | 9 |
| Invocaciones al modelo por turno | 0.36 |
| Consultas al RAG por llamada | 0.11 |

**Desglose de latencia por etapa** (ms):

| Etapa | P50 | P95 |
|---|---|---|
| extraccion | 2360 | 8211 |
| primer_audio | 2501 | 8220 |
| stt | 1968 | 5143 |
| total | 2501 | 8220 |

**Costo estimado por llamada.** La solución corre local, así que el costo medido es cero. Se extrapola el mismo consumo de tokens a precios de API de producción para hacer la cifra comparable:

| Escenario | USD por llamada |
|---|---|
| local (Llama 3.2 3B en Ollama) | $0.000000 |
| modelo pequeno de nube (referencia) | $0.000041 |
| modelo grande de nube (referencia) | $0.001267 |

_Fórmula: `(tokens_entrada / 1e6 × precio_entrada) + (tokens_salida / 1e6 × precio_salida)`, con los tokens medidos por Ollama (`prompt_eval_count` y `eval_count`), no estimados._

<!-- METRICAS:FIN -->

Estos números **no se escriben a mano**. `make metrics` los recalcula desde los
JSONL de `logs/` y reescribe esta sección estampando el commit. La rúbrica
contrasta el README contra los logs de la sesión; la forma segura de que
concuerden es que nadie los transcriba.

### Alcance de la medición

Las cifras salen de las sesiones ejecutadas **sobre el build entregado**. Las
sesiones anteriores, hechas mientras se corregían defectos que ya no existen en
el código, están en [`logs/archivo/`](logs/archivo/): **no se han borrado**, se
han sacado del cálculo, igual que no se reportarían mediciones de una versión
distinta del producto.

Qué contenían y por qué se excluyen:

| Defecto de aquellas sesiones | Coste observado | Corregido en |
|---|---|---|
| El reconocedor se cargaba en el primer turno | 20,6 s de transcripción | Se carga al arrancar |
| La gramática de salida se compilaba por slot en caliente | 4–18 s por turno | Se precalientan los 4 slots |
| Contexto RAG de 2195 tokens | 40 s en un turno | 2 fragmentos recortados |
| El modelo se descargaba de memoria entre llamadas | 48,4 s en un turno | `keep_alive` 2 h + latido cada 10 min |

Todas ellas son reproducibles a partir de los propios archivos archivados.

---

## Arquitectura

Detalle completo en [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md). En una frase:
un proceso Python y un archivo SQLite.

```
navegador (VAD + micrófono + reproducción + barge-in)
    │  WebSocket: PCM 16 kHz ↑ / audio ↓ / eventos JSON
    ▼
FastAPI ── dialog/ ── máquina de 6 slots, silencios, terceros, guardas
        ── asr/    ── faster-whisper + parser de números en español
        ── llm/    ── Ollama: extracción por slot y respuestas ancladas
        ── rag/    ── híbrido vectorial + léxico, RRF, verificador de anclaje
        ── triage/ ── regla determinista + banderas rojas + incertidumbre
        ── summary/── JSON + bundle FHIR R4 + alerta persistida
        ── obs/    ── traza JSONL por turno, métricas, costeo
    │
    ├── Ollama (contenedor aparte)
    └── data/knowledge.db  ·  chunks + vec0 + fts5 + llamadas + alertas + recibos
```

### Decisiones que vale la pena mirar

**Todo el conocimiento vive en un solo archivo SQLite.** El índice denso
(`sqlite-vec`), el léxico (FTS5) y los metadatos comparten el mismo `rowid`, así
que borrar un documento es **una transacción**. No existe ningún otro almacén
donde pueda sobrevivir una copia: el olvido de la compuerta G5 es estructural.

**Recuperación híbrida, no puramente vectorial.** Medido sobre 10 consultas
coloquiales contra 18 pasajes, el mejor modelo denso disponible acierta 6 en el
primer puesto. Lo léxico aporta los términos exactos ("38", "purulenta") y lo
denso el salto entre idiomas ("me sale pus" → "purulent discharge"). Se fusionan
con RRF, que solo usa el puesto y no exige calibrar escalas incomparables.

**Verificador de anclaje.** El modelo debe entregar la frase literal del corpus
que respalda su respuesta; esa frase se contrasta contra el fragmento real antes
de que el agente hable. Si no verifica, el agente declara su límite en vez de
responder. La alucinación clínica deja de ser un riesgo probabilístico.

**Filtro por escenario quirúrgico.** No es una optimización: la carpeta
`breast_cancer/` del corpus entregado contiene guías de **cáncer de cuello
uterino** (136 fragmentos). Sin el filtro, una pregunta sobre una mastectomía
recupera material de otra patología.

**El agente nunca tranquiliza.** El cierre en verde siempre enumera los signos de
alarma que obligan a consultar. "Tranquilizar al paciente ante un síntoma de
alarma" es una conducta penalizada de forma explícita por la rúbrica.

---

## Configuración

Todo en `.env` (ver `.env.example`). Ninguna variable es un secreto.

| Variable | Por defecto | Para qué |
|---|---|---|
| `LLM_MODEL` | `llama3.2:3b` | Modelo de razonamiento (validado contra la lista permitida) |
| `LLM_EXTRACTOR_MODEL` | *(vacío)* | Modelo pequeño opcional solo para extraer slots |
| `EMBED_BACKEND` | `fastembed` | `fastembed` (ONNX, sin torch) o `bge-m3` |
| `TRIAGE_PROFILE` | `conservative` | `conservative` u `optimal` |
| `ASR_MODEL` | `small` | Tamaño de Whisper |

---

## Verificación

```bash
make verify   # comprueba las 5 compuertas contra la aplicación viva
make eval     # triaje (160 casos) + inyección (23 ataques)
make test     # pruebas unitarias
make lint     # ruff
```

`make verify` sube un PDF que el agente nunca ha visto, comprueba que lo aprende,
lo borra y comprueba que lo olvida. Imprime el recibo con los fragmentos
eliminados.

---

## Limitaciones (honestas)

- **La regla de triaje está calibrada sobre 160 casos sintéticos** generados por
  un mismo proceso, así que es probable que sobreajuste al generador. Por eso el
  sistema no depende solo de ella: encima hay banderas rojas por síntomas de
  texto libre y escalamiento por incertidumbre, capas que no dependen de ese
  ajuste. Validar los umbrales con un clínico real es el primer punto del plan a
  dos semanas.
- **`movilidad` es el slot más débil** (7/11 en la muestra medida). Falla
  sobrecalificando la gravedad, que es la dirección segura.
- **Un PDF del corpus está escaneado sin capa de texto.** Se recupera con OCR
  (`rapidocr-onnxruntime`), pero su calidad de extracción es menor que la del resto.
- **Sin telefonía real**, por diseño: el reto lo excluye explícitamente.
- El corpus entregado no es todo el material de evaluación, así que el agente
  responderá "no lo sé" ante temas fuera de él. Es el comportamiento buscado.

---

## Datos y licencia

Código bajo **MIT** (ver [`LICENSE`](LICENSE)).

El corpus de `challenge-data/` pertenece a sus autores y **no se redistribuye en
este repositorio**: se clona aparte con `make dataset`. Los datos clínicos del
reto son sintéticos y no están validados clínicamente.
