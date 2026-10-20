"""
Smart Gym - Contador de repetições por visão computacional (evolução do CP4)
---------------------------------------------------------------------------
- YOLO (COCO, classe 67 = "cell phone") detecta o "equipamento" (proxy do halter,
  já que o COCO não possui a classe halter).
- MediaPipe PoseLandmarker extrai ombro/cotovelo/punho e calcula o ângulo do cotovelo.
- Máquina de estados conta só ciclos completos: EXTENSAO -> FLEXAO -> EXTENSAO,
  e apenas enquanto o equipamento está presente (por padrão, na mão).

No CP05 este módulo é usado pelo estacao.py (--visao): cada "+1 REP" vira o
comando REP para o Arduino e cada "REP INVALIDADA" vira INV.

Também roda sozinho, como no CP4:
    python visao.py                   # webcam 0
    python visao.py --pose-model lite # mais leve (CPU fraca)

Instalação: pip install -r requirements-visao.txt
Na primeira execução baixa yolo11n.pt e pose_landmarker_<variante>.task;
rode uma vez com internet antes da apresentação.

Teclas: q = sair | r = zerar contador | s = alternar braço (auto/esq/dir)
"""
import argparse
import math
import os
import time
import urllib.request
from collections import deque

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision
from ultralytics import YOLO

COCO_CELL_PHONE = 67
VIS_MIN = 0.5
POSE_GRACE_FRAMES = 5

POSE_MODEL_URLS = {
    "lite": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
    "full": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task",
    "heavy": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task",
}

# Índices dos 33 landmarks do MediaPipe Pose (lado anatômico do aluno)
ARMS = {
    "ESQ": (11, 13, 15),  # ombro, cotovelo, punho esquerdos
    "DIR": (12, 14, 16),  # ombro, cotovelo, punho direitos
}
# Com a imagem espelhada, o MediaPipe enxerga o braço esquerdo real como direito.
ARMS_ESPELHADO = {"ESQ": ARMS["DIR"], "DIR": ARMS["ESQ"]}
SKELETON = [
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
    (15, 17), (15, 19), (16, 18), (16, 20),
    (11, 23), (12, 24), (23, 24), (23, 25), (25, 27), (24, 26), (26, 28),
]


# ----------------------------------------------------------------- utilitários
def ensure_pose_model(variant):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"pose_landmarker_{variant}.task")
    if not os.path.exists(path):
        print(f"Baixando modelo de pose ({variant})...")
        urllib.request.urlretrieve(POSE_MODEL_URLS[variant], path)
    return path


def abrir_camera(source):
    """Abre webcam (índice) ou vídeo. No Windows tenta DirectShow antes do MSMF,
    que falha com várias webcams USB."""
    if not source.isdigit():
        cap = cv2.VideoCapture(source)
        return cap if cap.isOpened() else None
    backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY] if os.name == "nt" else [cv2.CAP_ANY]
    for backend in backends:
        cap = cv2.VideoCapture(int(source), backend)
        if cap.isOpened() and cap.read()[0]:
            return cap
        cap.release()
    return None


def joint_angle(a, b, c):
    ba = np.array(a) - np.array(b)
    bc = np.array(c) - np.array(b)
    cos = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc) + 1e-6)
    return math.degrees(math.acos(np.clip(cos, -1.0, 1.0)))


def point_box_distance(px, py, box):
    x1, y1, x2, y2 = box
    dx = max(x1 - px, 0, px - x2)
    dy = max(y1 - py, 0, py - y2)
    return math.hypot(dx, dy)


def landmark_visibility(p, vis_supported):
    if vis_supported:
        return getattr(p, "visibility", 0.0) or 0.0
    # Fallback: algumas versões retornam visibility=0 sempre; usa "dentro da imagem"
    return 1.0 if (0.0 <= p.x <= 1.0 and 0.0 <= p.y <= 1.0) else 0.0


