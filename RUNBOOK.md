# Runbook — qué hacer ahora, en orden

Estado: las 5 compuertas pasan, el corpus está indexado y las evaluaciones corren.
Falta lo que necesita tu micrófono, tu cuenta de GitHub y tu cara frente a cámara.

Todos los comandos se ejecutan desde `/home/mbuil/projects/techsphere-2026`.

---

## 0. Arreglar `.env` — HAZLO ANTES DE ARRANCAR

El índice está construido con **768 dimensiones** (`paraphrase-multilingual-mpnet-base-v2`).
Si tu `.env` copió la línea vieja de la plantilla, dice `intfloat/multilingual-e5-large`,
que produce 1024, y la aplicación **se negará a arrancar** con este mensaje:

```
RuntimeError: El indice fue construido con embeddings de 768 dimensiones
y el modelo actual produce 1024.
```

Eso es intencional: mezclar dos espacios vectoriales rompería la recuperación en
silencio, y prefiero un fallo ruidoso. La línea debe quedar así:

```bash
EMBED_MODEL=sentence-transformers/paraphrase-multilingual-mpnet-base-v2
```

Comprueba también que estas dos estén correctas:

```bash
LLM_MODEL=llama3.2:3b          # cualquier otro valor fuera de la lista aborta el arranque (G3)
LLM_BASE_URL=http://localhost:11434
```

---

## 1. Reiniciar la aplicación

**Sí, hay que reiniciar.** `config = Config()` se instancia al importar el módulo,
así que `.env` se lee una sola vez al arrancar el proceso. Cambiarlo con la app
viva no tiene ningún efecto.

```bash
pkill -f postop.main:app
sleep 2
make run
```

Espera a ver estas dos líneas antes de seguir:

```
modelo llama3.2:3b precalentado en NNNN ms
Application startup complete.
```

El precalentado tarda unos segundos y es deliberado: sin él, el primer turno
hablado paga la carga completa del modelo (medido: 17,2 s), y ese es justo el
turno con el que el jurado verifica G4.

Comprobación rápida en otra terminal:

```bash
curl -s localhost:8080/api/salud | python3 -m json.tool
```

Debe decir `"llm_disponible": true`, `"tts": true`, `"chunks": 4460`.

---

## 2. La llamada de voz real (esto es lo que falta para las métricas)

Abre **<http://localhost:8080/call>** en Chrome.

> Tiene que ser `localhost`, no la IP de la red. El micrófono exige contexto
> seguro y sin HTTPS el navegador bloquea `getUserMedia`.

1. Elige un paciente y el día postoperatorio.
2. Pulsa **Iniciar llamada** y acepta el permiso del micrófono.
3. Habla normal. La barra de nivel se mueve; tras ~700 ms de silencio se cierra
   el turno y el agente responde.
4. Haz **al menos dos llamadas completas** (6 preguntas cada una) para que haya
   suficientes turnos hablados que promediar.

**Guion sugerido para la llamada roja** (es el caso real del dataset, con el
paciente minimizando su propia fiebre):

| Pregunta del agente | Qué decir |
|---|---|
| Apertura | "Sí, claro, dígame" |
| Dolor | "Ay no, más o menos no más, un dolorcito. Si acaso un seis" |
| Fiebre | "Me tomé la temperatura y salió como en treinta y ocho, pero eso debe ser del calor de acá no más" |
| — | *(el agente debe interrumpir el protocolo aquí y escalar)* |

Si el agente escala en ese punto, has demostrado en vivo lo más difícil del reto:
extraer el hecho (38,0 °C) por encima de la interpretación del paciente ("no es nada").

**Guion para la llamada verde:** responde con normalidad a las seis preguntas.
Fíjate en que el cierre **siempre** enumera los signos de alarma; nunca dice
"todo está bien" y ya.

---

## 3. Generar las métricas del README

```bash
make metrics
```

Reescribe la sección entre `<!-- METRICAS:INICIO -->` y `<!-- METRICAS:FIN -->`
con lo que digan los logs, estampando el commit.

**No edites esos números a mano nunca.** La rúbrica contrasta el README contra
los logs de la sesión y advierte que reportar cifras que no se sostienen penaliza
más que no reportarlas.

Si sale `No hay turnos registrados`, es que solo hiciste llamadas por el camino de
texto: los turnos de texto se excluyen a propósito del cálculo de latencia, porque
no tienen ni transcripción ni espera de audio y hundirían la mediana.

---

## 4. Verificación completa antes de entregar

