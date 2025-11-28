from flask import Flask, request, jsonify
import threading
import time
import requests
import socket

from escpos.printer import Usb

app = Flask(__name__)

# =========================
# CONFIG
# =========================

AI_SERVER_BASE_URL = "http://localhost:8000"  # your AI/parliament server

ARTNET_PORT = 6454
ARTNET_UNIVERSE = 0  # first universe
DMX_UNIVERSE_SIZE = 512
DMX_FPS = 30  # how often to send Art-Net frames

# Default order of characters (can be overridden by /start payload)
DEFAULT_ACTORS_ORDER = ["rain", "fungi", "bee", "fox", "tree"]

# DMX channels for each actor (1-based)
ACTOR_CONFIG = {
    "rain":  {"light_channel": 1,  "motor_channel": 2},
    "fungi": {"light_channel": 3,  "motor_channel": 4},
    "bee":   {"light_channel": 5,  "motor_channel": 6},
    "fox":   {"light_channel": 7,  "motor_channel": 8},
    "tree":  {"light_channel": 9,  "motor_channel": 10},
}

MIN_SLOT_DURATION = 10.0  # seconds per character minimum (used for discussion AND voting)

# === THERMAL PRINTER CONFIG (USB ESC/POS) ===
# Replace these with your printer IDs from `lsusb` on Pi
PRINTER_VID = 0x0483
PRINTER_PID = 0x5743
PRINTER_IN_EP = 0x82   # often 0x82
PRINTER_OUT_EP = 0x01  # often 0x01

# DEBUG mode:
#   True  = assume Windows (simple Usb(VID, PID))
#   False = assume Raspberry Pi / Linux (Usb with in_ep / out_ep)
DEBUG = True


# =========================
# NETWORK / BROADCAST HELPERS
# =========================

def get_local_ip():
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception as e:
        print("[DMX][WARN] Could not auto-detect local IP, using 127.0.0.1:", e)
    return ip


def auto_detect_broadcast_ip():
    local_ip = get_local_ip()
    try:
        parts = local_ip.split(".")
        if len(parts) == 4:
            broadcast_ip = ".".join(parts[:3] + ["255"])
            print(f"[DMX] Auto-detected broadcast IP: {broadcast_ip} (from local IP {local_ip})")
            return broadcast_ip
    except Exception as e:
        print("[DMX][WARN] Failed to compute broadcast from local IP:", e)

    print("[DMX][WARN] Falling back to broadcast 255.255.255.255")
    return "255.255.255.255"


# =========================
# DMX CONTROLLER
# =========================

class DMXController:
    """
    DMX controller with continuous sending loop over Art-Net (broadcast).
    """

    def __init__(
        self,
        universe_size=DMX_UNIVERSE_SIZE,
        fps=DMX_FPS,
        universe=ARTNET_UNIVERSE,
    ):
        self.buffer = [0] * universe_size
        self.lock = threading.Lock()
        self.fps = fps
        self.running = True

        self.universe = universe
        self.target_ip = auto_detect_broadcast_ip()
        self.port = ARTNET_PORT

        # UDP socket with broadcast enabled
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        self.thread = threading.Thread(target=self._send_loop, daemon=True)
        self.thread.start()

    def _build_artnet_packet(self, dmx_data: bytes) -> bytes:
        packet = bytearray()
        packet.extend(b"Art-Net\x00")
        packet.extend((0x00, 0x50))      # OpDmx
        packet.extend((0x00, 14))        # ProtVer
        packet.append(0x00)              # Sequence
        packet.append(0x00)              # Physical
        packet.append(self.universe & 0xFF)
        packet.append((self.universe >> 8) & 0xFF)
        length = len(dmx_data)
        packet.append((length >> 8) & 0xFF)
        packet.append(length & 0xFF)
        packet.extend(dmx_data)
        return bytes(packet)

    def _send_loop(self):
        interval = 1.0 / self.fps
        while self.running:
            with self.lock:
                frame = bytes(self.buffer)
            try:
                artnet_packet = self._build_artnet_packet(frame)
                self.sock.sendto(artnet_packet, (self.target_ip, self.port))
            except Exception as e:
                print("[DMX][WARN] Failed to send Art-Net packet:", e)
            time.sleep(interval)

    def set_channel(self, channel, value):
        if channel < 1 or channel > len(self.buffer):
            return
        with self.lock:
            self.buffer[channel - 1] = max(0, min(255, value))

    def blackout(self):
        with self.lock:
            self.buffer = [0] * len(self.buffer)

    def stop(self):
        self.running = False
        try:
            self.thread.join()
        except RuntimeError:
            pass
        self.sock.close()


