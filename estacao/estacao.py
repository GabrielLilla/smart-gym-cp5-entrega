"""
Smart Gym - CP05 | Script Python da estação (Publisher MQTT)

Ponte entre o Arduino Uno (USB Serial) e o broker MQTT:
  - lê as linhas "@{json}" emitidas pelo Uno e publica em smartgym/<estacao>/<tipo>;
  - assina smartgym/<estacao>/cmd e repassa os comandos ao Uno (ex.: LIBERAR);
  - define o Last Will: se o script cair, o broker publica "offline";
  - com --visao, conta as repetições pela câmera (módulo visao.py, evolução do CP4)
    e envia REP/INV ao Uno, que continua dono da máquina de estados.

Uso:
  python estacao.py --porta COM9                      # repetições pelo botão
  python estacao.py --porta COM9 --visao              # repetições pela câmera
  python estacao.py --porta COM9 --visao --pose-model lite --source 1
  (digite um UID no terminal, ex.: E2 A3 5F 11, para simular um cartão)
"""
import argparse
import json
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt
import serial

TIPOS_PUBLICADOS = {"status", "acesso", "serie"}
COMANDOS_ACEITOS = {"liberar": "LIBERAR", "status": "STATUS"}
DESCANSO_IDEAL_S = (60, 90)  # faixa de descanso entre séries sugerida no desafio

VERDE, VERMELHO, AMARELO, BRANCO = (0, 200, 0), (0, 0, 230), (0, 200, 255), (230, 230, 230)


