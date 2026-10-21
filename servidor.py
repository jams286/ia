"""Servicio "ia" de la casa: tareas cortas con un modelo local (Ollama + gemma3:4b, solo procesador).

Escucha solo en 127.0.0.1; nginx publica /ia/api/ -> /api/. Ollama queda solo para este servicio.
Sin dependencias externas (solo la biblioteca estándar).

    python3 servidor.py

Límites para no castigar la laptop (ver lab/llm-local/RESULTADOS.md):
    - un pedido a la vez (los demás esperan turno hasta ESPERA_MAX y si no, "intenta en un momento");
    - 4 hilos (la mitad del procesador) y prioridad baja (el servicio corre con Nice);
    - si el procesador pasa de TEMP_MAX °C no se empieza nada nuevo;
    - textos de entrada acotados.

Rutas:
    GET  /api/estado               {"listo", "ocupado", "temperatura", "modelo_cargado"}
    POST /api/precalentar          carga el modelo en RAM (la página lo pide al abrir un diálogo de IA)
    POST /api/ordenar-lista        {"texto"}            -> {"items": [{"nombre", "cantidad"}]}
    POST /api/importar-receta      {"texto"}            -> {"titulo", "porciones", "tiempo", "ingredientes": [..], "pasos": [..]}
    POST /api/sugerir-recetas      {"despensa": [..]}   -> {"recetas": [{"titulo", "usa", "falta", "idea"}]}
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = '127.0.0.1'
PUERTO = int(os.environ.get('IA_PUERTO', 8775))
OLLAMA = os.environ.get('IA_OLLAMA', 'http://127.0.0.1:11434')
MODELO = os.environ.get('IA_MODELO', 'gemma3:4b')
HILOS = 4
TEMP_MAX = 90
ESPERA_MAX = 90
MAX_JSON = 32 * 1024

SISTEMA = ('Eres un ayudante de cocina de una familia en El Salvador. Respondes en español, '
           'con nombres de productos y comidas como se dicen allá, y solo con el JSON pedido.')

turno = threading.Semaphore(1)
ocupado = threading.Event()

# Sensor del paquete del procesador (el número de zona cambia entre equipos)
SENSOR = next((z.parent / 'temp' for z in Path('/sys/class/thermal').glob('thermal_zone*/type')
               if z.read_text().strip() == 'x86_pkg_temp'), None)


class Rechazo(Exception):
    """Errores con mensaje para mostrar en la página."""

    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


def temperatura():
    try:
        return int(SENSOR.read_text()) // 1000 if SENSOR else None
    except OSError:
        return None


# ---------- Esquemas de respuesta (Ollama obliga al modelo a seguirlos) ----------

def lista_de(tipo):
    return {'type': 'array', 'items': tipo}


TEXTO = {'type': 'string'}
ITEM = {'type': 'object', 'properties': {'nombre': TEXTO, 'cantidad': TEXTO}, 'required': ['nombre', 'cantidad']}
ESQUEMA_ITEMS = {'type': 'object', 'properties': {'items': lista_de(ITEM)}, 'required': ['items']}
ESQUEMA_RECETA = {
    'type': 'object',
    'properties': {'titulo': TEXTO, 'porciones': TEXTO, 'tiempo': TEXTO,
                   'ingredientes': lista_de(TEXTO), 'pasos': lista_de(TEXTO)},
    'required': ['titulo', 'porciones', 'tiempo', 'ingredientes', 'pasos'],
}
ESQUEMA_SUGERENCIAS = {
    'type': 'object',
    'properties': {'recetas': lista_de({
        'type': 'object',
        'properties': {'titulo': TEXTO, 'usa': lista_de(TEXTO), 'falta': lista_de(TEXTO), 'idea': TEXTO},
        'required': ['titulo', 'usa', 'falta', 'idea']})},
    'required': ['recetas'],
}


# ---------- Ollama ----------

def ollama(ruta, cuerpo, espera=240):
    pedido = urllib.request.Request(f'{OLLAMA}{ruta}', data=json.dumps(cuerpo).encode(),
                                    headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(pedido, timeout=espera) as r:
            return json.load(r)
    except urllib.error.URLError as e:
        raise Rechazo('El modelo no está disponible ahora. Intenta en un momento.', 503) from e


def preguntar(pedido, esquema):
    """Un pedido al modelo, respetando turno y temperatura. Devuelve el JSON ya leído."""
    if not turno.acquire(timeout=ESPERA_MAX):
        raise Rechazo('La IA está ocupada con otro pedido. Intenta en un momento.', 503)
    try:
        t = temperatura()
        if t is not None and t >= TEMP_MAX:
            raise Rechazo(f'El servidor está caliente ({t} °C). Intenta en un par de minutos.', 503)
        ocupado.set()
        inicio = time.monotonic()
        r = ollama('/api/chat', {
            'model': MODELO,
            'messages': [{'role': 'system', 'content': SISTEMA}, {'role': 'user', 'content': pedido}],
            'format': esquema,
            'stream': False,
            'keep_alive': '10m',
            'options': {'num_thread': HILOS, 'temperature': 0.2, 'num_ctx': 4096},
        })
        print(f'Pedido en {time.monotonic() - inicio:.1f} s, {r.get("eval_count", 0)} tokens, '
              f'{temperatura()} °C', flush=True)
        try:
            return json.loads(r['message']['content'])
        except (KeyError, json.JSONDecodeError):
            raise Rechazo('La IA respondió algo que no se entiende. Intenta otra vez.', 502) from None
    finally:
        ocupado.clear()
        turno.release()


def precalentar():
    # Sin turno: cargar el modelo no estorba a un pedido (Ollama lo hace una sola vez)
    ollama('/api/generate', {'model': MODELO, 'prompt': '', 'keep_alive': '10m'}, espera=120)
    return {'listo': True}


def estado():
    cargados = []
    try:
        with urllib.request.urlopen(f'{OLLAMA}/api/ps', timeout=5) as r:
            cargados = json.load(r).get('models', [])
        listo = True
    except (urllib.error.URLError, OSError):
        listo = False
    return {
        'listo': listo,
        'ocupado': ocupado.is_set(),
        'temperatura': temperatura(),
        'modelo': MODELO,
        'modelo_cargado': any(m.get('name', '').startswith(MODELO) for m in cargados),
    }


# ---------- Tareas ----------

def texto_de(datos, maximo):
    texto = datos.get('texto')
    if not isinstance(texto, str) or not texto.strip():
        raise Rechazo('Falta el texto')
    if len(texto) > maximo:
        raise Rechazo(f'El texto es muy largo (máximo {maximo} caracteres)')
    return texto.strip()


def limpiar_items(items):
    salida = []
    for i in items if isinstance(items, list) else []:
        nombre = re.sub(r'\s+', ' ', str(i.get('nombre', ''))).strip()[:60]
        cantidad = re.sub(r'\s+', ' ', str(i.get('cantidad', ''))).strip()[:20]
        if nombre:
            salida.append({'nombre': nombre[0].upper() + nombre[1:], 'cantidad': cantidad})
    return salida


def ordenar_lista(datos):
    texto = texto_de(datos, 600)
    r = preguntar(
        'Convierte este texto dictado en una lista de compras limpia. Quita muletillas, corrige nombres, '
        'respeta las correcciones de quien habla ("bueno mejor dos"), separa cada producto y pon la cantidad '
        f'con su unidad si se dijo (si no, cantidad vacía). No inventes productos.\n\nTexto: "{texto}"',
        ESQUEMA_ITEMS)
    return {'items': limpiar_items(r.get('items'))}


def importar_receta(datos):
    texto = texto_de(datos, 4000)
    r = preguntar(
        'Convierte este texto (copiado de un mensaje o de internet) en una receta ordenada. '
        'Título corto; porciones y tiempo si los dice (si no, vacío). Ingredientes: uno por elemento, con su '
        'cantidad al inicio como en "2 tazas de harina". Pasos: uno por elemento, cortos, en orden, y si el texto '
        'dice un tiempo de cocción ponlo en el paso ("hornear 25 minutos"). No inventes nada que no esté en el texto.'
        f'\n\nTexto:\n"""\n{texto}\n"""', ESQUEMA_RECETA)

    def lineas(valor, maximo):
        return [re.sub(r'\s+', ' ', str(x)).strip()[:300] for x in (valor if isinstance(valor, list) else [])
                if str(x).strip()][:maximo]

    return {
        'titulo': str(r.get('titulo', '')).strip()[:100],
        'porciones': str(r.get('porciones', '')).strip()[:30],
        'tiempo': str(r.get('tiempo', '')).strip()[:30],
        'ingredientes': lineas(r.get('ingredientes'), 80),
        'pasos': lineas(r.get('pasos'), 60),
    }


def sugerir_recetas(datos):
    despensa = datos.get('despensa')
    if not isinstance(despensa, list) or not despensa:
        raise Rechazo('La despensa está vacía')
    productos = ', '.join(re.sub(r'\s+', ' ', str(p)).strip()[:60] for p in despensa[:120] if str(p).strip())
    r = preguntar(
        f'Esto es lo que hay en la despensa: {productos}.\n\nSugiere 3 comidas caseras que se puedan hacer hoy '
        'con eso. Para cada una: título, qué productos de la despensa usa, qué falta (vacío si no falta nada; '
        'no pongas como faltante algo que sí está en la despensa) y una idea de cómo prepararla en una o dos frases.',
        ESQUEMA_SUGERENCIAS)
    recetas = []
    for x in (r.get('recetas') or [])[:3]:
        recetas.append({
            'titulo': str(x.get('titulo', '')).strip()[:100],
            'usa': [str(u).strip()[:60] for u in x.get('usa', []) if str(u).strip()][:30],
            'falta': [str(f).strip()[:60] for f in x.get('falta', []) if str(f).strip()][:15],
            'idea': str(x.get('idea', '')).strip()[:600],
        })
    return {'recetas': recetas}


# ---------- HTTP ----------

RUTAS = {
    ('GET', '/api/estado'): lambda d: estado(),
    ('POST', '/api/precalentar'): lambda d: precalentar(),
    ('POST', '/api/ordenar-lista'): ordenar_lista,
    ('POST', '/api/importar-receta'): importar_receta,
    ('POST', '/api/sugerir-recetas'): sugerir_recetas,
}


class Manejador(BaseHTTPRequestHandler):
    server_version = 'IA'

    def log_message(self, formato, *args):
        pass

    def responder(self, codigo, datos):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode('utf-8')
        self.send_response(codigo)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def leer_json(self):
        largo = int(self.headers.get('Content-Length') or 0)
        if largo > MAX_JSON:
            raise Rechazo('Pedido demasiado grande', 413)
        if not largo:
            return {}
        try:
            datos = json.loads(self.rfile.read(largo))
        except json.JSONDecodeError:
            raise Rechazo('JSON inválido') from None
        if not isinstance(datos, dict):
            raise Rechazo('Se esperaba un objeto JSON')
        return datos

    def atender(self):
        accion = RUTAS.get((self.command, self.path.split('?')[0]))
        if not accion:
            self.responder(404, {'error': 'No existe'})
            return
        try:
            self.responder(200, accion(self.leer_json() if self.command == 'POST' else {}))
        except Rechazo as e:
            self.responder(e.codigo, {'error': str(e)})
        except Exception as e:  # noqa: BLE001 - que el servicio no se caiga por un pedido
            print('Error:', repr(e), flush=True)
            self.responder(500, {'error': 'Error interno'})

    do_GET = do_POST = atender


def main():
    servidor = ThreadingHTTPServer((HOST, PUERTO), Manejador)
    print(f'IA en http://{HOST}:{PUERTO} (modelo {MODELO}, Ollama {OLLAMA})', flush=True)
    servidor.serve_forever()


if __name__ == '__main__':
    main()