# =========================
# THERMAL PRINTER (DEBUG SWITCH)
# =========================

class ThermalPrinter:
    """
    ESC/POS thermal printer using python-escpos.

    DEBUG = True  -> Windows mode (simple constructor)
    DEBUG = False -> Raspberry Pi mode (needs endpoint numbers)
    """

    def __init__(self,
                 vid=PRINTER_VID,
                 pid=PRINTER_PID,
                 in_ep=PRINTER_IN_EP,
                 out_ep=PRINTER_OUT_EP):

        self.vid = vid
        self.pid = pid
        self.in_ep = in_ep
        self.out_ep = out_ep
        self.printer = None

        if DEBUG:
            print("[PRINTER] DEBUG mode ON → assuming Windows USB printer")
            self._open_windows_printer()
        else:
            print("[PRINTER] DEBUG mode OFF → assuming Raspberry Pi USB printer")
            self._open_linux_printer()

    def _open_windows_printer(self):
        try:
            self.printer = Usb(self.vid, self.pid, encoding="utf-8")
            print(f"[PRINTER] Connected (Windows mode) VID=0x{self.vid:04x} PID=0x{self.pid:04x}")
        except Exception as e:
            print("[PRINTER][ERROR] Could not open printer in Windows mode:", e)
            self.printer = None

    def _open_linux_printer(self):
        try:
            self.printer = Usb(self.vid,
                               self.pid,
                               in_ep=self.in_ep,
                               out_ep=self.out_ep,
                               encoding="utf-8")
            print(f"[PRINTER] Connected (Raspberry Pi mode) "
                  f"VID=0x{self.vid:04x} PID=0x{self.pid:04x} EP_IN=0x{self.in_ep:02x} EP_OUT=0x{self.out_ep:02x}")
        except Exception as e:
            print("[PRINTER][ERROR] Could not open printer in Raspberry Pi mode:", e)
            self.printer = None

    def _ensure_printer(self):
        if self.printer is not None:
            return True

        print("[PRINTER] Attempting reconnect...")
        if DEBUG:
            self._open_windows_printer()
        else:
            self._open_linux_printer()

        return self.printer is not None

    def print_text(self, actor, text):
        """
        Print the given actor's text on the thermal printer.
        'actor' is a label (e.g. "proposal", "lake", "votes", "lake vote").
        """
        header = f"— {actor.upper()} —\n"

        if not self._ensure_printer():
            print("[PRINTER][FALLBACK]", repr(header + text))
            return

        try:
            self.printer.text("\n")
            self.printer.text(header)
            self.printer.text(text)
            self.printer.text("\n\n")
            self.printer.cut()
            print(f"[PRINTER] Printed for {actor}")
        except Exception as e:
            print("[PRINTER][ERROR] Failed during print:", e)
            self.printer = None


dmx = DMXController()
printer = ThermalPrinter()


# =========================
# DRAMA STATE MACHINE
# =========================

DRAMA_STATE = {
    "running": False,
    "actors_order": list(DEFAULT_ACTORS_ORDER),
    "current_index": -1,  # index into actors_order
    "actor_data": {
        actor: {
            "text": None,
            "start_time": None,
            "printed": False,
        }
        for actor in DEFAULT_ACTORS_ORDER
    },
    "votes": {},           # actor -> vote text
    "voting_done": False,
    "lock": threading.Lock(),
}


def _init_actor_data(actors_order):
    return {
        actor: {
            "text": None,
            "start_time": None,
            "printed": False,
        }
        for actor in actors_order
    }


def _set_idle_scene():
    """Idle DMX look: everything off for now."""
    dmx.blackout()


def _set_active_actor_scene(actor):
    """
    Discussion DMX: this actor is 'on stage'.
    """
    _set_idle_scene()
    cfg = ACTOR_CONFIG.get(actor)
    if not cfg:
        print(f"[WARN] No DMX config for actor {actor}")
        return
    light_ch = cfg["light_channel"]
    motor_ch = cfg["motor_channel"]

    # Full light on, medium motor
    dmx.set_channel(light_ch, 255)
    dmx.set_channel(motor_ch, 128)


