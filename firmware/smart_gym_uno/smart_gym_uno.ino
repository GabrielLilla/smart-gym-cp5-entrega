// =====================================================================
//  Smart Gym - Checkpoint 05 | Firmware da estação (Arduino Uno)
//
//  O Uno não tem rede: ele cuida do mundo físico (RFID, LEDs, buzzer,
//  botões) e da máquina de estados. Cada evento relevante é enviado pela
//  Serial (USB) como uma linha JSON iniciada por '@'. O script Python
//  da estação (estacao/estacao.py) lê essas linhas e publica no MQTT.
//
//  Protocolo Serial (9600 baud):
//    Uno -> PC : @{"tipo":"status"|"acesso"|"serie", ...}   (eventos)
//                qualquer outra linha = log de depuração
//    PC -> Uno : UID E2 A3 5F 11   simula a leitura de um cartão
//                REP               repetição válida detectada pela câmera
//                INV               repetição invalidada pela câmera
//                STATUS            reenvia o status atual
// =====================================================================
#include <SPI.h>
#include <MFRC522.h>

// ---------------- Pinagem ----------------
#define PIN_BTN_REP     2   // repetição manual (plano B da visão computacional)
#define PIN_BTN_FIM     3   // encerra a série atual
#define PIN_LED_VERDE   5
#define PIN_LED_VERM    6
#define PIN_BUZZER      8
#define PIN_RFID_RST    9
#define PIN_RFID_SS    10   // MOSI=11, MISO=12, SCK=13 (SPI do Uno)

// ---------------- Regras ----------------
#define TIMEOUT_INATIVIDADE_MS  120000UL  // libera após 2 min sem atividade
#define ANTI_REPIQUE_CARTAO_MS  2000UL    // ignora o mesmo cartão em sequência

// ---------------- Alunos cadastrados ----------------
struct Aluno {
  const char* uid;
  const char* nome;
};

const Aluno ALUNOS[] = {
  {"E2 A3 5F 11", "Ana Souza"},
  {"EC B8 53 73", "Bruno Lima"},
};
const byte TOTAL_ALUNOS = sizeof(ALUNOS) / sizeof(ALUNOS[0]);

MFRC522 rfid(PIN_RFID_SS, PIN_RFID_RST);

// ---------------- Estado da sessão ----------------
bool ocupada = false;
const char* alunoAtual = "";
char uidAtual[24] = "";
int repeticoes = 0;
int invalidadas = 0;
int serieAtual = 0;
unsigned long inicioSerie = 0;
unsigned long ultimaAtividade = 0;

char ultimoUidLido[24] = "";
unsigned long ultimoUidEm = 0;

char linhaSerial[40];
byte tamLinha = 0;

// ---------------- Botões com debounce ----------------
struct Botao {
  byte pino;
  bool estadoEstavel;
  bool ultimaLeitura;
  unsigned long mudouEm;
};

Botao btnRep = {PIN_BTN_REP, HIGH, HIGH, 0};
Botao btnFim = {PIN_BTN_FIM, HIGH, HIGH, 0};

// Retorna true apenas na borda de descida (botão pressionado).
bool foiPressionado(Botao& b) {
  bool leitura = digitalRead(b.pino);
  if (leitura != b.ultimaLeitura) {
    b.mudouEm = millis();
    b.ultimaLeitura = leitura;
  }
  if (millis() - b.mudouEm > 40 && leitura != b.estadoEstavel) {
    b.estadoEstavel = leitura;
    return leitura == LOW;
  }
  return false;
}

// ---------------- Feedback local ----------------
void atualizarLeds() {
  digitalWrite(PIN_LED_VERDE, ocupada ? LOW : HIGH);
  digitalWrite(PIN_LED_VERM, ocupada ? HIGH : LOW);
}