def parse_args():
    p = argparse.ArgumentParser(description="Estação Smart Gym: Arduino Uno (Serial) -> MQTT")
    p.add_argument("--porta", default=os.getenv("SERIAL_PORT", "COM9"), help="porta serial do Arduino")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--broker", default=os.getenv("MQTT_HOST", "3.135.234.101"))
    p.add_argument("--broker-porta", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    p.add_argument("--estacao", default=os.getenv("ESTACAO_ID", "estacao01"))
    p.add_argument("--base", default=os.getenv("TOPICO_BASE", "smartgym"))
    p.add_argument("--visao", action="store_true", help="conta repetições pela câmera (YOLO + MediaPipe)")
    # Opções não reconhecidas aqui (--source, --pose-model, --no-mirror...) vão para o visao.py
    return p.parse_known_args()


class Estacao:
    def __init__(self, args):
        self.estacao = args.estacao
        self.base = f"{args.base}/{args.estacao}"
        self.arduino = serial.Serial(args.porta, args.baud, timeout=1)
        self.lock_serial = threading.Lock()
        self.parar = threading.Event()

        # Espelho do estado do Uno, atualizado pelos eventos que passam pela Serial
        self.ocupada = False
        self.aluno = ""
        self.ultima_serie = None  # (numero, repeticoes, instante)
        self.visao = None

        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"smartgym-{args.estacao}")
        user = os.getenv("MQTT_USER")
        if user:
            self.mqtt.username_pw_set(user, os.getenv("MQTT_PASS"))
        self.mqtt.will_set(f"{self.base}/conexao", "offline", qos=1, retain=True)
        self.mqtt.on_connect = self.ao_conectar
        self.mqtt.on_message = self.ao_receber_comando
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=10)
        self.mqtt.connect_async(args.broker, args.broker_porta, keepalive=15)

    # ---------------- MQTT ----------------
    def ao_conectar(self, cliente, _userdata, _flags, rc, _props=None):
        if rc != 0:
            print(f"[MQTT] falha ao conectar: {rc}")
            return
        print(f"[MQTT] conectado. Publicando em {self.base}/...")
        cliente.publish(f"{self.base}/conexao", "online", qos=1, retain=True)
        cliente.subscribe(f"{self.base}/cmd", qos=1)
        self.enviar_ao_arduino("STATUS")  # republica o estado atual após (re)conectar

    def ao_receber_comando(self, _cliente, _userdata, msg):
        acao = msg.payload.decode(errors="replace").strip().lower()
        print(f"[MQTT] SUB {msg.topic} <- {acao}")
        if acao in COMANDOS_ACEITOS:
            self.enviar_ao_arduino(COMANDOS_ACEITOS[acao])

    def publicar_evento(self, evento):
        tipo = evento.pop("tipo", None)
        if tipo not in TIPOS_PUBLICADOS:
            return
        self.atualizar_estado_local(tipo, evento)
        evento["estacao"] = self.estacao
        payload = json.dumps(evento, ensure_ascii=False)
        # status é retained: quem assinar depois recebe o estado atual da máquina
        info = self.mqtt.publish(f"{self.base}/{tipo}", payload, qos=1, retain=(tipo == "status"))
        situacao = "PUB" if info.rc == mqtt.MQTT_ERR_SUCCESS else "SEM BROKER (na fila)"
        print(f"[MQTT] {situacao} {self.base}/{tipo} {payload}")

    def atualizar_estado_local(self, tipo, evento):
        if tipo == "status":
            ocupada = evento.get("status") == "ocupada"
            if ocupada != self.ocupada:
                self.ultima_serie = None
                self.zerar_visao()  # novo aluno (ou saída): contador da câmera recomeça
            self.ocupada, self.aluno = ocupada, evento.get("aluno", "")
        elif tipo == "serie":
            self.ultima_serie = (evento.get("serie"), evento.get("repeticoes"), time.time())
            self.zerar_visao()  # cada série começa do zero

    # ---------------- Visão computacional ----------------
    def zerar_visao(self):
        if self.visao:
            self.visao.zerar()

    def ao_evento_visao(self, evento):
        if not self.ocupada:
            return  # sem aluno identificado pelo RFID, a repetição não é contabilizada
        self.enviar_ao_arduino("REP" if evento == "+1 REP" else "INV")

    def contexto_visao(self):
        if not self.ocupada:
            return [("Estacao LIVRE - passe o cartao RFID para iniciar", VERDE)]
        linhas = [(f"OCUPADA - {self.aluno}", VERMELHO)]
        if self.ultima_serie:
            numero, reps, quando = self.ultima_serie
            descanso = int(time.time() - quando)
            minimo, maximo = DESCANSO_IDEAL_S
            dica = "pode iniciar a proxima serie" if descanso >= minimo else f"descanso ideal {minimo}-{maximo}s"
            cor = AMARELO if descanso > maximo else BRANCO
            linhas.append((f"Serie {numero}: {reps} reps | descanso {descanso}s ({dica})", cor))
        return linhas

    # ---------------- Serial ----------------
    def enviar_ao_arduino(self, linha):
        with self.lock_serial:
            self.arduino.write(f"{linha}\n".encode())

    def ler_teclado(self):
        """Permite simular um cartão digitando o UID no terminal."""
        for linha in sys.stdin:
            uid = linha.strip().upper()
            if uid:
                self.enviar_ao_arduino(f"UID {uid}")

    def ler_serial(self):
        try:
            while not self.parar.is_set():
                bruto = self.arduino.readline()
                if not bruto:
                    continue
                linha = bruto.decode(errors="replace").strip()
                if not linha.startswith("@"):
                    print(f"[UNO] {linha}")
                    continue
                try:
                    self.publicar_evento(json.loads(linha[1:]))
                except json.JSONDecodeError:
                    print(f"[UNO] linha inválida: {linha}")
        except serial.SerialException as erro:
            print(f"\n[SERIAL] Arduino desconectado ou porta em uso: {erro}")
        finally:
            self.parar.set()

    def executar(self, args_visao=None):
        self.mqtt.loop_start()
        threading.Thread(target=self.ler_teclado, daemon=True).start()
        time.sleep(2)  # o Uno reinicia ao abrir a porta serial
        threading.Thread(target=self.ler_serial, daemon=True).start()
        print("Estação pronta. Aproxime um cartão ou digite um UID (ex.: E2 A3 5F 11).")

        if args_visao is None:
            while not self.parar.wait(0.5):
                pass
            return

        # A janela do OpenCV precisa rodar na thread principal.
        from visao import ContadorVisao
        self.visao = ContadorVisao(args_visao)
        print("Visão ativa: janela da câmera aberta (q = sair).")
        self.visao.executar(ao_evento=self.ao_evento_visao, contexto=self.contexto_visao,
                            deve_parar=self.parar.is_set)

    def encerrar(self):
        self.parar.set()
        if self.mqtt.is_connected():
            self.mqtt.publish(f"{self.base}/conexao", "offline", qos=1, retain=True).wait_for_publish(2)
        self.mqtt.loop_stop()
        self.mqtt.disconnect()
        self.arduino.close()


if __name__ == "__main__":
    args, resto = parse_args()
    args_visao = None
    if args.visao:
        from visao import build_parser
        args_visao = build_parser(add_help=False).parse_args(resto)
    elif resto:
        raise SystemExit(f"Opções desconhecidas: {' '.join(resto)} (as opções de câmera exigem --visao)")

    estacao = Estacao(args)
    try:
        estacao.executar(args_visao)
    except KeyboardInterrupt:
        print("\nEncerrando estação...")
    finally:
        estacao.encerrar()
