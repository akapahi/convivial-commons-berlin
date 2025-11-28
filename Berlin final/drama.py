from flask import Flask, request, jsonify
import threading
import time
import requests
import socket
import os
import json

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

# Timing (mutable via /timing)
THINKING_TIME = 7.0     # seconds min while "waiting for AI" per actor
READING_TIME  = 20.0    # seconds after print to let people read
VOTING_TIME   = 30.0    # seconds to vibrate/light all actors during voting

# === THERMAL PRINTER CONFIG (USB ESC/POS) ===
PRINTER_VID = 0x0483
PRINTER_PID = 0x5743
PRINTER_IN_EP = 0x82   # often 0x82
PRINTER_OUT_EP = 0x01  # often 0x01

# DEBUG mode:
#   True  = assume Windows (simple Usb(VID, PID))
#   False = assume Raspberry Pi / Linux (Usb with in_ep / out_ep)
DEBUG = True

# Path to proposals.json (same folder as this script)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROPOSALS_PATH = os.path.join(BASE_DIR, "proposals.json")


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
        'actor' is a label (e.g. "proposal", "lake", "votes").
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


def _set_voting_scene_all(actors_order):
    """
    Voting DMX: all actors on at once.
    """
    _set_idle_scene()
    for actor in actors_order:
        cfg = ACTOR_CONFIG.get(actor)
        if not cfg:
            print(f"[WARN] No DMX config for actor {actor} (voting)")
            continue
        light_ch = cfg["light_channel"]
        motor_ch = cfg["motor_channel"]
        # e.g. half light, lower motor
        dmx.set_channel(light_ch, 128)
        dmx.set_channel(motor_ch, 80)


def _drama_loop():
    """
    Background thread that runs:
      1) Discussion dramatization (DMX + per-actor speech print)
      2) Voting dramatization (DMX for all + summary print)
      3) Idle scene
    """
    global THINKING_TIME, READING_TIME, VOTING_TIME

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

        # THINKING PHASE: wait at least THINKING_TIME seconds AND until text arrives
        slot_start = time.time()
        while True:
            with DRAMA_STATE["lock"]:
                actor_entry = DRAMA_STATE["actor_data"][actor]
                text = actor_entry["text"]

            elapsed = time.time() - slot_start

            if text is not None and elapsed >= THINKING_TIME:
                break

            time.sleep(0.2)

        # PRINT the text
        printer.print_text(actor, text)
        with DRAMA_STATE["lock"]:
            DRAMA_STATE["actor_data"][actor]["printed"] = True
        print(f"[DRAMA] Printed discussion for {actor} after {elapsed:.1f}s")

        # READING PHASE: keep scene on for READING_TIME seconds
        read_start = time.time()
        while time.time() - read_start < READING_TIME:
            time.sleep(0.2)

    print("[DRAMA] Discussion phase complete. Starting voting dramatization.")

    # ---- VOTING PHASE (all actors at once) ----
    _set_voting_scene_all(actors_order)

    voting_start = time.time()
    while True:
        with DRAMA_STATE["lock"]:
            have_any_votes = bool(DRAMA_STATE["votes"])
        elapsed = time.time() - voting_start

        # We want at least VOTING_TIME seconds AND to have received votes
        if elapsed >= VOTING_TIME and have_any_votes:
            break

        # No timeout: keep waiting as long as needed
        time.sleep(0.2)

    print("[DRAMA] Voting time + votes condition met. Printing voting summary.")

    with DRAMA_STATE["lock"]:
        votes_copy = dict(DRAMA_STATE["votes"])
        DRAMA_STATE["running"] = False
        DRAMA_STATE["current_index"] = -1
        DRAMA_STATE["voting_done"] = True

    # Build and print summary (whatever votes we have)
    summary_lines = ["VOTING SUMMARY", ""]
    if votes_copy:
        for actor in actors_order:
            v = votes_copy.get(actor)
            if v is not None:
                summary_lines.append(f"{actor.upper()}: {v}")
    else:
        summary_lines.append("(no votes received)")
    summary_text = "\n".join(summary_lines) + "\n"

    printer.print_text("votes", summary_text)
    print("[DRAMA] Printed voting summary")

    # READING PHASE for votes
    read_start = time.time()
    while time.time() - read_start < READING_TIME:
        time.sleep(0.2)

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
# FRONTEND ROUTES (UI)
# =========================

