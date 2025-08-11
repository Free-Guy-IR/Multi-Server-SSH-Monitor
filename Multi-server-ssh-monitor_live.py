#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi‑Server SSH Monitor — Final (add/remove servers + persistence + global terminal + live cmd)
-----------------------------------------------------------------------------------------------
• Multi-server SSH monitoring (CPU, RAM, Disk, Net RX/TX, Uptime, Load)
• Grafana-like dark UI with gauges, charts, and management modal
• Live command (per server) via SSE + normal exec
• Global Terminal: run one command on ALL servers concurrently (with live output + interactive input)
• Persistence: time-series saved to ./persist/<server>.ndjson and reloaded on startup
• Tehran clock (Asia/Tehran) in header, independent of system timezone
• Add/Remove servers from UI — updates servers.json and monitor live

Run:
  pip install Flask==3.0.0 paramiko==3.4.0
  python Multi-server-ssh-monitor_final.py
  open http://127.0.0.1:8000
"""
import json
import os
import time
import threading
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, Dict, Any, List, Tuple

from flask import Flask, jsonify, request, Response, render_template_string, abort

try:
    import paramiko  # type: ignore
except Exception as e:
    raise SystemExit("\n[!] Paramiko is required. Install with: pip install paramiko\n") from e


# ----------------------------- Config & State -----------------------------

PERSIST_DIR = os.path.join(os.path.dirname(__file__), "persist")
os.makedirs(PERSIST_DIR, exist_ok=True)


@dataclass
class ServerConfig:
    name: str
    host: str
    port: int = 22
    username: str = "root"
    password: Optional[str] = None
    key_path: Optional[str] = None


@dataclass
class Sample:
    t: float
    cpu: float
    ram: float
    disk: float
    rx_rate: float
    tx_rate: float
    rx_total: int
    tx_total: int
    load1: float
    uptime_s: int


@dataclass
class ServerState:
    cfg: ServerConfig
    ssh: Optional[paramiko.SSHClient] = None
    connected: bool = False

    # For CPU/net deltas
    _prev_cpu: Optional[Tuple[int, int]] = None  # (idle+io, total)
    _prev_net: Optional[Tuple[int, int]] = None  # (rx_bytes, tx_bytes)

    # Rolling history
    history: deque = field(default_factory=lambda: deque(maxlen=3600))  # last ~hour

    last: Optional[Sample] = None
    last_err: Optional[str] = None


class Monitor:
    def __init__(self, cfg_path: str):
        if not os.path.exists(cfg_path):
            raise SystemExit("[!] servers.json not found next to the script.")

        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        self.cfg_path = cfg_path
        self.interval = int(raw.get("poll_interval", 2))
        self.admin_token: Optional[str] = raw.get("admin_token")
        self.states: List[ServerState] = []

        for s in raw.get("servers", []):
            sc = ServerConfig(
                name=s.get("name") or s["host"],
                host=s["host"],
                port=int(s.get("port", 22)),
                username=s.get("username", "root"),
                password=s.get("password"),
                key_path=s.get("key_path"),
            )
            self.states.append(ServerState(cfg=sc))

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._lock = threading.Lock()

    # ---------------------- SSH Connect / Exec Helpers ----------------------

    def start(self) -> None:
        print(f"[DEBUG] Starting monitor for {len(self.states)} servers, interval={self.interval}s")
        for st in self.states:
            self._connect(st)
            self._load_persist(st)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for st in self.states:
            try:
                if st.ssh:
                    st.ssh.close()
            except Exception:
                pass

    def _connect(self, st: ServerState) -> None:
        print(f"[DEBUG] Connecting to {st.cfg.name} ({st.cfg.host})...")
        if st.ssh:
            try:
                st.ssh.close()
            except Exception:
                pass
            st.ssh = None

        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            if st.cfg.key_path:
                pkey = paramiko.RSAKey.from_private_key_file(st.cfg.key_path)
                cli.connect(
                    hostname=st.cfg.host,
                    port=st.cfg.port,
                    username=st.cfg.username,
                    pkey=pkey,
                    banner_timeout=10,
                    auth_timeout=10,
                    timeout=10,
                )
            else:
                cli.connect(
                    hostname=st.cfg.host,
                    port=st.cfg.port,
                    username=st.cfg.username,
                    password=st.cfg.password,
                    banner_timeout=10,
                    auth_timeout=10,
                    timeout=10,
                )
            st.ssh = cli
            st.connected = True
            st.last_err = None
            print(f"[OK] ✅ Connected to {st.cfg.name} ({st.cfg.host})")
        except Exception as e:
            st.connected = False
            st.ssh = None
            st.last_err = str(e)
            print(f"[ERR] ❌ Failed to connect {st.cfg.name} ({st.cfg.host}): {e}")

    def _exec(self, st: ServerState, cmd: str, timeout: int = 8) -> Tuple[str, str, int]:
        if not st.ssh:
            raise RuntimeError("SSH not connected")
        stdin, stdout, stderr = st.ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        rc = stdout.channel.recv_exit_status()
        return out, err, rc

    # ----------------------------- Persistence -----------------------------

    def _persist_path(self, st: ServerState) -> str:
        safe = "".join(c for c in st.cfg.name if c.isalnum() or c in ("-", "_"))
        return os.path.join(PERSIST_DIR, f"{safe}.ndjson")

    def _load_persist(self, st: ServerState, seconds: int = 3600) -> None:
        """Load last <=seconds of data from NDJSON file."""
        try:
            pth = self._persist_path(st)
            if not os.path.exists(pth):
                return
            cutoff = time.time() - seconds
            with open(pth, "r", encoding="utf-8") as f:
                for ln in f:
                    try:
                        d = json.loads(ln)
                        if d.get("t", 0) >= cutoff:
                            smp = Sample(**d)
                            st.history.append(smp)
                            st.last = smp
                    except Exception:
                        continue
            print(f"[PERSIST] Loaded {len(st.history)} samples for {st.cfg.name}")
        except Exception as e:
            print(f"[PERSIST] load error for {st.cfg.name}: {e}")

    def _append_persist(self, st: ServerState, smp: Sample) -> None:
        try:
            pth = self._persist_path(st)
            with open(pth, "a", encoding="utf-8") as f:
                f.write(json.dumps(vars(smp), ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[PERSIST] append error for {st.cfg.name}: {e}")

    # ----------------------------- Polling Loop -----------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            for st in list(self.states):
                try:
                    if not st.connected or not st.ssh:
                        self._connect(st)
                        if not st.connected:
                            continue
                    self._poll_server(st)
                except Exception as e:
                    st.last_err = str(e)
                    st.connected = False
                    try:
                        if st.ssh:
                            st.ssh.close()
                    except Exception:
                        pass
                    st.ssh = None
            dt = time.time() - t0
            time.sleep(max(0.0, self.interval - dt))

    def _poll_server(self, st: ServerState) -> None:
        cmd = r"""sh -c '
          head -n1 /proc/stat;
          free -b | sed -n "2p";
          df -PB1 / | tail -n1;
          cat /proc/net/dev;
          cat /proc/uptime;
          cat /proc/loadavg;
        '"""
        out, err, rc = self._exec(st, cmd)
        if rc != 0:
            raise RuntimeError(f"remote rc={rc} err={err.strip()}")

        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        cpu_line = lines[0].split()
        cpu_nums = list(map(int, cpu_line[1:11]))
        idle_io = cpu_nums[3] + cpu_nums[4]
        total = sum(cpu_nums)

        cpu_pct = 0.0
        if st._prev_cpu is not None:
            prev_idle_io, prev_total = st._prev_cpu
            d_idle_io = idle_io - prev_idle_io
            d_total = total - prev_total
            if d_total > 0:
                cpu_pct = 100.0 * (1.0 - (d_idle_io / d_total))
        st._prev_cpu = (idle_io, total)

        mem_line = lines[1].split()
        try:
            mem_total = float(mem_line[1])
            mem_available = float(mem_line[6]) if len(mem_line) >= 7 else float(mem_line[2])
        except Exception:
            mem_total = 1.0
            mem_available = 0.0
        used_mem = max(0.0, mem_total - mem_available)
        ram_pct = (used_mem / mem_total) * 100.0 if mem_total > 0 else 0.0

        disk_parts = lines[2].split()
        try:
            disk_size = float(disk_parts[1])
            disk_used = float(disk_parts[2])
        except Exception:
            disk_size, disk_used = 1.0, 0.0
        disk_pct = (disk_used / disk_size) * 100.0 if disk_size > 0 else 0.0

        loadavg_line = lines[-1]
        uptime_line = lines[-2]

        rx_total = tx_total = 0
        for nln in lines[3:-2]:
            if ":" not in nln:
                continue
            name, rest = nln.split(":", 1)
            name = name.strip()
            if name == "lo":
                continue
            cols = rest.split()
            if len(cols) < 16:
                continue
            rx_total += int(cols[0])
            tx_total += int(cols[8])

        rx_rate = tx_rate = 0.0
        if st._prev_net is not None:
            p_rx, p_tx = st._prev_net
            d_rx = rx_total - p_rx
            d_tx = tx_total - p_tx
            if d_rx < 0: d_rx = 0
            if d_tx < 0: d_tx = 0
            rx_rate = d_rx / float(self.interval)
            tx_rate = d_tx / float(self.interval)
        st._prev_net = (rx_total, tx_total)

        try:
            uptime_s = int(float(uptime_line.split()[0]))
        except Exception:
            uptime_s = 0
        try:
            load1 = float(loadavg_line.split()[0])
        except Exception:
            load1 = 0.0

        smp = Sample(
            t=time.time(),
            cpu=max(0.0, min(100.0, cpu_pct)),
            ram=max(0.0, min(100.0, ram_pct)),
            disk=max(0.0, min(100.0, disk_pct)),
            rx_rate=rx_rate,
            tx_rate=tx_rate,
            rx_total=rx_total,
            tx_total=tx_total,
            load1=load1,
            uptime_s=uptime_s,
        )
        st.last = smp
        st.history.append(smp)
        self._append_persist(st, smp)

    # ----------------------------- Query Helpers -----------------------------

    def get_summary(self) -> Dict[str, Any]:
        data = []
        with self._lock:
            for st in self.states:
                d = {
                    "name": st.cfg.name,
                    "host": st.cfg.host,
                    "connected": st.connected,
                    "error": st.last_err,
                    "interval": self.interval,
                    "last": None,
                }
                if st.last:
                    d["last"] = vars(st.last)
                data.append(d)
        return {"servers": data, "interval": self.interval, "admin_token": bool(self.admin_token)}

    def get_timeseries(self, name: str, seconds: int) -> Dict[str, Any]:
        st = next((x for x in self.states if x.cfg.name == name or x.cfg.host == name), None)
        if not st:
            raise KeyError("server not found")
        cutoff = time.time() - max(5, seconds)
        hist = [vars(h) for h in st.history if h.t >= cutoff]
        return {"name": st.cfg.name, "series": hist, "interval": self.interval}

    # ----------------------------- Management -----------------------------

    def _require_token(self):
        if self.admin_token:
            tok = request.headers.get("X-Auth-Token") or request.args.get("token")
            if tok != self.admin_token:
                abort(401, "invalid admin token")

    def do_reboot(self, name: str, shutdown: bool = False) -> str:
        self._require_token()
        st = next((x for x in self.states if x.cfg.name == name or x.cfg.host == name), None)
        if not st or not st.ssh:
            abort(400, "server not connected")
        cmd = "shutdown -h now" if shutdown else "reboot"
        self._exec(st, cmd, timeout=3)
        return "ok"

    def do_service_restart(self, name: str, svc: str) -> str:
        self._require_token()
        st = next((x for x in self.states if x.cfg.name == name or x.cfg.host == name), None)
        if not st or not st.ssh:
            abort(400, "server not connected")
        if not svc or any(c in svc for c in ";|&$`"):
            abort(400, "invalid service")
        self._exec(st, f"systemctl restart {svc}", timeout=10)
        return "ok"

    def do_command(self, name: str, cmd: str) -> Dict[str, Any]:
        self._require_token()
        st = next((x for x in self.states if x.cfg.name == name or x.cfg.host == name), None)
        if not st or not st.ssh:
            abort(400, "server not connected")
        if not cmd:
            abort(400, "empty command")
        out, err, rc = self._exec(st, cmd, timeout=15)
        out = out[-4000:]
        err = err[-2000:]
        return {"rc": rc, "out": out, "err": err}

    def get_top(self, name: str) -> Dict[str, Any]:
        st = next((x for x in self.states if x.cfg.name == name or x.cfg.host == name), None)
        if not st or not st.ssh:
            abort(400, "server not connected")
        cmd = r"""ps -eo pid,comm,%cpu,%mem --no-headers --sort=-%cpu | head -n 5"""
        out, err, rc = self._exec(st, cmd, timeout=6)
        rows = []
        for line in out.strip().splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4:
                rows.append({"pid": parts[0], "cmd": parts[1], "cpu": parts[2], "mem": parts[3]})
        return {"rows": rows}

    # ----------------------------- Add/Remove servers -----------------------------

    def _save_servers_json(self) -> None:
        try:
            payload = {
                "poll_interval": self.interval,
                "admin_token": self.admin_token,
                "servers": [vars(s.cfg) for s in self.states],
            }
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[CFG] save error: {e}")

    def add_server(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self._require_token()
        for key in ("name", "host", "username"):
            if not data.get(key):
                abort(400, f"missing {key}")
        if any(s.cfg.name == data["name"] or s.cfg.host == data["host"] for s in self.states):
            abort(400, "server exists")
        sc = ServerConfig(
            name=data["name"],
            host=data["host"],
            port=int(data.get("port", 22)),
            username=data.get("username", "root"),
            password=data.get("password"),
            key_path=data.get("key_path"),
        )
        st = ServerState(cfg=sc)
        with self._lock:
            self.states.append(st)
        self._connect(st)
        self._load_persist(st)  # load any past history (file might be present)
        self._save_servers_json()
        return {"status": "ok"}

    def remove_server(self, name_or_host: str) -> Dict[str, Any]:
        self._require_token()
        idx = None
        for i, st in enumerate(self.states):
            if st.cfg.name == name_or_host or st.cfg.host == name_or_host:
                idx = i
                break
        if idx is None:
            abort(404, "server not found")
        st = self.states.pop(idx)
        try:
            if st.ssh:
                st.ssh.close()
        except Exception:
            pass
        self._save_servers_json()
        return {"status": "ok"}


# ----------------------------- App / Routes -----------------------------

app = Flask(__name__)
MON: Optional[Monitor] = None


@app.route("/")
def index() -> Response:
    assert MON is not None
    return render_template_string(DASHBOARD_HTML, poll_interval=MON.interval)


@app.get("/api/summary")
def api_summary():
    assert MON is not None
    return jsonify(MON.get_summary())


@app.get("/api/timeseries")
def api_timeseries():
    assert MON is not None
    name = request.args.get("name", "")
    seconds = int(request.args.get("seconds", "300"))
    try:
        data = MON.get_timeseries(name, seconds)
        return jsonify(data)
    except KeyError:
        abort(404, "server not found")


@app.post("/api/reboot/<name>")
def api_reboot(name: str):
    assert MON is not None
    res = MON.do_reboot(name, shutdown=False)
    return jsonify({"status": res})


@app.post("/api/shutdown/<name>")
def api_shutdown(name: str):
    assert MON is not None
    res = MON.do_reboot(name, shutdown=True)
    return jsonify({"status": res})


@app.post("/api/service/<name>/restart")
def api_service_restart(name: str):
    assert MON is not None
    svc = request.json.get("service", "") if request.is_json else request.form.get("service", "")
    res = MON.do_service_restart(name, svc)
    return jsonify({"status": res})


@app.post("/api/cmd/<name>")
def api_cmd(name: str):
    assert MON is not None
    cmd = request.json.get("cmd", "") if request.is_json else request.form.get("cmd", "")
    res = MON.do_command(name, cmd)
    return jsonify(res)


@app.get("/api/top/<name>")
def api_top(name: str):
    assert MON is not None
    return jsonify(MON.get_top(name))


# --- live command streaming (SSE) ---
@app.get("/api/cmd_stream/<name>")
def api_cmd_stream(name: str):
    assert MON is not None
    MON._require_token()
    cmd = request.args.get("cmd", "")
    if not cmd:
        return Response("missing cmd", status=400)
    st = next((x for x in MON.states if x.cfg.name == name or x.cfg.host == name), None)
    if not st or not st.ssh:
        return Response("server not connected", status=400)

    def stream():
        chan = st.ssh.get_transport().open_session()
        chan.get_pty()
        chan.exec_command(cmd)
        try:
            while True:
                pushed = False
                if chan.recv_ready():
                    chunk = chan.recv(4096).decode("utf-8", "ignore")
                    if chunk:
                        yield "data: " + json.dumps({"out": chunk}) + "\n\n"
                        pushed = True
                if chan.recv_stderr_ready():
                    chunk = chan.recv_stderr(4096).decode("utf-8", "ignore")
                    if chunk:
                        yield "data: " + json.dumps({"err": chunk}) + "\n\n"
                        pushed = True
                if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                    rc = chan.recv_exit_status()
                    yield "data: " + json.dumps({"done": True, "rc": rc}) + "\n\n"
                    break
                if not pushed:
                    time.sleep(0.05)
        finally:
            try: chan.close()
            except Exception: pass

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(stream(), mimetype="text/event-stream", headers=headers)


# ======================= Global Terminal (all servers) =======================
import uuid
from queue import Queue, Empty

MON_LIVE = {"sessions": {}}  # sid -> {"chans": {name: channel}, "q": Queue, "closed": bool}

def _spawn_reader_global(name, chan, q):
    import time
    while True:
        pushed = False
        if chan.recv_ready():
            chunk = chan.recv(4096).decode("utf-8", "ignore")
            if chunk:
                q.put({"server": name, "type": "out", "data": chunk})
                pushed = True
        if chan.recv_stderr_ready():
            chunk = chan.recv_stderr(4096).decode("utf-8", "ignore")
            if chunk:
                q.put({"server": name, "type": "err", "data": chunk})
                pushed = True
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            rc = chan.recv_exit_status()
            q.put({"server": name, "type": "done", "rc": rc})
            break
        if not pushed:
            time.sleep(0.03)

@app.post("/api/term/start_all")
def api_term_start_all():
    assert MON is not None
    MON._require_token()
    payload = request.get_json(silent=True) or {}
    cmd = payload.get("cmd", "")
    targets = payload.get("targets")
    if not cmd:
        return jsonify({"error": "empty cmd"}), 400

    names = []
    for st in MON.states:
        if st.ssh and st.connected and (not targets or st.cfg.name in targets or st.cfg.host in targets):
            names.append(st.cfg.name)
    if not names:
        return jsonify({"error": "no connected servers"}), 400

    q = Queue()
    chans = {}
    for st in MON.states:
        if st.cfg.name not in names:
            continue
        try:
            chan = st.ssh.get_transport().open_session()
            chan.get_pty()
            chan.exec_command(cmd)
            chans[st.cfg.name] = chan
            t = threading.Thread(target=_spawn_reader_global, args=(st.cfg.name, chan, q), daemon=True)
            t.start()
        except Exception as e:
            q.put({"server": st.cfg.name, "type": "err", "data": f"[open failed] {e}\n"})

    sid = uuid.uuid4().hex
    MON_LIVE["sessions"][sid] = {"chans": chans, "q": q, "closed": False}
    return jsonify({"sid": sid, "servers": names})

@app.get("/api/term/stream/<sid>")
def api_term_stream(sid: str):
    assert MON is not None
    MON._require_token()
    sess = MON_LIVE["sessions"].get(sid)
    if not sess:
        return Response("session not found", status=404)

    def gen():
        init = {"type": "init", "sid": sid, "servers": list(sess["chans"].keys())}
        yield "data: " + json.dumps(init) + "\n\n"
        done_servers = set()
        while True:
            if sess["closed"] and sess["q"].empty():
                break
            try:
                ev = sess["q"].get(timeout=0.2)
            except Empty:
                continue
            if ev.get("type") == "done":
                done_servers.add(ev.get("server"))
            yield "data: " + json.dumps(ev) + "\n\n"
            if len(done_servers) >= len(sess["chans"]):
                break
        yield "data: " + json.dumps({"type": "session_end"}) + "\n\n"

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(gen(), mimetype="text/event-stream", headers=headers)

@app.post("/api/term/input_all/<sid>")
def api_term_input_all(sid: str):
    assert MON is not None
    MON._require_token()
    sess = MON_LIVE["sessions"].get(sid)
    if not sess:
        return jsonify({"error":"session not found"}), 404
    data = (request.get_json(silent=True) or {}).get("data", "")
    if not data:
        return jsonify({"error":"empty input"}), 400
    for ch in sess["chans"].values():
        try: ch.send(data)
        except Exception: pass
    return jsonify({"status":"ok"})

@app.post("/api/term/input_one/<sid>/<server>")
def api_term_input_one(sid: str, server: str):
    assert MON is not None
    MON._require_token()
    sess = MON_LIVE["sessions"].get(sid)
    if not sess:
        return jsonify({"error":"session not found"}), 404
    ch = sess["chans"].get(server)
    if not ch:
        return jsonify({"error":"server not in session"}), 404
    data = (request.get_json(silent=True) or {}).get("data", "")
    if not data:
        return jsonify({"error":"empty input"}), 400
    ch.send(data)
    return jsonify({"status":"ok"})

@app.post("/api/term/close/<sid>")
def api_term_close(sid: str):
    assert MON is not None
    MON._require_token()
    sess = MON_LIVE["sessions"].pop(sid, None)
    if not sess:
        return jsonify({"status":"ok"})
    for ch in sess["chans"].values():
        try: ch.close()
        except Exception: pass
    sess["closed"] = True
    return jsonify({"status":"ok"})


# ---------------- Add/Remove servers routes ----------------
@app.get("/api/servers")
def api_servers():
    assert MON is not None
    return jsonify([vars(s.cfg) for s in MON.states])

@app.post("/api/server/add")
def api_server_add():
    assert MON is not None
    data = request.get_json(silent=True) or {}
    res = MON.add_server(data)
    return jsonify(res)

@app.post("/api/server/remove")
def api_server_remove():
    assert MON is not None
    data = request.get_json(silent=True) or {}
    target = data.get("target","")
    res = MON.remove_server(target)
    return jsonify(res)


# ----------------------------- HTML (UI) -----------------------------

DASHBOARD_HTML = r"""
<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Multi‑Server SSH Monitor</title>

<!-- Font: Inter (fallback to system) -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">

<!-- Chart.js -->
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>

<style>
:root{
  --bg:#0f1115;
  --panel:#141824;
  --panel-2:#111525;
  --text:#e5e7eb;
  --muted:#9aa0aa;
  --accent:#51a8ff;
  --accent2:#ff6b6b;
  --ok:#16c47f;
  --warn:#f8d34a;
  --err:#ff5d5d;
  --card-radius:14px;
  --shadow:0 6px 18px rgba(0,0,0,.35);
}

*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;background:linear-gradient(180deg,#0c0f14,#0f1115 25%,#0f1115);
  color:var(--text);font-family:Inter,system-ui,Segoe UI,Roboto,Arial,sans-serif;
  -webkit-font-smoothing:antialiased; font-feature-settings:"tnum" 1,"lnum" 1;
}

.header{
  position:sticky;top:0;z-index:10;
  backdrop-filter:saturate(160%) blur(8px);
  background:rgba(16,18,26,.55);
  border-bottom:1px solid rgba(255,255,255,.06);
}
.header .inner{
  display:flex;align-items:center;gap:12px;
  padding:14px 18px;max-width:1400px;margin:auto;
}
.brand{font-weight:800;font-size:18px;letter-spacing:.3px}
.chips{margin-inline-start:auto;display:flex;gap:8px;align-items:center}
.chip{font-size:12px;color:var(--muted);background:#1a2030;border:1px solid #252a3a;
  padding:6px 10px;border-radius:30px}

.container{max-width:1400px;margin:18px auto;padding:0 18px 60px}
.grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));
  gap:16px;
}
.card{
  background:linear-gradient(180deg,#13192a,#121726);
  border:1px solid #22283a;border-radius:var(--card-radius);box-shadow:var(--shadow);
  padding:14px 14px 12px; position:relative; overflow:hidden;
}
.card .head{
  display:flex;align-items:baseline;gap:8px;margin-bottom:8px;
}
.card .title{
  font-weight:700;font-size:14.5px
}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-inline-start:6px}
.dot.ok{background:var(--ok)} .dot.bad{background:var(--err)}

.card .sub{
  color:var(--muted);font-size:12px;margin-inline-start:6px;direction:ltr;unicode-bidi:plaintext
}

.actions{margin-inline-start:auto;display:flex;gap:6px}
.btn{
  background:#1a2030;border:1px solid #262c3e;color:#cfd6e4;
  padding:6px 10px;border-radius:8px;font-size:12px;cursor:pointer
}
.btn:hover{filter:brightness(1.08)}
.btn.warn{border-color:#3a2b18;background:#241a10}
.btn.danger{border-color:#3a2020;background:#241212}

.row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;align-items:center}
.mini{height:120px}
.canvas-wrap{height:118px}

.kpi{display:flex;gap:14px;margin-top:6px;margin-bottom:4px}
.kpi .item{font-size:12.5px;color:#cfd6e4}
.kpi b{font-weight:700;color:#fff}

.footer-note{color:var(--muted);text-align:center;margin-top:22px;font-size:12px}

.modal{
  position:fixed;inset:0;background:rgba(0,0,0,.55);
  display:none;align-items:center;justify-content:center;
}
.modal .box{
  width:min(640px,92vw);background:#121726;border:1px solid #262c3e;border-radius:12px;
  padding:16px;box-shadow:var(--shadow);
}
.modal .row{grid-template-columns:1fr auto}
.modal label{font-size:12px;color:var(--muted)}
.modal input, .modal select{width:100%;background:#0f1322;border:1px solid #23283a;border-radius:8px;
  color:#e6ecf5;padding:10px 12px;margin-top:6px}
.modal .box .actions{margin-top:12px}
.hidden{display:none}
.gauge-labels{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:4px;color:#9aa0aa;font-size:12px;text-align:center}
</style>
</head>
<body>
<header class="header">
  <div class="inner">
    <div class="brand">Multi‑Server SSH Monitor</div>
    <div class="chips">
      <div class="chip" id="now"></div>
      <div class="chip">Tip: use SSH keys</div>
      <div class="chip" id="range-chips">
        بازه: <button class="btn" data-r="60">1m</button>
        <button class="btn" data-r="300">5m</button>
        <button class="btn" data-r="900">15m</button>
        <button class="btn" data-r="3600">1h</button>
      </div>
      <button class="btn" id="btn-global-term" style="margin-inline-start:8px">ترمینال کلی</button>
      <button class="btn" id="btn-manage-servers" style="margin-inline-start:6px">مدیریت سرورها</button>
    </div>
  </div>
</header>

<div class="container">
  <div id="grid" class="grid"></div>
  <div class="footer-note">Refresh interval adapts to servers.json • همه‌چیز فقط روی همین سیستم شما اجرا می‌شود</div>
</div>

<!-- Modal for management (per-server) -->
<div id="modal" class="modal" role="dialog" aria-modal="true">
  <div class="box">
    <div class="head" style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <div class="title" id="modal-title" style="font-weight:700">مدیریت سرور</div>
      <div class="sub" id="modal-sub"></div>
      <div class="actions" style="margin-inline-start:auto">
        <button class="btn" onclick="closeModal()">بستن</button>
      </div>
    </div>
    <div class="row">
      <div>
        <label>دستور دلخواه</label>
        <input id="cmd-input" placeholder="مثال: systemctl status nginx یا ls -lh /var/log">
      </div>
      <div style="display:flex;align-items:flex-end;gap:6px">
        <button class="btn" onclick="runCmd()">اجرا</button>
        <button class="btn" onclick="runCmdLive()">Live</button>
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <div>
        <label>ریاستارت سرویس (systemd)</label>
        <input id="svc-input" placeholder="مثال: nginx">
      </div>
      <div style="display:flex;align-items:flex-end;gap:6px">
        <button class="btn" onclick="restartService()">Restart</button>
      </div>
    </div>
    <div class="actions" style="gap:8px">
      <button class="btn warn" onclick="reboot()">Reboot</button>
      <button class="btn danger" onclick="shutdown()">Shutdown</button>
    </div>
    <pre id="cmd-out" style="margin-top:10px;background:#0b0f1a;border:1px solid #1e2435;border-radius:8px;padding:10px;max-height:320px;overflow:auto;color:#cfe2ff"></pre>
  </div>
</div>

<!-- Global Terminal Modal -->
<div id="gmodal" class="modal" role="dialog" aria-modal="true" style="display:none">
  <div class="box" style="width:min(900px,94vw)">
    <div class="head" style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <div class="title" style="font-weight:700">ترمینال کلی (همزمان روی همه سرورها)</div>
      <div class="sub" id="g-sid" style="direction:ltr"></div>
      <div class="actions" style="margin-inline-start:auto">
        <button class="btn" id="g-close">بستن</button>
      </div>
    </div>
    <div class="row">
      <div>
        <label>دستور اولیه</label>
        <input id="g-cmd" placeholder="مثال: bash script.sh یا apt update">
      </div>
      <div style="display:flex;align-items:flex-end;gap:6px">
        <button class="btn" id="g-start">Start</button>
      </div>
    </div>
    <div style="margin-top:10px; display:flex; gap:8px; align-items:center">
      <input id="g-input" placeholder="ورودی برای ارسال (Enter=ارسال به همه)" style="flex:1;background:#0f1322;border:1px solid #23283a;border-radius:8px;color:#e6ecf5;padding:10px 12px">
      <button class="btn" id="g-sendall">ارسال به همه</button>
      <button class="btn warn" id="g-stop">پایان</button>
    </div>
    <div id="g-outputs" style="margin-top:12px;max-height:420px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px"></div>
  </div>
</div>

<!-- Manage Servers Modal -->
<div id="msmodal" class="modal" role="dialog" aria-modal="true" style="display:none">
  <div class="box" style="width:min(780px,94vw)">
    <div class="head" style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <div class="title" style="font-weight:700">مدیریت سرورها</div>
      <div class="actions" style="margin-inline-start:auto">
        <button class="btn" id="ms-close">بستن</button>
      </div>
    </div>
    <div class="row">
      <div><label>نام</label><input id="ms-name" placeholder="مثلا: New-Server"></div>
      <div><label>IP/Host</label><input id="ms-host" placeholder="مثلا: 1.2.3.4"></div>
      <div><label>Port</label><input id="ms-port" value="22"></div>
    </div>
    <div class="row" style="margin-top:10px">
      <div><label>Username</label><input id="ms-user" value="root"></div>
      <div><label>Password</label><input id="ms-pass" placeholder="(یا خالی اگر با کلید)"></div>
      <div><label>Key path</label><input id="ms-key" placeholder="~/.ssh/id_rsa (اختیاری)"></div>
    </div>
    <div class="actions" style="gap:8px">
      <button class="btn" id="ms-add">افزودن سرور</button>
    </div>
    <div style="margin-top:10px">
      <label>لیست سرورها</label>
      <div id="ms-list" style="margin-top:6px;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px"></div>
    </div>
  </div>
</div>

<script>
const fmtBytes = (bps) => {
  if (bps < 1024) return bps.toFixed(0) + ' B/s';
  const kb = bps/1024; if (kb < 1024) return kb.toFixed(2)+' KB/s';
  const mb = kb/1024; if (mb < 1024) return mb.toFixed(2)+' MB/s';
  const gb = mb/1024; return gb.toFixed(2)+' GB/s';
};

const state = { range: 300, charts: {}, admin: false, token: '', servers: [] };

// Tehran time (always Asia/Tehran)
function tickClockTehran(){
  const fmt = new Intl.DateTimeFormat('fa-IR', {year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false, timeZone:'Asia/Tehran'});
  document.getElementById('now').textContent = fmt.format(new Date());
}
setInterval(tickClockTehran, 1000); tickClockTehran();

document.getElementById('range-chips').addEventListener('click', (e)=>{
  const btn = e.target.closest('button[data-r]');
  if (!btn) return;
  state.range = parseInt(btn.getAttribute('data-r'),10);
  refreshAllSeries();
});

async function fetchSummary(){
  const r = await fetch('/api/summary');
  const j = await r.json();
  state.admin = !!j.admin_token;
  state.servers = j.servers;
  renderCards(j.servers, j.interval);
}

function makeGauge(ctx, initial){
  return new Chart(ctx, {
    type: 'doughnut',
    data:{ labels:['',''],
      datasets:[{
        data:[initial, 100-initial],
        cutout:'70%', circumference:180, rotation:270, borderWidth:0
      }]
    },
    options:{ responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}, tooltip:{enabled:false}} }
  });
}

function makeLine(ctx, labels, a, b){
  return new Chart(ctx,{
    type:'line',
    data:{labels:labels, datasets:[
      {label:'RX', data:a, tension:.35, fill:true},
      {label:'TX', data:b, tension:.35, fill:true}
    ]},
    options:{ responsive:true, maintainAspectRatio:false, animation:false, plugins:{legend:{display:true, labels:{boxWidth:10}}},
      scales:{x:{display:false},y:{display:true, grid:{display:false}}}
    }
  });
}

function makeTriple(ctx){
  return new Chart(ctx,{
    type:'line',
    data:{labels:[], datasets:[
      {label:'% CPU', data:[], tension:.35, fill:true},
      {label:'% RAM', data:[], tension:.35, fill:true},
      {label:'% Disk', data:[], tension:.35, fill:true},
    ]},
    options:{ responsive:true, maintainAspectRatio:false, animation:false,
      plugins:{legend:{display:true, labels:{boxWidth:10}}},
      scales:{x:{display:false}, y:{display:true, min:0, max:100}}
    }
  });
}

function uptimeFmt(sec){
  const d = Math.floor(sec/86400); sec%=86400;
  const h = Math.floor(sec/3600); sec%=3600;
  const m = Math.floor(sec/60);
  return `${d}d ${h}h ${m}m`;
}
function dot(ok){ return `<span class="dot ${ok?'ok':'bad'}"></span>` }

function ensureLabels(container){
  if (container.querySelector('.gauge-labels')) return;
  const div = document.createElement('div');
  div.className = 'gauge-labels';
  div.innerHTML = '<div>CPU</div><div>RAM</div><div>Disk</div>';
  container.appendChild(div);
}

function renderCards(servers, interval){
  const grid = document.getElementById('grid');
  for(const s of servers){
    if (document.getElementById('card-'+s.name)) continue;
    const card = document.createElement('div'); card.className='card'; card.id='card-'+s.name;
    card.innerHTML = `
      <div class="head">
        <div class="title">${s.name} <span class="sub">(${s.host})</span> ${dot(s.connected)}</div>
        <div class="actions">
          <button class="btn" onclick="openModal('${s.name}','${s.host}')">مدیریت</button>
          <button class="btn" onclick="showTop('${s.name}')">Top</button>
        </div>
      </div>
      <div class="row">
        <div class="canvas-wrap"><canvas id="g1-${s.name}" class="mini"></canvas></div>
        <div class="canvas-wrap"><canvas id="g2-${s.name}" class="mini"></canvas></div>
        <div class="canvas-wrap"><canvas id="g3-${s.name}" class="mini"></canvas></div>
      </div>
      <div class="gauge-labels">
        <div>CPU</div><div>RAM</div><div>Disk</div>
      </div>
      <div class="kpi">
        <div class="item">Load(1m): <b id="ld-${s.name}">—</b></div>
        <div class="item">Uptime: <b id="up-${s.name}">—</b></div>
      </div>
      <div class="canvas-wrap" style="height:140px"><canvas id="net-${s.name}"></canvas></div>
      <div class="canvas-wrap" style="height:140px"><canvas id="tri-${s.name}"></canvas></div>
    `;
    grid.appendChild(card);
    ensureLabels(card);

    state.charts[s.name] = {
      g1: makeGauge(document.getElementById(`g1-${s.name}`).getContext('2d'), 0),
      g2: makeGauge(document.getElementById(`g2-${s.name}`).getContext('2d'), 0),
      g3: makeGauge(document.getElementById(`g3-${s.name}`).getContext('2d'), 0),
      net: makeLine(document.getElementById(`net-${s.name}`).getContext('2d'), [], [], []),
      tri: makeTriple(document.getElementById(`tri-${s.name}`).getContext('2d')),
    };
  }
  updateSummary(servers);
  refreshAllSeries();
  setTimeout(fetchSummary, interval*1000);
}

function updateSummary(servers){
  servers.forEach(s=>{
    const el = document.querySelector(`#card-${s.name} .title`);
    if (el) el.innerHTML = `${s.name} <span class="sub">(${s.host})</span> ${dot(s.connected)}`;
    if (!s.last) return;
    const v = s.last; const ch = state.charts[s.name]; if (!ch) return;
    const setGauge = (inst, value)=>{ const val = Math.max(0, Math.min(100, value)); inst.data.datasets[0].data=[val,100-val]; inst.update('none'); };
    setGauge(ch.g1, v.cpu); setGauge(ch.g2, v.ram); setGauge(ch.g3, v.disk);
    const up = document.getElementById(`up-${s.name}`); const ld = document.getElementById(`ld-${s.name}`);
    if (up) up.textContent = uptimeFmt(v.uptime_s);
    if (ld) ld.textContent = v.load1.toFixed(2);
  });
}

async function refreshAllSeries(){
  for(const s of state.servers){
    await refreshSeriesFor(s.name);
  }
}

async function refreshSeriesFor(name){
  const r = await fetch(`/api/timeseries?name=${encodeURIComponent(name)}&seconds=${state.range}`);
  if (!r.ok) return;
  const j = await r.json();
  const ch = state.charts[name]; if (!ch) return;
  const labels = j.series.map(p=> new Date(p.t*1000).toLocaleTimeString('fa-IR',{hour12:false,timeZone:'Asia/Tehran'}));
  const rx = j.series.map(p=> p.rx_rate); const tx = j.series.map(p=> p.tx_rate);
  ch.net.data.labels = labels; ch.net.data.datasets[0].data = rx; ch.net.data.datasets[1].data = tx;
  ch.net.options.scales.y.ticks = { callback:(v)=>fmtBytes(v) }; ch.net.update('none');
  const cpu=j.series.map(p=>p.cpu), ram=j.series.map(p=>p.ram), dsk=j.series.map(p=>p.disk);
  ch.tri.data.labels = labels; ch.tri.data.datasets[0].data=cpu; ch.tri.data.datasets[1].data=ram; ch.tri.data.datasets[2].data=dsk; ch.tri.update('none');
}

let currentServer = null;
function openModal(name, host){
  currentServer = name;
  document.getElementById('modal-title').textContent = `مدیریت ${name}`;
  document.getElementById('modal-sub').textContent = `(${host})`;
  document.getElementById('cmd-out').textContent = '';
  document.getElementById('cmd-input').value='';
  document.getElementById('svc-input').value='';
  document.getElementById('modal').style.display='flex';
}
window.closeModal = function(){
  if (window.liveES){ try{ window.liveES.close(); }catch(e){} window.liveES = null; }
  const m = document.getElementById('modal'); if (m) m.style.display='none';
}

async function doPOST(url, body){
  const headers = {'Content-Type':'application/json'};
  if (state.token) headers['X-Auth-Token'] = state.token;
  const r = await fetch(url,{method:'POST', headers, body: JSON.stringify(body||{})});
  if (!r.ok){
    const t = await r.text();
    throw new Error(t || r.statusText);
  }
  return r.json();
}

async function reboot(){
  if (!currentServer) return;
  if (!confirm('Reboot این سرور انجام شود؟')) return;
  try { await doPOST(`/api/reboot/${encodeURIComponent(currentServer)}`); alert('درخواست ریبوت ارسال شد'); }
  catch(e){ alert('خطا: '+e.message) }
}
async function shutdown(){
  if (!currentServer) return;
  if (!confirm('Shutdown این سرور انجام شود؟')) return;
  try { await doPOST(`/api/shutdown/${encodeURIComponent(currentServer)}`); alert('درخواست شات‌داون ارسال شد'); }
  catch(e){ alert('خطا: '+e.message) }
}
async function restartService(){
  const svc = document.getElementById('svc-input').value.trim();
  if (!svc) return alert('نام سرویس را وارد کنید');
  try{
    await doPOST(`/api/service/${encodeURIComponent(currentServer)}/restart`,{service:svc});
    document.getElementById('cmd-out').textContent = `✅ service ${svc} restarted`;
  }catch(e){
    document.getElementById('cmd-out').textContent = '❌ '+e.message;
  }
}
async function runCmd(){
  const cmd = document.getElementById('cmd-input').value.trim();
  if (!cmd) return;
  try{
    const j = await doPOST(`/api/cmd/${encodeURIComponent(currentServer)}`,{cmd});
    document.getElementById('cmd-out').textContent = `rc=${j.rc}\n\n` + (j.out||'') + (j.err?('\nERR:\n'+j.err):'');
  }catch(e){
    document.getElementById('cmd-out').textContent = '❌ '+e.message;
  }
}

// Live terminal (per-server)
let liveES = null;
function appendOut(txt){ const out = document.getElementById('cmd-out'); out.textContent += txt; out.scrollTop = out.scrollHeight; }
function runCmdLive(){
  if (!currentServer) return;
  const cmd = document.getElementById('cmd-input').value.trim();
  if (!cmd) { alert('یک دستور وارد کنید'); return; }
  if (liveES) { try{ liveES.close(); }catch(e){} liveES = null; }
  document.getElementById('cmd-out').textContent = '⏳ اجرای زنده...\n';
  const tokenParam = state.token ? `&token=${encodeURIComponent(state.token)}` : '';
  const url = `/api/cmd_stream/${encodeURIComponent(currentServer)}?cmd=${encodeURIComponent(cmd)}${tokenParam}`;
  liveES = new EventSource(url);
  liveES.onmessage = (ev)=>{
    try{
      const data = JSON.parse(ev.data);
      if (data.out) appendOut(data.out);
      if (data.err) appendOut(data.err);
      if (data.done){
        appendOut(`\n\n✅ پایان (rc=${data.rc})\n`);
        liveES.close(); liveES = null;
      }
    }catch(e){ appendOut('\n[parse error]\n'); }
  };
  liveES.onerror = ()=>{ appendOut('\n❌ خطا در استریم\n'); try{ liveES.close(); }catch(e){} liveES = null; };
}

// Global terminal
let gSid = null, gES = null, gServers = [];
document.getElementById('btn-global-term').addEventListener('click', ()=>{ document.getElementById('gmodal').style.display='flex'; });
document.getElementById('g-close').addEventListener('click', ()=>{ if (gES){ try{ gES.close(); }catch(e){} gES=null; } gSid=null; document.getElementById('gmodal').style.display='none'; });
document.getElementById('g-start').addEventListener('click', startGlobal);
document.getElementById('g-sendall').addEventListener('click', sendAll);
document.getElementById('g-stop').addEventListener('click', stopGlobal);
document.getElementById('g-input').addEventListener('keydown', (e)=>{ if (e.key==='Enter'){ e.preventDefault(); sendAll(); } });

function addPane(name){
  const box = document.createElement('div');
  box.className='card';
  box.innerHTML = `
    <div class="head" style="margin-bottom:6px">
      <div class="title">${name}</div>
      <div class="actions" style="margin-inline-start:auto">
        <button class="btn" data-send-one="${name}">ارسال</button>
      </div>
    </div>
    <pre id="go-${name}" style="background:#0b0f1a;border:1px solid #1e2435;border-radius:8px;padding:10px;max-height:260px;overflow:auto;color:#cfe2ff"></pre>
  `;
  document.getElementById('g-outputs').appendChild(box);
}
function appendPane(name, txt){ const el = document.getElementById('go-'+name); if (!el) return; el.textContent += txt; el.scrollTop = el.scrollHeight; }
document.body.addEventListener('click', (e)=>{ const t=e.target; if (t.dataset && t.dataset.sendOne){ sendOne(t.dataset.sendOne); } });

async function startGlobal(){
  if (gSid){ alert('سشن فعال است'); return; }
  const cmd = document.getElementById('g-cmd').value.trim();
  if (!cmd){ alert('دستور را وارد کنید'); return; }
  const headers = {'Content-Type':'application/json'};
  if (state.token) headers['X-Auth-Token'] = state.token;
  const r = await fetch('/api/term/start_all', {method:'POST', headers, body: JSON.stringify({cmd})});
  if (!r.ok){ alert(await r.text()); return; }
  const j = await r.json(); gSid = j.sid; document.getElementById('g-sid').textContent = 'SID: '+gSid;
  const tokenParam = state.token ? ('?token='+encodeURIComponent(state.token)) : '';
  gES = new EventSource('/api/term/stream/'+encodeURIComponent(gSid)+tokenParam);
  gES.onmessage = (ev)=>{
    const d = JSON.parse(ev.data);
    if (d.type === 'init'){ gServers=d.servers||[]; document.getElementById('g-outputs').innerHTML=''; gServers.forEach(addPane); return; }
    if (d.type === 'out' || d.type === 'err'){ appendPane(d.server, d.data); }
    if (d.type === 'done'){ appendPane(d.server, `\n[rc=${d.rc}] ✅\n`); }
  };
}
async function sendAll(){
  if (!gSid) return;
  const val = document.getElementById('g-input').value; if (!val) return;
  document.getElementById('g-input').value='';
  const headers = {'Content-Type':'application/json'};
  if (state.token) headers['X-Auth-Token'] = state.token;
  await fetch('/api/term/input_all/'+encodeURIComponent(gSid), {method:'POST', headers, body: JSON.stringify({data: val+"\\n"})});
}
async function sendOne(name){
  if (!gSid) return;
  const val = prompt('ورودی فقط برای '+name+':',''); if (val===null) return;
  const headers = {'Content-Type':'application/json'};
  if (state.token) headers['X-Auth-Token'] = state.token;
  await fetch('/api/term/input_one/'+encodeURIComponent(gSid)+'/'+encodeURIComponent(name), {method:'POST', headers, body: JSON.stringify({data: val+"\\n"})});
}
async function stopGlobal(){
  if (!gSid) return;
  const headers = {'Content-Type':'application/json'};
  if (state.token) headers['X-Auth-Token'] = state.token;
  await fetch('/api/term/close/'+encodeURIComponent(gSid), {method:'POST', headers});
  if (gES){ try{ gES.close(); }catch(e){} gES=null; } gSid=null;
}

// Manage servers UI
document.getElementById('btn-manage-servers').addEventListener('click', ()=>{ document.getElementById('msmodal').style.display='flex'; loadServersList(); });
document.getElementById('ms-close').addEventListener('click', ()=>{ document.getElementById('msmodal').style.display='none'; });
document.getElementById('ms-add').addEventListener('click', addServer);

async function loadServersList(){
  const r = await fetch('/api/servers'); const list = await r.json();
  const wrap = document.getElementById('ms-list'); wrap.innerHTML='';
  for(const s of list){
    const el = document.createElement('div'); el.className='card';
    el.innerHTML = `
      <div class="head"><div class="title">${s.name}</div><div class="sub">${s.host}:${s.port}</div>
        <div class="actions" style="margin-inline-start:auto"><button class="btn danger" data-remove="${s.name}">حذف</button></div></div>`;
    wrap.appendChild(el);
  }
}

document.getElementById('ms-list').addEventListener('click', async (e)=>{
  const t = e.target.closest('[data-remove]'); if (!t) return;
  const name = t.getAttribute('data-remove');
  if (!confirm('حذف '+name+' ?')) return;
  const headers = {'Content-Type':'application/json'}; if (state.token) headers['X-Auth-Token']=state.token;
  await fetch('/api/server/remove', {method:'POST', headers, body: JSON.stringify({target:name})});
  await loadServersList(); setTimeout(fetchSummary, 200);
});

async function addServer(){
  const name = document.getElementById('ms-name').value.trim();
  const host = document.getElementById('ms-host').value.trim();
  const port = parseInt(document.getElementById('ms-port').value.trim()||'22',10);
  const user = document.getElementById('ms-user').value.trim();
  const pwd  = document.getElementById('ms-pass').value;
  const key  = document.getElementById('ms-key').value.trim();
  if (!name || !host || !user) return alert('نام، میزبان، کاربر لازم است');
  const headers = {'Content-Type':'application/json'}; if (state.token) headers['X-Auth-Token']=state.token;
  const body = {name, host, port, username:user}; if (pwd) body.password=pwd; if (key) body.key_path=key;
  const r = await fetch('/api/server/add', {method:'POST', headers, body: JSON.stringify(body)});
  if (!r.ok){ alert(await r.text()); return; }
  document.getElementById('ms-name').value=''; document.getElementById('ms-host').value=''; document.getElementById('ms-port').value='22';
  document.getElementById('ms-user').value='root'; document.getElementById('ms-pass').value=''; document.getElementById('ms-key').value='';
  await loadServersList(); setTimeout(fetchSummary, 200);
}

async function showTop(name){
  const r = await fetch(`/api/top/${encodeURIComponent(name)}`);
  if (!r.ok) return;
  const j = await r.json();
  let txt = 'Top 5 by CPU:\nPID\tCPU%\tMEM%\tCMD\n';
  for(const row of j.rows){
    txt += `${row.pid}\t${row.cpu}\t${row.mem}\t${row.cmd}\n`;
  }
  currentServer = name;
  openModal(name, '');
  document.getElementById('cmd-out').textContent = txt;
}

(async function init(){
  const sumR = await fetch('/api/summary'); const sumJ = await sumR.json();
  if (sumJ.admin_token){
    const tok = prompt('Admin token برای عملیات مدیریتی را وارد کنید (می‌توانید خالی بگذارید):','');
    if (tok) state.token = tok;
  }
  state.servers = sumJ.servers;
  renderCards(sumJ.servers, sumJ.interval);
})();
</script>
</body>
</html>
"""

# ----------------------------- Entrypoint -----------------------------

def main():
    global MON
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = os.path.join(here, "servers.json")
    MON = Monitor(cfg)
    MON.start()
    app.run(host="0.0.0.0", port=8000, debug=False)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