# ------------------------------------------------------------------ componentes
class EquipmentTracker:
    """Votação em janela deslizante para evitar flicker do YOLO."""

    def __init__(self, window=10, min_hits=4):
        self.hist = deque(maxlen=window)
        self.min_hits = min_hits

    def update(self, detected):
        self.hist.append(detected)
        return sum(self.hist) >= self.min_hits


class RepCounter:
    IDLE, EXTENDED, FLEXED = "AGUARDANDO EXTENSAO", "ESTENDIDO", "FLEXIONADO"

    def __init__(self, ext_thr=150.0, flex_thr=50.0):
        self.ext_thr = ext_thr
        self.flex_thr = flex_thr
        self.reset()

    def reset(self):
        self.count = 0
        self.invalid = 0
        self.state = self.IDLE

    def update(self, angle, equipment_ok):
        if angle is None or not equipment_ok:
            event = None
            if self.state == self.FLEXED:
                self.invalid += 1
                event = "REP INVALIDADA"
            self.state = self.IDLE
            return event

        if self.state == self.IDLE and angle >= self.ext_thr:
            self.state = self.EXTENDED
        elif self.state == self.EXTENDED and angle <= self.flex_thr:
            self.state = self.FLEXED
        elif self.state == self.FLEXED and angle >= self.ext_thr:
            self.count += 1
            self.state = self.EXTENDED
            return "+1 REP"
        return None


# -------------------------------------------------------------------- desenho
def draw_skeleton(frame, pts_px, vis):
    for a, b in SKELETON:
        if vis[a] >= VIS_MIN and vis[b] >= VIS_MIN:
            cv2.line(frame, pts_px[a], pts_px[b], (160, 160, 160), 2)
    for i in {i for pair in SKELETON for i in pair}:
        if vis[i] >= VIS_MIN:
            cv2.circle(frame, pts_px[i], 4, (220, 220, 220), -1)