```bash
make verify   # las 5 compuertas contra la app viva
make eval     # triaje (160 casos) + inyección (23 ataques)
make lint
```

Esperado:

```
5/5 compuertas aprobadas
OK: ningun falso negativo critico en ningun perfil.
TOTAL  23/23 (100.0%)
All checks passed!
```

---

## 5. Prueba de arranque en frío cronometrada (compuerta G2)

Esto es lo que más entregas elimina. Hazlo **en una carpeta limpia**, siguiendo tu
propio README al pie de la letra y con cronómetro:

```bash
cd /tmp && rm -rf prueba-g2
git clone https://github.com/MattBuiles/postop-voice-agent.git prueba-g2 && cd prueba-g2
cp .env.example .env
time docker compose up
```

Objetivo: **≤ 15 minutos** hasta tener la app respondiendo. Si te pasas, lo que hay
que recortar es la descarga del modelo, no la documentación.

---

## 6. Git — necesita tu autorización explícita

No he tocado git. La rúbrica puntúa la historia de commits, así que conviene que
no sea un único commit gigante.

```bash
git init
git add -A
git status          # revisa ANTES de comitear
```

Verifica que **no** entren: `.env`, `challenge-data/` (133 MB que no son nuestros
para redistribuir), `models/` ni `logs/`. Ya están en `.gitignore`, pero míralo.

`data/knowledge.db` **sí debe entrar**: es el índice pre-construido que salva G2.
Comprueba su tamaño con `du -h data/knowledge.db`; si pasa de 100 MB necesitarás
git-lfs o publicarlo como release de GitHub.

Luego crea el repositorio **público** en GitHub con licencia MIT.

---

## 7. Entregables que faltan

| # | Entregable | Estado |
|---|---|---|
| 01 | Repositorio público | falta crear en GitHub |
| 02 | Diagrama | `docs/ARQUITECTURA.md` tiene los dos diagramas en Mermaid; expórtalos a PNG |
| 03 | Informe final | falta redactar — la materia prima está en los docstrings y en `PLAN-MAESTRO.md` |
| 04 | Video | falta grabar |

### Guion del video (8–9 min)

| Tramo | Qué mostrar |
|---|---|
| 0:00–0:30 | El problema, con la cita del propio README del reto |
| 0:30–1:30 | Llamada **verde** completa, con el panel glass-box a la vista |
| 1:30–3:00 | Llamada **roja**: el paciente minimiza los 38 °C, el agente extrae el hecho y escala |
| 3:00–4:00 | Consola: subir un PDF → preguntar → borrarlo → volver a preguntar → mostrar el recibo de olvido |
| 4:00–5:00 | `make eval` en pantalla: 0 falsos negativos, 23/23 en inyección |
| 5:00–6:30 | **Pregunta 1** frente a cámara |
| 6:30–8:30 | **Pregunta 2** frente a cámara |

**Para la Pregunta 2, la decisión técnica más fuerte es la elección de modelo.**
El reto permite cuatro familias, no versiones exactas, y admite el sucesor vigente
cuando un proveedor retira un snapshot — cosa que ya pasó con dos de los modelos
que nombraba el material (Gemini 1.5 Flash y Llama 3.1 70B en Groq).

El argumento no es que la nube estuviera prohibida: es que era **frágil**. Un
modelo local elimina la dependencia de que un proveedor mantenga un snapshot, los
límites de peticiones por minuto y la red en una sesión cronometrada. Y de ahí que
la arquitectura se rediseñara para que un 3B bastara: el modelo no decide el
triaje, solo extrae seis campos tipados bajo esquema forzado.

Riesgos que conviene reconocer en cámara (reconocerlos suma, ocultarlos resta):
la regla de triaje está calibrada sobre 160 casos sintéticos y probablemente
sobreajusta al generador; `movilidad` es el slot más débil y falla sobrecalificando
la gravedad, que es la dirección segura.

---

## Cuidado con la memoria de WSL

Esta máquina tiene 11 GB. **No corras a la vez** la indexación y la aplicación:
juntas se acercan al límite. Si necesitas reindexar, para antes la app.

```bash
free -h                                    # ver disponible
pkill -f postop.main:app                   # parar la app
make index                                 # reindexar (~17 min, pico 3,7 GB)
```

La ingesta imprime su pico de memoria al terminar. Si vuelve a acercarse a los
6 GB, hay una regresión en el tamaño de lote (`LOTE_EMBEBIDO` en `rag/ingest.py`).
