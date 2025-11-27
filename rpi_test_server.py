from flask import Flask, jsonify, request
import socket
import threading
import atexit
import sys
import traceback

app = Flask(__name__)

# -------- CONFIG --------
TARGET = None
UDP_PORT = 6454
SOCKET_TIMEOUT = 1.0
# ------------------------

_state_lock = threading.Lock()
_is_started = False


# ------------------ Network Helpers ------------------

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return None


def make_broadcast_from_ip(ip_addr: str) -> str:
    try:
        parts = ip_addr.split(".")
        if len(parts) == 4:
            parts[-1] = "255"
            return ".".join(parts)
    except Exception:
        pass
    return "192.168.1.255"


def get_local_broadcast():
    local_ip = get_local_ip()
    if local_ip:
        return make_broadcast_from_ip(local_ip)
    return "192.168.1.255"


def build_socket(broadcast=False):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(SOCKET_TIMEOUT)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


def send_udp(payload: bytes, target: str, port: int = UDP_PORT):
    """
    Send a single UDP payload.
    No keepalive, no looping.
    """
    if target is None:
        target_ip = get_local_broadcast()
        broadcast_flag = True
    elif isinstance(target, str) and target.lower() == "broadcast":
        target_ip = get_local_broadcast()
        broadcast_flag = True
    else:
        target_ip = target
        if not isinstance(target_ip, str):
            target_ip = get_local_broadcast()
            broadcast_flag = True
        else:
            broadcast_flag = target_ip.endswith(".255")

    sock = build_socket(broadcast_flag)
    try:
        app.logger.debug("send_udp: to %s:%d  (broadcast=%s)", target_ip, port, broadcast_flag)
        sock.sendto(payload, (target_ip, port))
    finally:
        sock.close()


# ------------------ Flask Endpoints ------------------

@app.route("/start", methods=["POST"])
def start_cmd():
    global _is_started
    with _state_lock:
        try:
            send_udp(b'1', TARGET, UDP_PORT)
        except Exception as e:
            app.logger.exception("Failed to send start UDP")
            return jsonify({"ok": False, "error": str(e)}), 500

        _is_started = True
    return jsonify({"ok": True, "action": "started", "target": TARGET})


@app.route("/stop", methods=["POST"])
def stop_cmd():
    global _is_started
    with _state_lock:
        try:
            send_udp(b'0', TARGET, UDP_PORT)
        except Exception as e:
            app.logger.exception("Failed to send stop UDP")
            return jsonify({"ok": False, "error": str(e)}), 500

        _is_started = False
    return jsonify({"ok": True, "action": "stopped", "target": TARGET})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "started": _is_started,
        "target": TARGET,
        "port": UDP_PORT
    })


@app.route("/")
def index():
    return jsonify({"ok": True, "endpoints": ["/start (POST)", "/stop (POST)", "/status (GET)"]})


# ------------------ Main ------------------

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        supplied = sys.argv[1].strip()
        if supplied.lower() == "broadcast":
            TARGET = get_local_broadcast()
        else:
            TARGET = make_broadcast_from_ip(supplied)
    else:
        TARGET = get_local_broadcast()

    if len(sys.argv) >= 3:
        UDP_PORT = int(sys.argv[2])

    app.logger.info("Starting server. TARGET = %s  PORT = %d", TARGET, UDP_PORT)
    local_ip = get_local_ip()
    app.logger.info("Local IP detected = %s  Broadcast = %s", local_ip, get_local_broadcast())

    app.run(host="0.0.0.0", port=5000, threaded=True)
