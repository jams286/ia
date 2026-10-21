# ia (casa)

Servicio de IA local que usan otras tools. No tiene página propia ni entrada en el hub.

## Cómo está hecho

- Ollama 0.40.7 instalado en el usuario (`~/.local/opt/ollama`, sin sudo), servicio `ollama.service` (copia en `servicio/`): `127.0.0.1:11434`, `CUDA_VISIBLE_DEVICES=-1` (solo procesador: la MX150 daba ~15 % más pero calentaba más), `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=10m`, `Nice=10`. Modelos en `~/.ollama/models`: `gemma3:4b` (en uso, ~3.5 GB de RAM cargado) y `qwen3:4b` (reserva).
- `servidor.py` (`ia.service`, `127.0.0.1:8775`, solo biblioteca estándar): tareas con prompt fijo y respuesta forzada a JSON con esquema (`format` de Ollama), `temperature 0.2`, `num_thread 4`, `num_ctx 4096`. Semáforo de 1 pedido (los demás esperan hasta 90 s → 503 "ocupada"); si el paquete del procesador (`x86_pkg_temp`) está a ≥ 90 °C responde 503 sin empezar. Todo lo que devuelve el modelo se limpia y recorta antes de responder. Variables `IA_PUERTO`, `IA_MODELO`, `IA_OLLAMA` para pruebas.
- Rutas: `GET /api/estado`, `POST /api/precalentar`, `POST /api/ordenar-lista {texto}`, `POST /api/importar-receta {texto}`, `POST /api/sugerir-recetas {despensa: [...]}`.
- Tiempos medidos con gemma3:4b: precalentar ~10 s (solo la primera vez en 10 min), ordenar lista ~10–22 s, importar receta ~35 s, sugerir recetas ~40–50 s. Temperatura máxima en las pruebas: 67–73 °C.
- Lo usan: `casa/lista-compras` (Ordenar con IA) y `casa/cocina` (¿Qué cocino?, Llenar desde un texto). Las páginas muestran "Pensando… N s".

## Dónde corre

- nginx: `/ia/api/` → `127.0.0.1:8775/api/` con `proxy_read_timeout` largo (config en `infra/nginx`).
- Origen de la decisión: `lab/llm-local` (prueba de 5 modelos).