def draw_panel(frame, counter, present, angle, side, side_mode, fps, event, ext_thr, flex_thr):
    h, w = frame.shape[:2]
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (340, 215), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)

    cv2.putText(frame, "REPETICOES", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 2)
    cv2.putText(frame, str(counter.count), (15, 95), cv2.FONT_HERSHEY_SIMPLEX, 2.4, (255, 255, 255), 5)

    status, color = (("Equipamento Detectado", (0, 200, 0)) if present
                     else ("Aguardando Equipamento", (0, 0, 230)))
    cv2.rectangle(frame, (15, 110), (325, 140), color, -1)
    cv2.putText(frame, status, (22, 132), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)

    ang_txt = f"{angle:.0f} graus" if angle is not None else "--"
    cv2.putText(frame, f"Fase: {counter.state}", (15, 165), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1)
    cv2.putText(frame, f"Cotovelo ({side or '-'} | {side_mode}): {ang_txt}", (15, 185),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1)
    cv2.putText(frame, f"Invalidadas: {counter.invalid}   FPS: {fps:.1f}", (15, 205),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1)

    if angle is not None:
        p = np.clip((ext_thr - angle) / (ext_thr - flex_thr), 0, 1)
        x0, y0, x1, y1 = w - 50, 60, w - 20, h - 60
        cv2.rectangle(frame, (x0, y0), (x1, y1), (200, 200, 200), 2)
        fill_y = int(y1 - p * (y1 - y0))
        cv2.rectangle(frame, (x0 + 3, fill_y), (x1 - 3, y1 - 3), (0, 200, 255), -1)
        cv2.putText(frame, f"{int(p * 100)}%", (x0 - 10, y0 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    if event:
        c = (0, 220, 0) if event.startswith("+") else (0, 0, 255)
        cv2.putText(frame, event, (w // 2 - 110, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.3, c, 3)


def draw_estacao(frame, linhas):
    """Faixa inferior com o contexto da estação (aluno, status, descanso)."""
    h, w = frame.shape[:2]
    altura = 26 * len(linhas) + 12
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, h - altura), (w, h), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)
    for i, (texto, cor) in enumerate(linhas):
        cv2.putText(frame, texto, (15, h - altura + 28 + 26 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.65, cor, 2)


# --------------------------------------------------------------- contador
def build_parser(add_help=True):
    ap = argparse.ArgumentParser(add_help=add_help)
    ap.add_argument("--source", default="0", help="índice da webcam ou caminho de vídeo")
    ap.add_argument("--model", default="yolo11n.pt")
    ap.add_argument("--pose-model", default="full", choices=["lite", "full", "heavy"])
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--yolo-every", type=int, default=2,
                    help="roda o YOLO a cada N frames (reaproveita as caixas entre eles)")
    ap.add_argument("--ext", type=float, default=150.0, help="ângulo mínimo de extensão")
    ap.add_argument("--flex", type=float, default=50.0, help="ângulo máximo de flexão")
    ap.add_argument("--hand-radius", type=float, default=1.0,
                    help="distância punho-caixa aceita, em múltiplos do lado maior da caixa")
    ap.add_argument("--any-position", action="store_true")
    ap.add_argument("--no-mirror", action="store_true")
    ap.add_argument("--save", default=None, help="caminho .mp4 para gravar a saída")
    return ap


class ContadorVisao:
    """Loop de câmera do CP4 encapsulado para ser usado pela estação.

    ao_evento(evento): chamado com "+1 REP" ou "REP INVALIDADA".
    contexto(): retorna linhas [(texto, cor_bgr)] desenhadas no rodapé.
    deve_parar(): encerra o loop quando retornar True.
    zerar(): pode ser chamado de outra thread (ex.: fim de série).
    """

    def __init__(self, args):
        self.args = args
        self.arms = ARMS if args.no_mirror else ARMS_ESPELHADO
        self.model = YOLO(args.model)
        options = vision.PoseLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=ensure_pose_model(args.pose_model)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.landmarker = vision.PoseLandmarker.create_from_options(options)
        self.cap = abrir_camera(args.source)
        if self.cap is None:
            raise SystemExit(
                f"Não foi possível abrir a câmera/fonte: {args.source}\n"
                "  - Tente outro índice: --source 1 (ou 2)\n"
                "  - Feche Teams/navegador/outros programas que usem a câmera\n"
                "  - Windows: Configurações > Privacidade > Câmera > permitir apps da área de trabalho\n"
                "  - Sem webcam: use um vídeo gravado, ex.: --source treino.mp4")
        self.counter = RepCounter(args.ext, args.flex)
        self._pedido_zerar = False

    def zerar(self):
        self._pedido_zerar = True

    def executar(self, ao_evento=None, contexto=None, deve_parar=None):
        args = self.args
        tracker = EquipmentTracker()
        side_modes = ["AUTO", "ESQ", "DIR"]
        side_mode_idx = 0
        active_side, ema_angle, missing = None, None, 0
        event, event_until = None, 0.0
        writer, t_prev, fps = None, time.time(), 0.0
        last_ts = -1
        frame_idx = 0
        boxes, confs = np.empty((0, 4)), np.empty(0)

        while not (deve_parar and deve_parar()):
            ok, frame = self.cap.read()
            if not ok:
                break
            if not args.no_mirror:
                frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]

            if self._pedido_zerar:
                self._pedido_zerar = False
                self.counter.reset()

            # ---- MediaPipe Tasks (timestamp precisa ser estritamente crescente)
            ts = int(time.monotonic() * 1000)
            if ts <= last_ts:
                ts = last_ts + 1
            last_ts = ts
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = self.landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)

            arms, pts_px, vis = {}, None, None
            if result.pose_landmarks:
                lm = result.pose_landmarks[0]
                vis_supported = any((getattr(p, "visibility", 0.0) or 0.0) > 0 for p in lm)
                vis = [landmark_visibility(p, vis_supported) for p in lm]
                pts_px = [(int(p.x * w), int(p.y * h)) for p in lm]
                for side, ids in self.arms.items():
                    arms[side] = {"pts": [(lm[i].x * w, lm[i].y * h) for i in ids],
                                  "vis": min(vis[i] for i in ids)}
            visible_arms = {s: a for s, a in arms.items() if a["vis"] >= VIS_MIN}

            # ---- YOLO: celular (classe COCO 67), a cada N frames para poupar CPU
            if frame_idx % max(args.yolo_every, 1) == 0:
                det = self.model.predict(frame, classes=[COCO_CELL_PHONE], conf=args.conf,
                                         imgsz=args.imgsz, verbose=False)[0]
                boxes = det.boxes.xyxy.cpu().numpy() if det.boxes is not None else np.empty((0, 4))
                confs = det.boxes.conf.cpu().numpy() if det.boxes is not None else np.empty(0)
            frame_idx += 1

            # ---- Associação equipamento <-> mão
            raw_ok, holding_side, best_box, best_score = False, None, None, math.inf
            for box, cf in zip(boxes, confs):
                if args.any_position and (best_box is None or cf > best_box[1]):
                    raw_ok, best_box = True, (box, cf)
                side_size = max(box[2] - box[0], box[3] - box[1])
                for side, a in visible_arms.items():
                    d = point_box_distance(*a["pts"][2], box)
                    if d <= side_size * args.hand_radius and d < best_score:
                        raw_ok, holding_side, best_box, best_score = True, side, (box, cf), d
            present = tracker.update(raw_ok)

            # ---- Escolha do braço (travado durante a flexão)
            mode = side_modes[side_mode_idx]
            if mode != "AUTO":
                new_side = mode
            elif self.counter.state == RepCounter.FLEXED and active_side is not None:
                new_side = active_side
            elif holding_side:
                new_side = holding_side
            elif visible_arms:
                new_side = max(visible_arms, key=lambda s: visible_arms[s]["vis"])
            else:
                new_side = active_side
            if new_side != active_side:
                ema_angle = None
            active_side = new_side

            # ---- Ângulo do cotovelo (EMA + tolerância a perda de pose)
            if active_side in visible_arms:
                raw_ang = joint_angle(*visible_arms[active_side]["pts"])
                ema_angle = raw_ang if ema_angle is None else 0.6 * ema_angle + 0.4 * raw_ang
                missing = 0
            else:
                missing += 1
            angle = ema_angle if missing <= POSE_GRACE_FRAMES else None

            ev = self.counter.update(angle, present)
            if ev:
                event, event_until = ev, time.time() + 1.2
                if ao_evento:
                    ao_evento(ev)

            # ---- Desenho
            if pts_px is not None:
                draw_skeleton(frame, pts_px, vis)
            if active_side in visible_arms and angle is not None:
                s, e, wr = [tuple(map(int, p)) for p in visible_arms[active_side]["pts"]]
                cv2.line(frame, s, e, (0, 255, 255), 5)
                cv2.line(frame, e, wr, (0, 255, 255), 5)
                cv2.putText(frame, f"{angle:.0f}", (e[0] + 10, e[1]), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            if best_box is not None:
                (x1, y1, x2, y2), cf = best_box[0].astype(int), best_box[1]
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 0), 2)
                cv2.putText(frame, f"celular {cf:.2f}", (x1, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2)

            now = time.time()
            fps = 0.9 * fps + 0.1 * (1.0 / max(now - t_prev, 1e-6))
            t_prev = now
            draw_panel(frame, self.counter, present, angle, active_side, mode, fps,
                       event if now < event_until else None, args.ext, args.flex)
            if contexto:
                draw_estacao(frame, contexto())

            if args.save:
                if writer is None:
                    out_fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
                    writer = cv2.VideoWriter(args.save, cv2.VideoWriter_fourcc(*"mp4v"), out_fps, (w, h))
                writer.write(frame)

            cv2.imshow("Smart Gym - Visao", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("r"):
                self.counter.reset()
            if key == ord("s"):
                side_mode_idx = (side_mode_idx + 1) % len(side_modes)
                active_side, ema_angle = None, None

        self.cap.release()
        if writer:
            writer.release()
        self.landmarker.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    ContadorVisao(build_parser().parse_args()).executar()