@app.route("/")
def index():
    """
    Simple frontend UI:
    - Fetches /proposals
    - Renders one button per proposal
    - On click, calls POST /start with that proposal
    - Has controls at the bottom to adjust THINKING/READING/VOTING times
    """
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Convivial Commons Parliament</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    :root {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0b0c10;
      color: #f5f5f5;
    }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: stretch;
      justify-content: center;
    }
    .container {
      max-width: 900px;
      width: 100%;
      padding: 24px;
      box-sizing: border-box;
    }
    h1 {
      margin-top: 0;
      font-size: 1.8rem;
    }
    p {
      line-height: 1.4;
      color: #c7c7c7;
    }
    .proposals-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 16px;
      margin-top: 20px;
    }
    .proposal-card {
      border-radius: 12px;
      padding: 16px;
      background: #15181f;
      border: 1px solid #262a33;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 10px;
    }
    .proposal-title {
      font-weight: 600;
      margin-bottom: 4px;
    }
    .proposal-meta {
      font-size: 0.8rem;
      opacity: 0.8;
      margin-bottom: 6px;
    }
    .proposal-text {
      font-size: 0.9rem;
      max-height: 6rem;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    button {
      border-radius: 999px;
      border: none;
      padding: 8px 16px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      align-self: flex-start;
      background: #f5f5f5;
      color: #111;
      transition: transform 0.05s ease, box-shadow 0.05s ease, opacity 0.15s;
      box-shadow: 0 4px 10px rgba(0,0,0,0.3);
    }
    button:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 14px rgba(0,0,0,0.4);
    }
    button:active {
      transform: translateY(0);
      box-shadow: 0 2px 6px rgba(0,0,0,0.5);
    }
    button:disabled {
      opacity: 0.5;
      cursor: default;
      transform: none;
      box-shadow: none;
    }
    .status {
      margin-top: 20px;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 0.9rem;
      background: #11141b;
      border: 1px solid #262a33;
      white-space: pre-line;
      max-height: 200px;
      overflow-y: auto;
    }
    .status span.label {
      font-weight: 600;
      text-transform: uppercase;
      font-size: 0.75rem;
      letter-spacing: 0.06em;
      opacity: 0.8;
    }
    .status-ok { color: #8ef59b; }
    .status-error { color: #ff7f7f; }

    .timing-card {
      margin-top: 24px;
      padding: 16px;
      border-radius: 12px;
      background: #15181f;
      border: 1px solid #262a33;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .timing-card h2 {
      font-size: 1rem;
      margin: 0 0 4px 0;
    }
    .timing-row {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 0.9rem;
    }
    .timing-row label {
      width: 140px;
    }
    .timing-row input[type="number"] {
      width: 80px;
      padding: 4px 6px;
      border-radius: 6px;
      border: 1px solid #444a55;
      background: #0b0c10;
      color: #f5f5f5;
    }
    .timing-row span.unit {
      font-size: 0.85rem;
      opacity: 0.7;
    }
    .timing-actions {
      margin-top: 8px;
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .timing-note {
      font-size: 0.75rem;
      opacity: 0.7;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>Convivial Commons Parliament</h1>
    <p>
      Choose a proposal to trigger a full parliament run on this dramatization server.
      Each card below comes from <code>proposals.json</code>.
    </p>

    <div id="proposals" class="proposals-list">
      <!-- proposals will be injected here -->
    </div>

    <div id="status" class="status">
      <span class="label">Status</span>
      <div id="status-text">Loading proposals…</div>
    </div>

    <div class="timing-card">
      <h2>Timing Controls</h2>
      <div class="timing-row">
        <label for="thinking-input">Thinking time</label>
        <input id="thinking-input" type="number" min="0" step="1" />
        <span class="unit">seconds (min before speech prints)</span>
      </div>
      <div class="timing-row">
        <label for="reading-input">Reading time</label>
        <input id="reading-input" type="number" min="0" step="1" />
        <span class="unit">seconds (lights stay on after print)</span>
      </div>
      <div class="timing-row">
        <label for="voting-input">Voting time</label>
        <input id="voting-input" type="number" min="0" step="1" />
        <span class="unit">seconds (all actors on during voting)</span>
      </div>
      <div class="timing-actions">
        <button id="save-timing-btn">Save timing</button>
        <span class="timing-note">Changes affect the next run (not the one currently playing).</span>
      </div>
    </div>
  </div>

  <script>
    const proposalsContainer = document.getElementById("proposals");
    const statusBox = document.getElementById("status-text");

    const thinkingInput = document.getElementById("thinking-input");
    const readingInput = document.getElementById("reading-input");
    const votingInput = document.getElementById("voting-input");
    const saveTimingBtn = document.getElementById("save-timing-btn");

    function setStatus(msg, type = "info") {
      statusBox.textContent = msg;
      statusBox.classList.remove("status-ok", "status-error");
      if (type === "ok") statusBox.classList.add("status-ok");
      if (type === "error") statusBox.classList.add("status-error");
    }

    async function loadProposals() {
      try {
        const res = await fetch("/proposals");
        if (!res.ok) {
          throw new Error("HTTP " + res.status);
        }
        const proposals = await res.json();
        renderProposals(proposals);
        setStatus("Loaded " + proposals.length + " proposals. Click one to start.");
      } catch (err) {
        console.error(err);
        setStatus("Failed to load proposals: " + err.message, "error");
      }
    }

    function renderProposals(proposals) {
      proposalsContainer.innerHTML = "";

      if (!Array.isArray(proposals) || proposals.length === 0) {
        proposalsContainer.innerHTML = "<p>No proposals found.</p>";
        return;
      }

      proposals.forEach((p, index) => {
        const card = document.createElement("div");
        card.className = "proposal-card";

        const title = document.createElement("div");
        title.className = "proposal-title";
        title.textContent = p.title || ("Proposal " + (index + 1));

        const meta = document.createElement("div");
        meta.className = "proposal-meta";
        const proposer = p.proposer || "human";
        const order = Array.isArray(p.order) ? p.order.join(", ") : "(default order)";
        meta.textContent = "Proposer: " + proposer + " • Order: " + order;

        const text = document.createElement("div");
        text.className = "proposal-text";
        text.textContent = p.prompt || "";

        const btn = document.createElement("button");
        btn.textContent = "Start Parliament";
        btn.addEventListener("click", () => {
          triggerParliament(p, btn);
        });

        card.appendChild(title);
        card.appendChild(meta);
        card.appendChild(text);
        card.appendChild(btn);
        proposalsContainer.appendChild(card);
      });
    }

    async function triggerParliament(proposal, buttonEl) {
      const payload = {
        prompt: proposal.prompt,
        proposer: proposal.proposer || "human",
        order: proposal.order || undefined,
      };

      if (!payload.prompt) {
        setStatus("Selected proposal has no prompt text.", "error");
        return;
      }

      const label = proposal.title || (payload.prompt.slice(0, 40) + "…");
      setStatus("Starting parliament for: " + label);
      buttonEl.disabled = true;

      try {
        const res = await fetch("/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        if (!res.ok) {
          const errText = await res.text();
          setStatus("Server returned " + res.status + ": " + errText, "error");
          buttonEl.disabled = false;
          return;
        }

        const data = await res.json();
        console.log("Drama /start response:", data);
        setStatus("Parliament started. Check lights, motors, and printer. :)", "ok");
      } catch (err) {
        console.error(err);
        setStatus("Failed to contact server: " + err.message, "error");
        buttonEl.disabled = false;
      }
    }

    async function loadTiming() {
      try {
        const res = await fetch("/timing");
        if (!res.ok) {
          throw new Error("HTTP " + res.status);
        }
        const data = await res.json();
        thinkingInput.value = data.thinking_time.toFixed(0);
        readingInput.value = data.reading_time.toFixed(0);
        votingInput.value = data.voting_time.toFixed(0);
      } catch (err) {
        console.error(err);
        setStatus("Failed to load timing: " + err.message, "error");
      }
    }

    async function saveTiming() {
      const thinking = parseFloat(thinkingInput.value);
      const reading = parseFloat(readingInput.value);
      const voting = parseFloat(votingInput.value);

      if (isNaN(thinking) || isNaN(reading) || isNaN(voting)) {
        setStatus("Please enter valid numbers for timing.", "error");
        return;
      }
      if (thinking < 0 || reading < 0 || voting < 0) {
        setStatus("Timing values cannot be negative.", "error");
        return;
      }

      saveTimingBtn.disabled = true;
      try {
        const res = await fetch("/timing", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            thinking_time: thinking,
            reading_time: reading,
            voting_time: voting
          })
        });
        if (!res.ok) {
          const errText = await res.text();
          setStatus("Failed to save timing: " + errText, "error");
          saveTimingBtn.disabled = false;
          return;
        }
        const data = await res.json();
        console.log("Timing updated:", data);
        setStatus("Timing updated. New runs will use these values.", "ok");
      } catch (err) {
        console.error(err);
        setStatus("Failed to save timing: " + err.message, "error");
      } finally {
        saveTimingBtn.disabled = false;
      }
    }

    saveTimingBtn.addEventListener("click", saveTiming);

    loadProposals();
    loadTiming();
  </script>
</body>
</html>
    """
    return html, 200, {"Content-Type": "text/html"}


@app.route("/proposals", methods=["GET"])
def get_proposals():
    """
    Return the proposals loaded from proposals.json as JSON array.
    """
    try:
        with open(PROPOSALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except FileNotFoundError:
        return jsonify([]), 200
    except Exception as e:
        return jsonify({"error": f"Could not read proposals.json: {e}"}), 500


# =========================
# API ENDPOINTS (CONTROL)
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
    """
    Called by AI server during discussion, one actor at a time:
    {
      "actor": "lake",
      "text": "..."
    }
    """
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
    """
    AI server sends ALL votes at once after voting is done.

    Expected body:
    {
      "votes": {
        "lake": "YES ...",
        "fungi": "NO ...",
        ...
      }
    }
    """
    data = request.get_json(force=True)
    votes = data.get("votes")

    if not isinstance(votes, dict) or not votes:
      return jsonify({"error": "Field 'votes' must be a non-empty object {actor: vote}"}), 400

    with DRAMA_STATE["lock"]:
        for actor, vote in votes.items():
            DRAMA_STATE["votes"][actor] = vote
            print(f"[DRAMA] Stored vote: {actor} -> {vote}")

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


@app.route("/timing", methods=["GET", "POST"])
def timing():
    """
    GET  -> return current timing values
    POST -> update timing values
      {
        "thinking_time": <seconds>,
        "reading_time": <seconds>,
        "voting_time": <seconds>
      }
    """
    global THINKING_TIME, READING_TIME, VOTING_TIME

    if request.method == "GET":
        return jsonify({
            "thinking_time": THINKING_TIME,
            "reading_time": READING_TIME,
            "voting_time": VOTING_TIME,
        })

    # POST
    data = request.get_json(force=True)
    thinking = data.get("thinking_time")
    reading = data.get("reading_time")
    voting = data.get("voting_time")

    try:
        if thinking is not None:
            thinking = float(thinking)
            if thinking < 0:
                raise ValueError("thinking_time must be >= 0")
            THINKING_TIME = thinking

        if reading is not None:
            reading = float(reading)
            if reading < 0:
                raise ValueError("reading_time must be >= 0")
            READING_TIME = reading

        if voting is not None:
            voting = float(voting)
            if voting < 0:
                raise ValueError("voting_time must be >= 0")
            VOTING_TIME = voting

    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    print(f"[TIMING] Updated: thinking={THINKING_TIME}, reading={READING_TIME}, voting={VOTING_TIME}")
    return jsonify({
        "thinking_time": THINKING_TIME,
        "reading_time": READING_TIME,
        "voting_time": VOTING_TIME,
        "status": "updated",
    })


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "dramatization_server_alive"})


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=8001, debug=True)
    finally:
        dmx.stop()