def _set_voting_scene(actor):
    """
    Voting DMX: slightly different look to signal voting phase.
    For now: dimmer light, motor lower intensity.
    """
    _set_idle_scene()
    cfg = ACTOR_CONFIG.get(actor)
    if not cfg:
        print(f"[WARN] No DMX config for actor {actor} (voting)")
        return
    light_ch = cfg["light_channel"]
    motor_ch = cfg["motor_channel"]

    # e.g. half light, lower motor
    dmx.set_channel(light_ch, 128)
    dmx.set_channel(motor_ch, 80)


def _drama_loop():
    """
    Background thread that runs:
      1) Discussion dramatization (DMX + per-actor speech print)
      2) Voting dramatization (DMX + per-actor vote print)
      3) Voting summary print
      4) Idle scene
    """
    # ---- DISCUSSION PHASE ----
    with DRAMA_STATE["lock"]:
        actors_order = list(DRAMA_STATE["actors_order"])
        DRAMA_STATE["current_index"] = 0

    for idx, actor in enumerate(actors_order):
        with DRAMA_STATE["lock"]:
            DRAMA_STATE["current_index"] = idx
            actor_entry = DRAMA_STATE["actor_data"][actor]
            actor_entry["start_time"] = time.time()
            actor_entry["printed"] = False

        print(f"[DRAMA] Discussion slot for actor={actor}")
        _set_active_actor_scene(actor)

        # Wait loop for this actor's discussion slot
        while True:
            with DRAMA_STATE["lock"]:
                actor_entry = DRAMA_STATE["actor_data"][actor]
                text = actor_entry["text"]
                start_time = actor_entry["start_time"]

            elapsed = time.time() - start_time if start_time else 0

            if text is not None and elapsed >= MIN_SLOT_DURATION:
                printer.print_text(actor, text)
                with DRAMA_STATE["lock"]:
                    DRAMA_STATE["actor_data"][actor]["printed"] = True
                print(f"[DRAMA] Finished discussion for {actor}, elapsed={elapsed:.1f}s")
                break

            time.sleep(0.5)

    print("[DRAMA] Discussion phase complete. Starting voting dramatization.")

    # ---- VOTING PHASE ----
    for actor in actors_order:
        print(f"[DRAMA] Voting slot for actor={actor}")
        _set_voting_scene(actor)
        slot_start = time.time()

        while True:
            with DRAMA_STATE["lock"]:
                vote = DRAMA_STATE["votes"].get(actor)
            elapsed = time.time() - slot_start

            if vote is not None and elapsed >= MIN_SLOT_DURATION:
                vote_text = f"{actor.upper()}: {vote}"
                printer.print_text(f"{actor} vote", vote_text)
                print(f"[DRAMA] Finished voting dramatization for {actor}, elapsed={elapsed:.1f}s")
                break

            time.sleep(0.5)

    # ---- SUMMARY + IDLE ----
    print("[DRAMA] All actors done voting. Printing voting summary and going idle.")

    with DRAMA_STATE["lock"]:
        votes_copy = dict(DRAMA_STATE["votes"])
        DRAMA_STATE["running"] = False
        DRAMA_STATE["current_index"] = -1
        DRAMA_STATE["voting_done"] = True

    if votes_copy:
        summary_lines = ["VOTING SUMMARY", ""]
        for actor in actors_order:
            v = votes_copy.get(actor)
            if v is not None:
                summary_lines.append(f"{actor.upper()}: {v}")
        summary_text = "\n".join(summary_lines) + "\n"
        printer.print_text("votes", summary_text)
        print("[DRAMA] Printed voting summary")

    _set_idle_scene()
    print("[DRAMA] Idle scene set.")