void bipe(byte vezes, int duracaoMs) {
  for (byte i = 0; i < vezes; i++) {
    tone(PIN_BUZZER, 2000, duracaoMs);
    delay(duracaoMs + 60);
  }
}

void sinalizarNegado() {
  for (byte i = 0; i < 3; i++) {
    digitalWrite(PIN_LED_VERM, HIGH);
    tone(PIN_BUZZER, 400, 120);
    delay(150);
    digitalWrite(PIN_LED_VERM, LOW);
    delay(100);
  }
  atualizarLeds();
}

// ---------------- Eventos para o PC (JSON por linha) ----------------
// Formatos ficam na flash (PSTR) para economizar os 2 KB de RAM do Uno.
char evento[176];

void emitirStatus(const char* motivo) {
  snprintf_P(evento, sizeof(evento),
             PSTR("@{\"tipo\":\"status\",\"status\":\"%s\",\"aluno\":\"%s\",\"serie\":%d,\"motivo\":\"%s\"}"),
             ocupada ? "ocupada" : "livre", ocupada ? alunoAtual : "", serieAtual, motivo);
  Serial.println(evento);
}

void emitirAcesso(const char* uid, const char* nome, bool autorizado, const char* motivo) {
  snprintf_P(evento, sizeof(evento),
             PSTR("@{\"tipo\":\"acesso\",\"uid\":\"%s\",\"aluno\":\"%s\",\"autorizado\":%s,\"motivo\":\"%s\"}"),
             uid, nome ? nome : "Desconhecido", autorizado ? "true" : "false", motivo);
  Serial.println(evento);
}

void emitirSerie() {
  snprintf_P(evento, sizeof(evento),
             PSTR("@{\"tipo\":\"serie\",\"aluno\":\"%s\",\"uid\":\"%s\",\"serie\":%d,\"repeticoes\":%d,\"invalidadas\":%d,\"duracao_s\":%lu}"),
             alunoAtual, uidAtual, serieAtual, repeticoes, invalidadas, (millis() - inicioSerie) / 1000UL);
  Serial.println(evento);
}

// ---------------- Máquina de estados ----------------
const char* buscarAluno(const char* uid) {
  for (byte i = 0; i < TOTAL_ALUNOS; i++) {
    if (strcasecmp(uid, ALUNOS[i].uid) == 0) return ALUNOS[i].nome;
  }
  return nullptr;
}

void finalizarSerie() {
  if (!ocupada || (repeticoes == 0 && invalidadas == 0)) return;
  serieAtual++;
  emitirSerie();  // performance só ao fim da série: 1 mensagem por série
  repeticoes = 0;
  invalidadas = 0;
  inicioSerie = millis();
  bipe(2, 80);
}

// LIVRE -> OCUPADA
void iniciarSessao(const char* uid, const char* nome) {
  ocupada = true;
  alunoAtual = nome;
  strncpy(uidAtual, uid, sizeof(uidAtual) - 1);
  repeticoes = 0;
  invalidadas = 0;
  serieAtual = 0;
  inicioSerie = millis();
  ultimaAtividade = millis();

  atualizarLeds();
  bipe(1, 150);
  emitirAcesso(uid, nome, true, "entrada");
  emitirStatus("entrada");  // "Ocupada" logo após o bipe
}

// OCUPADA -> LIVRE
void encerrarSessao(const char* motivo) {
  finalizarSerie();  // não perde repetições ainda não enviadas
  ocupada = false;
  alunoAtual = "";
  uidAtual[0] = '\0';
  repeticoes = 0;
  invalidadas = 0;
  serieAtual = 0;

  atualizarLeds();
  emitirStatus(motivo);
}

