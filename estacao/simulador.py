"""
Smart Gym - CP05 | Simulador de estação (Publisher)

Publica os mesmos tópicos e payloads da estação real. Útil para testar o
dashboard sem hardware e como plano B na apresentação.
Uso: python simulador.py
"""
import json
import os
import time

import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "3.135.234.101")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
ESTACAO = os.getenv("ESTACAO_ID", "estacao02")
BASE = f"{os.getenv('TOPICO_BASE', 'smartgym')}/{ESTACAO}"

cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"smartgym-sim-{ESTACAO}")
cliente.will_set(f"{BASE}/conexao", "offline", qos=1, retain=True)


def pub(tipo, dados, retain=False):
    payload = dados if isinstance(dados, str) else json.dumps({**dados, "estacao": ESTACAO}, ensure_ascii=False)
    cliente.publish(f"{BASE}/{tipo}", payload, qos=1, retain=retain)
    print(f"PUB {BASE}/{tipo} {payload}")


def sessao(aluno, uid, series):
    """series: lista de (repeticoes_validas, repeticoes_invalidadas)."""
    pub("acesso", {"uid": uid, "aluno": aluno, "autorizado": True, "motivo": "entrada"})
    pub("status", {"status": "ocupada", "aluno": aluno, "serie": 0, "motivo": "entrada"}, retain=True)
    for n, (reps, invalidas) in enumerate(series, start=1):
        inicio = time.time()
        time.sleep(reps * 0.6)  # tempo de execução da série
        pub("serie", {"aluno": aluno, "uid": uid, "serie": n, "repeticoes": reps,
                      "invalidadas": invalidas, "duracao_s": round(time.time() - inicio)})
        time.sleep(3)  # descanso
    pub("acesso", {"uid": uid, "aluno": aluno, "autorizado": True, "motivo": "saida"})
    pub("status", {"status": "livre", "aluno": "", "serie": 0, "motivo": "saida"}, retain=True)


if __name__ == "__main__":
    cliente.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    cliente.loop_start()
    print(f"Conectado a {MQTT_HOST}:{MQTT_PORT} como {BASE}")

    pub("conexao", "online", retain=True)
    pub("status", {"status": "livre", "aluno": "", "serie": 0}, retain=True)
    time.sleep(2)

    pub("acesso", {"uid": "99 99 99 99", "aluno": "Desconhecido", "autorizado": False, "motivo": "nao_cadastrado"})
    time.sleep(2)
    sessao("Bruno Lima", "11 22 33 44", [(10, 0), (8, 2), (12, 1)])
    time.sleep(1)

    # Desconexão limpa não dispara o Last Will: avisa o "offline" explicitamente.
    pub("conexao", "offline", retain=True)
    time.sleep(1)
    cliente.loop_stop()
    cliente.disconnect()
