"""
Smart Gym - CP05 | Dashboard Web (Flask)

Subscriber MQTT: assina smartgym/+/+ no broker e repassa cada evento ao
navegador em tempo real via Server-Sent Events (SSE), sem refresh manual.
"""
import json
import os
import queue
import threading
import uuid
from collections import deque
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, render_template

MQTT_HOST = os.getenv("MQTT_HOST", "3.135.234.101")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
TOPICO_BASE = os.getenv("TOPICO_BASE", "smartgym")
PORTA = int(os.getenv("PORT", "5000"))
HISTORICO_MAX = 50

app = Flask(__name__)

# ---------------- Estado em memória ----------------
lock = threading.Lock()
estado = {
    "broker": {"url": f"mqtt://{MQTT_HOST}:{MQTT_PORT}", "conectado": False},
    "estacoes": {},  # {id: {status, aluno, serie, ultimas_reps, conexao, atualizadoEm}}
    "series": deque(maxlen=HISTORICO_MAX),   # séries finalizadas (mais recente primeiro)
    "acessos": deque(maxlen=HISTORICO_MAX),  # tentativas de acesso RFID
}
clientes_sse: set[queue.Queue] = set()


def agora():
    return datetime.now(timezone.utc).isoformat()


def snapshot():
    with lock:
        return {
            "broker": estado["broker"],
            "estacoes": estado["estacoes"],
            "series": list(estado["series"]),
            "acessos": list(estado["acessos"]),
        }


def transmitir(evento, dados):
    msg = f"event: {evento}\ndata: {json.dumps(dados, ensure_ascii=False)}\n\n"
    for q in list(clientes_sse):
        q.put(msg)


def estacao(id_):
    return estado["estacoes"].setdefault(id_, {
        "id": id_, "status": "desconhecido", "aluno": "", "serie": 0,
        "ultimas_reps": None, "conexao": "desconhecido", "atualizadoEm": None,
    })


# ---------------- MQTT (subscriber) ----------------
def ao_conectar(cliente, _userdata, _flags, rc, _props=None):
    if rc != 0:
        print(f"[MQTT] falha ao conectar: {rc}")
        return
    print(f"[MQTT] conectado a {estado['broker']['url']}")
    cliente.subscribe(f"{TOPICO_BASE}/+/+", qos=1)
    estado["broker"]["conectado"] = True
    transmitir("broker", estado["broker"])


def ao_desconectar(_cliente, _userdata, _flags, _rc, _props=None):
    print("[MQTT] desconectado, tentando reconectar...")
    estado["broker"]["conectado"] = False
    transmitir("broker", estado["broker"])


def ao_receber(_cliente, _userdata, msg):
    partes = msg.topic.split("/")
    if len(partes) != 3:
        return
    _, id_, tipo = partes
    texto = msg.payload.decode("utf-8", errors="replace")
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        dados = {"valor": texto}
    print(f"[MQTT] {msg.topic} {texto}")

    recebido = agora()
    with lock:
        est = estacao(id_)
        if tipo == "status":
            est["status"] = dados.get("status")
            est["aluno"] = dados.get("aluno", "")
            est["serie"] = dados.get("serie", 0)
            if est["status"] == "livre":
                est["ultimas_reps"] = None
        elif tipo == "conexao":
            est["conexao"] = dados.get("valor")
        elif tipo == "serie":
            est["serie"] = dados.get("serie", est["serie"])
            est["ultimas_reps"] = dados.get("repeticoes")
            estado["series"].appendleft({**dados, "estacao": id_, "recebidoEm": recebido})
        elif tipo == "acesso":
            estado["acessos"].appendleft({**dados, "estacao": id_, "recebidoEm": recebido})
        else:
            return  # ex.: cmd, publicado pelo próprio dashboard
        est["atualizadoEm"] = recebido
        evento = {"tipo": tipo, "estacao": id_, "dados": dados, "recebidoEm": recebido, "estado": dict(est)}
    transmitir("mqtt", evento)


cliente_mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"smartgym-dashboard-{uuid.uuid4().hex[:6]}")
if MQTT_USER:
    cliente_mqtt.username_pw_set(MQTT_USER, MQTT_PASS)
cliente_mqtt.on_connect = ao_conectar
cliente_mqtt.on_disconnect = ao_desconectar
cliente_mqtt.on_message = ao_receber
cliente_mqtt.reconnect_delay_set(min_delay=1, max_delay=10)


# ---------------- Rotas HTTP ----------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/estado")
def api_estado():
    return jsonify(snapshot())


@app.get("/events")
def eventos():
    q = queue.Queue()
    clientes_sse.add(q)

    def stream():
        try:
            yield f"event: snapshot\ndata: {json.dumps(snapshot(), ensure_ascii=False)}\n\n"
            while True:
                try:
                    yield q.get(timeout=25)
                except queue.Empty:
                    yield ": ping\n\n"  # mantém a conexão viva através de proxies
        finally:
            clientes_sse.discard(q)

    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/estacoes/<id_>/<acao>")
def comando(id_, acao):
    """Publica em smartgym/<id>/cmd. A estação assina esse tópico."""
    if acao not in ("liberar", "status"):
        return jsonify(erro="ação inválida"), 400
    cliente_mqtt.publish(f"{TOPICO_BASE}/cmd", acao, qos=1)
    return jsonify(enviado=acao, estacao=id_), 202


if __name__ == "__main__":
    cliente_mqtt.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    cliente_mqtt.loop_start()
    print(f"[HTTP] dashboard em http://localhost:{PORTA}")
    app.run(host="0.0.0.0", port=PORTA, threaded=True)