void tratarCartao(const char* uid) {
  // Evita que um cartão encostado por mais tempo gere entrada e saída seguidas.
  if (strcmp(uid, ultimoUidLido) == 0 && millis() - ultimoUidEm < ANTI_REPIQUE_CARTAO_MS) return;
  strncpy(ultimoUidLido, uid, sizeof(ultimoUidLido) - 1);
  ultimoUidEm = millis();

  const char* nome = buscarAluno(uid);
  if (!nome) {
    emitirAcesso(uid, nullptr, false, "nao_cadastrado");
    sinalizarNegado();
  } else if (!ocupada) {
    iniciarSessao(uid, nome);
  } else if (strcasecmp(uid, uidAtual) == 0) {
    emitirAcesso(uid, nome, true, "saida");
    encerrarSessao("saida");
    bipe(1, 300);
  } else {
    emitirAcesso(uid, nome, false, "estacao_ocupada");
    sinalizarNegado();
  }
}

// Repetição vinda do botão ou da câmera: só conta com aluno identificado.
void registrarRepeticao(bool valida) {
  if (!ocupada) return;
  ultimaAtividade = millis();
  if (valida) {
    repeticoes++;
    Serial.print(F("Repeticao "));  // log local, não vira mensagem MQTT
    Serial.println(repeticoes);
  } else {
    invalidadas++;
    Serial.print(F("Repeticao invalidada "));
    Serial.println(invalidadas);
  }
}

// ---------------- Entradas ----------------
// Lê o cartão do MFRC522 no formato "A1 B2 C3 D4". Retorna false se não houver cartão.
bool lerCartao(char* uid, size_t tam) {
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return false;
  uid[0] = '\0';
  for (byte i = 0; i < rfid.uid.size && strlen(uid) + 4 < tam; i++) {
    char hex[4];
    snprintf_P(hex, sizeof(hex), i ? PSTR(" %02X") : PSTR("%02X"), rfid.uid.uidByte[i]);
    strcat(uid, hex);
  }
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  return true;
}

// Comandos vindos do script Python (ou digitados no Monitor Serial).
void processarComando(char* cmd) {
  if (strncasecmp_P(cmd, PSTR("UID "), 4) == 0) {
    char* uid = cmd + 4;
    for (char* p = uid; *p; p++) *p = toupper(*p);
    tratarCartao(uid);
  } else if (strcasecmp_P(cmd, PSTR("REP")) == 0) {
    registrarRepeticao(true);
  } else if (strcasecmp_P(cmd, PSTR("INV")) == 0) {
    registrarRepeticao(false);
  } else if (strcasecmp_P(cmd, PSTR("LIBERAR")) == 0) {
    if (ocupada) encerrarSessao("remoto");
  } else if (strcasecmp_P(cmd, PSTR("STATUS")) == 0) {
    emitirStatus("consulta");
  }
}

void lerSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      linhaSerial[tamLinha] = '\0';
      if (tamLinha > 0) processarComando(linhaSerial);
      tamLinha = 0;
    } else if (tamLinha < sizeof(linhaSerial) - 1) {
      linhaSerial[tamLinha++] = c;
    }
  }
}

// =====================================================================
void setup() {
  Serial.begin(9600);
  pinMode(PIN_LED_VERDE, OUTPUT);
  pinMode(PIN_LED_VERM, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_BTN_REP, INPUT_PULLUP);
  pinMode(PIN_BTN_FIM, INPUT_PULLUP);
  atualizarLeds();

  SPI.begin();
  rfid.PCD_Init();

  Serial.println(F("Smart Gym (Uno) pronta. Aproxime o cartao."));
  emitirStatus("inicio");
}

void loop() {
  char uid[24];
  if (lerCartao(uid, sizeof(uid))) tratarCartao(uid);
  lerSerial();

  if (foiPressionado(btnRep)) {
    if (ocupada) registrarRepeticao(true);
    else sinalizarNegado();
  }

  if (foiPressionado(btnFim) && ocupada) {
    ultimaAtividade = millis();
    finalizarSerie();
  }

  if (ocupada && millis() - ultimaAtividade > TIMEOUT_INATIVIDADE_MS) {
    encerrarSessao("timeout");
  }
}
