# server.py (patched to avoid None target in send_udp)
from flask import Flask, jsonify, request
import socket
import threading
import atexit
import sys
import traceback

app = Flask(__name__)

# -------- CONFIG --------
# If TARGET is None, we will auto-detect and replace the last octet with 255
TARGET = None
UDP_PORT = 6454
KEEPALIVE_INTERVAL = 10
SOCKET_TIMEOUT = 1.0
# ------------------------

_keepalive_thread = None
_keepalive_stop = threading.Event()
_state_lock = threading.Lock()
_is_started = False

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
    Send a UDP payload to target:port.
    If target is None or 'broadcast' -> compute broadcast address.
    This function is defensive: it will never call .endswith on None.
    """
    # Defensive normalization
    if target is None:
        app.logger.debug("send_udp: target is None -> using local broadcast")
        target_ip = get_local_broadcast()
        broadcast_flag = True
    elif isinstance(target, str) and target.lower() == "broadcast":
        target_ip = get_local_broadcast()
        broadcast_flag = True
    else:
        target_ip = target
        # If target_ip ended up None or non-string, fall back
        if not isinstance(target_ip, str):
            app.logger.debug("send_udp: target not a string (%r) -> using local broadcast", target_ip)
            target_ip = get_local_broadcast()
            broadcast_flag = True
        else:
            broadcast_flag = target_ip.endswith(".255")

    sock = build_socket(broadcast_flag)
    try:
        app.logger.debug("send_udp: sending to %s:%d (broadcast=%s) payload=%r", target_ip, port, broadcast_flag, payload)
        sock.sendto(payload, (target_ip, port))
    finally:
        sock.close()

def _keepalive_loop(target, port, interval):
    payload = b'1'
    while not _keepalive_stop.wait(0):
        try:
            send_udp(payload, target, port)
        except Exception as e:
            # log full traceback so you can see what went wrong
            app.logger.error("Keepalive send failed: %s\n%s", e, traceback.format_exc())
        if _keepalive_stop.wait(interval):
            break

def start_keepalive(target=TARGET, port=UDP_PORT, interval=KEEPALIVE_INTERVAL):
    global _keepalive_thread, _keepalive_stop
    stop_keepalive()
    _keepalive_stop.clear()

    # Normalize target for the thread: if None -> use computed broadcast
    if target is None:
        target = get_local_broadcast()
        app.logger.debug("start_keepalive: target was None, using %s", target)

    thread = threading.Thread(target=_keepalive_loop, args=(target, port, interval), daemon=True)
    _keepalive_thread = thread
    thread.start()

def stop_keepalive():
    global _keepalive_thread, _keepalive_stop
    if _keepalive_thread is None:
        return
    _keepalive_stop.set()
    _keepalive_thread.join(timeout=1.0)
    _keepalive_thread = None
    _keepalive_stop.clear()

@app.route("/start", methods=["POST"])
def start():
    global _is_started
    with _state_lock:
        try:
            send_udp(b'1', TARGET, UDP_PORT)
        except Exception as e:
            app.logger.exception("Failed to send start UDP")
            return jsonify({"ok": False, "error": str(e)}), 500
        start_keepalive(TARGET, UDP_PORT, KEEPALIVE_INTERVAL)
        _is_started = True
    return jsonify({"ok": True, "action": "started", "target": TARGET})

@app.route("/stop", methods=["POST"])
def stop():
    global _is_started
    with _state_lock:
        try:
            send_udp(b'0', TARGET, UDP_PORT)
        except Exception as e:
            app.logger.exception("Failed to send stop UDP")
            return jsonify({"ok": False, "error": str(e)}), 500
        stop_keepalive()
        _is_started = False
    return jsonify({"ok": True, "action": "stopped", "target": TARGET})

@app.route("/status", methods=["GET"])
def status():
    with _state_lock:
        return jsonify({
            "started": _is_started,
            "target": TARGET,
            "port": UDP_PORT,
            "keepalive_interval": KEEPALIVE_INTERVAL,
            "keepalive_running": _keepalive_thread is not None
        })

@app.route("/")
def index():
    return jsonify({"ok": True, "endpoints": ["/start (POST)", "/stop (POST)", "/status (GET)"]})

def _graceful_shutdown():
    try:
        stop_keepalive()
    except Exception:
        pass

atexit.register(_graceful_shutdown)

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

    app.logger.info("Starting server. TARGET = %s PORT = %d", TARGET, UDP_PORT)
    # also print detected local ip + broadcast for clarity
    local_ip = get_local_ip()
    app.logger.info("Local IP detected = %s  Broadcast = %s", local_ip, get_local_broadcast())

    app.run(host="0.0.0.0", port=5000, threaded=True)
