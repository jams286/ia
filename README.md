# ia

Inteligencia artificial local de la casa: un modelo de lenguaje (gemma3:4b) corriendo en el server, sin internet, para tareas cortas de las tools:

- Lista de compras: "✨ Ordenar con IA" lo que se dictó enredado.
- Cocina: "✨ ¿Qué cocino con lo que hay?" y "✨ Llenar desde un texto" (recetas copiadas de WhatsApp o internet).

No es una app del hub: es el servicio que usan esas tools (`https://192.168.1.4/ia/api/`).

## Partes

- `servicio/ollama.service`: Ollama (`~/.local/opt/ollama`, instalado sin sudo) en `127.0.0.1:11434`, solo procesador, un modelo y un pedido a la vez, se descarga de la RAM tras 10 min sin uso.
- `servidor.py` + `servicio/ia.service`: API en `127.0.0.1:8775` con las tareas y los límites (un pedido a la vez, 4 hilos, no empieza nada si el procesador está a 90 °C o más). nginx publica `/ia/api/` → `/api/`.
- Modelos guardados: `gemma3:4b` (el que se usa) y `qwen3:4b` (el segundo mejor de la prueba, por si se quiere probar con `IA_MODELO=qwen3:4b`). Prueba completa en `lab/llm-local/RESULTADOS.md`.

## Comandos

```
cp ~/proyectos/casa/ia/servicio/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ollama.service ia.service
systemctl --user restart ia.service              # después de cambiar servidor.py
journalctl --user -u ia -f                       # cada pedido: tiempo, tokens y temperatura
curl -s http://127.0.0.1:8775/api/estado         # ¿listo?, ¿ocupado?, temperatura, ¿modelo cargado?
```