def start_drama_run(prompt, proposer="human", order=None):
    """
    Start a full run:
      - Reset state
      - Print proposal
      - Kick AI parliament
      - Start drama loop (discussion + voting)
    """
    # Decide actors_order for this run
    if isinstance(order, list) and order:
        actors_order = [a for a in order if a in ACTOR_CONFIG]
        if not actors_order:
            actors_order = list(DEFAULT_ACTORS_ORDER)
    else:
        actors_order = list(DEFAULT_ACTORS_ORDER)

    with DRAMA_STATE["lock"]:
        DRAMA_STATE["actors_order"] = actors_order
        DRAMA_STATE["actor_data"] = _init_actor_data(actors_order)
        DRAMA_STATE["current_index"] = -1
        DRAMA_STATE["running"] = True
        DRAMA_STATE["votes"] = {}
        DRAMA_STATE["voting_done"] = False

    _set_idle_scene()

    # Print proposal at the beginning
    try:
        proposal_text = f"Proposal by {proposer}:\n\n{prompt}"
        printer.print_text("proposal", proposal_text)
        print("[DRAMA] Printed proposal")
    except Exception as e:
        print("[PRINTER][ERROR] Could not print proposal:", e)

    # Kick off AI parliament in another thread so we don't block
    def ai_starter():
        try:
            payload = {
                "proposal": prompt,
                "proposer": proposer,
                "order": actors_order,
            }
            resp = requests.post(
                f"{AI_SERVER_BASE_URL}/parliament",
                json=payload,
                timeout=30,
            )
            print("[DRAMA] AI parliament start response:", resp.status_code)
        except Exception as e:
            print("[ERROR] Failed to start parliament on AI server:", e)

    threading.Thread(target=ai_starter, daemon=True).start()

    # Start drama loop in background
    t = threading.Thread(target=_drama_loop, daemon=True)
    t.start()


# =========================
# HTTP ENDPOINTS
# =========================

@app.route("/start", methods=["POST"])
def start_endpoint():
    data = request.get_json(force=True)
    prompt = data.get("prompt")
    proposer = data.get("proposer", "human")
    order = data.get("order")

    if not prompt:
        return jsonify({"error": "Missing 'prompt'"}), 400

    with DRAMA_STATE["lock"]:
        if DRAMA_STATE["running"]:
            return jsonify({"error": "Drama already running"}), 400

    start_drama_run(prompt, proposer, order)
    return jsonify({"status": "started"})


@app.route("/actor_text", methods=["POST"])
def actor_text():
    data = request.get_json(force=True)
    actor = data.get("actor")
    text = data.get("text")

    if not actor or not text:
        return jsonify({"error": "Both 'actor' and 'text' required"}), 400

    with DRAMA_STATE["lock"]:
        if actor not in DRAMA_STATE["actor_data"]:
            return jsonify({"error": f"Unknown or inactive actor '{actor}'"}), 400
        DRAMA_STATE["actor_data"][actor]["text"] = text

    print(f"[DRAMA] Received text for actor={actor}, len={len(text)}")
    return jsonify({"status": "ok"})


@app.route("/actor_vote", methods=["POST"])
def actor_vote():
    data = request.get_json(force=True)
    actor = data.get("actor")
    vote = data.get("vote")

    if not actor or vote is None:
        return jsonify({"error": "Both 'actor' and 'vote' required"}), 400

    with DRAMA_STATE["lock"]:
        DRAMA_STATE["votes"][actor] = vote
        print(f"[DRAMA] Received vote: {actor} -> {vote}")

    return jsonify({"status": "ok"})


@app.route("/status", methods=["GET"])
def status():
    with DRAMA_STATE["lock"]:
        current_index = DRAMA_STATE["current_index"]
        running = DRAMA_STATE["running"]
        actors_order = list(DRAMA_STATE["actors_order"])
        actor_data_copy = {
            actor: {
                "has_text": (info["text"] is not None),
                "start_time": info["start_time"],
                "printed": info["printed"],
            }
            for actor, info in DRAMA_STATE["actor_data"].items()
        }
        votes_copy = dict(DRAMA_STATE["votes"])
        voting_done = DRAMA_STATE["voting_done"]

    current_actor = (
        actors_order[current_index]
        if (running and current_index >= 0 and current_index < len(actors_order))
        else None
    )

    return jsonify({
        "running": running,
        "current_actor": current_actor,
        "actors_order": actors_order,
        "actor_data": actor_data_copy,
        "votes": votes_copy,
        "voting_done": voting_done,
    })


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "dramatization_server_alive"})


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=8001, debug=True)
    finally:
        dmx.stop()
