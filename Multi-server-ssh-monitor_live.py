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



@app.get("/api/debug")
def api_debug():
    assert MON is not None
    try:
        info = {
            "cwd": os.getcwd(),
            "cfg_path": getattr(MON, "cfg_path", None),
            "interval": getattr(MON, "interval", None),
            "states_count": len(getattr(MON, "states", [])),
            "names": [s.cfg.name for s in getattr(MON, "states", [])],
            "admin_token": bool(getattr(MON, "admin_token", None)),
        }
    except Exception as e:
        info = {"error": str(e)}
    return jsonify(info)
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
    if isinstance(data, str):
        data = data.replace("\r\n", "\n").replace("\r", "\n").replace("\\n", "\n")
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


@app.post("/api/term/interrupt/<sid>")
def api_term_interrupt(sid: str):
    assert MON is not None
    MON._require_token()
    sess = MON_LIVE["sessions"].get(sid)
    if not sess:
        return jsonify({"error":"session not found"}), 404
    sent = 0
    # send Ctrl+C twice to all channels (like v9 behavior)
    for _ in range(2):
        for ch in list(sess.get("chans", {}).values()):
            try:
                ch.send("\x03")
                sent += 1
            except Exception:
                pass
        time.sleep(0.1)
    return jsonify({"status":"ok","sent":sent})


@app.post("/api/server/update")
def api_server_update():
    assert MON is not None
    data = request.get_json(silent=True) or {}
    name = data.get("name","")
    if not name:
        return jsonify({"error":"missing name"}), 400
    MON._require_token()
    st = next((x for x in MON.states if x.cfg.name == name), None)
    if not st:
        return jsonify({"error":"server not found"}), 404
    host = data.get("host"); port = data.get("port"); user = data.get("username")
    pwd = data.get("password"); key = data.get("key_path")
    if host is not None: st.cfg.host = host
    if port is not None: st.cfg.port = int(port)
    if user is not None: st.cfg.username = user
    if pwd is not None: st.cfg.password = pwd
    if key is not None: st.cfg.key_path = key
    try:
        MON._connect(st)
    except Exception:
        pass
    MON._save_servers_json()
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
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js">
document.addEventListener('DOMContentLoaded', ()=>{
  const lp = document.getElementById('lp-toggle');
  if (lp){ lp.style.display='none'; }
});

</script>

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
.mini canvas{display:block;width:100% !important;height:100% !important;}

/* terminal resize styles */
.g-pane{ resize: both; min-width: 320px; min-height: 200px; overflow: hidden; position: relative; }
.g-body{ height: 100%; overflow: auto; scrollbar-width: none; } /* Firefox hide */
.g-body::-webkit-scrollbar{ width:0; height:0; } /* WebKit hide */
.g-pane .corner-grip{ position:absolute; right:4px; bottom:4px; width:12px; height:12px; border:1px dashed rgba(255,255,255,.25); border-right:none; border-bottom:none; transform: rotate(45deg); pointer-events:none; opacity:.6; }
</style>
</head>
<body>
<header class="header">
  <div class="inner">
    <div class="brand">Multi‑Server SSH Monitor</div>
    <div class="chips">
      <div class="chip" id="now"></div>
      <button class="btn" id="lp-toggle" title="کاهش مصرف CPU/شبکه">کم‌مصرف: خاموش</button>
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
        <button class="btn" id="g-stagger" title="کاهش بار رندر خروجی">حالت کم‌مصرف: خاموش</button>
        <button class="btn danger" id="g-int">توقف همه</button>
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

// ---- Low Power mode + timers ----
state.lowPower = localStorage.getItem('mssm_lp') === '1';
state.summaryTimer = null;
state.seriesTimer = null;
state.clockTimer = null;
state._nextIdx = 0;

function setClockTimer(){
  try{ if (state.clockTimer){ clearInterval(state.clockTimer); } }catch(e){}
  const period = state.lowPower ? 5000 : 1000;
  state.clockTimer = setInterval(tickClockTehran, period);
}

function applyLowPowerUI(){
  const lpBtn = document.getElementById('lp-toggle');
  if (lpBtn) lpBtn.textContent = 'کم‌مصرف: ' + (state.lowPower ? 'روشن' : 'خاموش');
}

function toggleLowPower(){
  state.lowPower = !state.lowPower;
  localStorage.setItem('mssm_lp', state.lowPower ? '1' : '0');
  applyLowPowerUI();
  setClockTimer();
  // reschedule fetch & series timers
  scheduleLoops(state.lastInterval || 3);
}
document.getElementById('lp-toggle').addEventListener('click', toggleLowPower);

// ---- Debug FAB (always visible on home) ----
(function(){
  function css(el, o){ for(const k in o){ el.style[k]=o[k]; } return el; }
  function esc(s){ return (s==null)?'':String(s).replace(/[&<>]/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m])); }
  let panel, fab;
  async function showDebug(){
    const out = [];
    try{ const d = await (await fetch('/api/debug')).json();
         out.push('cfg_path: '+esc(d.cfg_path));
         out.push('interval: '+esc(d.interval));
         out.push('states_count: '+esc(d.states_count)+' names: '+JSON.stringify(d.names||[])); }
    catch(e){ out.push('debug api error: '+e); }
    try{ const s = await (await fetch('/api/servers')).json();
         out.push('GET /api/servers -> '+(Array.isArray(s)? s.length : '???')); } catch(e){}
    try{ const sum = await (await fetch('/api/summary')).json();
         out.push('summary servers: '+(sum.servers? sum.servers.length : '???')+' interval: '+sum.interval); } catch(e){}
    if (window.state) out.push('UI state.servers: '+(Array.isArray(state.servers)? state.servers.length : 'n/a'));
    panel.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><b>Debug</b><button class="btn" id="dbg-close">بستن</button></div>' + '<pre style="white-space:pre-wrap;direction:ltr">'+esc(out.join('\\n'))+'</pre>';
    panel.style.display='block';
    const c = document.getElementById('dbg-close'); if (c) c.onclick = ()=> panel.style.display='none';
  }
  function ensureUI(){
    if (!panel){
      panel = document.createElement('div'); panel.id='dbg-panel';
      css(panel,{position:'fixed',right:'10px',bottom:'60px',width:'min(60vw,620px)',maxHeight:'60vh',overflow:'auto',background:'#0b0f1a',border:'1px solid #243',boxShadow:'0 6px 24px rgba(0,0,0,.5)',padding:'10px',borderRadius:'10px',zIndex:9999,color:'#cfe2ff',fontSize:'12px',display:'none'});
      document.body.appendChild(panel);
    }
    if (!fab){
      fab = document.createElement('button'); fab.textContent='🛠'; fab.title='Debug (Alt+D)';
      fab.className='btn'; css(fab,{position:'fixed',right:'10px',bottom:'10px',zIndex:9999,opacity:0.85});
      fab.onclick = function(){ panel.innerHTML='<pre>Loading...</pre>'; panel.style.display='block'; showDebug(); };
      document.body.appendChild(fab);
      window.addEventListener('keydown', (e)=>{ if (e.altKey && (e.key==='d'||e.key==='D')) fab.click(); });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ensureUI);
  else ensureUI();
})();

// ---- Scheduling helpers ----
function scheduleLoops(interval){
  state.lastInterval = interval || state.lastInterval || 3;
  // clear old timers
  try{ if (state.summaryTimer) clearTimeout(state.summaryTimer); }catch(e){}
  try{ if (state.seriesTimer) clearInterval(state.seriesTimer); }catch(e){}
  // schedule summary (status) polling
  const k = state.lowPower ? 3 : 1;
  state.summaryTimer = setTimeout(fetchSummary, (state.lastInterval * k) * 1000);
  // schedule timeseries refresh loop
  const per = state.lowPower ? 6000 : 2500;
  state.seriesTimer = setInterval(refreshAllSeries, per);
}


// Tehran time (always Asia/Tehran)
function tickClockTehran(){
  const fmt = new Intl.DateTimeFormat('fa-IR', {year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false, timeZone:'Asia/Tehran'});
  document.getElementById('now').textContent = fmt.format(new Date());
}
setClockTimer(); tickClockTehran(); applyLowPowerUI();

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
  updateCardStates(j.servers);

}


function updateCardStates(list){
  for(const s of list){
    const card = document.getElementById('card-'+s.name);
    if(!card) continue;
    const d = card.querySelector('.dot');
    if (d){
      d.classList.toggle('ok', !!s.connected);
      d.classList.toggle('bad', !s.connected);
    }
    const eb = card.querySelector('.edit-btn');
    if (eb){
      eb.style.display = s.connected ? 'none' : 'inline-block';
    }
  }
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
          <button class="btn edit-btn" style="display:none" onclick="openOfflineEditor('${s.name}')">ویرایش</button>
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
  scheduleLoops(interval);
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
  if (!state.servers || state.servers.length===0) return;
  if (!state.lowPower){
    for(const s of state.servers){ await refreshSeriesFor(s.name); }
  }else{
    const i = state._nextIdx % state.servers.length;
    const target = state.servers[i]; state._nextIdx++;
    if (target) await refreshSeriesFor(target.name);
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

function openServerEditor(name){
  try{
    const m = document.getElementById('msmodal');
    if (!m){ alert('Server editor not found'); return; }
    const item = (state.servers||[]).find(x=>x.name===name) || null;
    m.style.display = 'flex';
    const F = (id, val)=>{ const el=document.getElementById(id); if(el) el.value = (val!=null? String(val): ''); };
    F('ms-name', item? item.name : name || '');
    F('ms-host', item? (item.host||'') : '');
    F('ms-port', item? (item.port||22) : 22);
    F('ms-user', item? (item.username||'root') : 'root');
    F('ms-pass', item? (item.password||'') : '');
    F('ms-key',  item? (item.key_path||'') : '');
    // focus first empty field
    const firstEmpty = ['ms-host','ms-user','ms-pass','ms-key'].map(id=>document.getElementById(id)).find(el=>el && !el.value);
    (firstEmpty || document.getElementById('ms-host')).focus();
  }catch(e){ console.error(e); }
}
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

document.getElementById('btn-global-term').addEventListener('click', ()=>{
  const t = document.getElementById('g-stagger');
  if (t){ t.textContent = state.lowPower ? 'کم‌مصرف: روشن' : 'کم‌مصرف: خاموش'; }
});

document.getElementById('g-sendall').addEventListener('click', sendAll);
document.getElementById('g-stop').addEventListener('click', stopGlobal);
document.getElementById('g-int').addEventListener('click', async ()=>{
  if(!gSid){ alert('سشن فعال نیست'); return; }
  try{ await doPOST('/api/term/interrupt/'+encodeURIComponent(gSid)); }catch(e){ alert('خطا: '+e.message); }
});
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
// --- Global Terminal Low-Power wrapper ---
const _appendPaneNow = appendPane;
let GT_STAGGER_ON = false;
const paneQueues = new Map();
const paneOrder = [];
let flushIdx = 0;
let FLUSH_MS = 80;
let flushTimer = null;

function ensureFlushTimer(){
  if (flushTimer) return;
  flushTimer = setInterval(()=>{
    if (!GT_STAGGER_ON) return;
    if (paneOrder.length === 0) return;
    const name = paneOrder[flushIdx % paneOrder.length];
    flushIdx++;
    const q = paneQueues.get(name);
    if (!q || q.length === 0) return;
    const chunk = q.splice(0, q.length).join('');
    _appendPaneNow(name, chunk);
  }, FLUSH_MS);
}

appendPane = function(name, txt){
  if (!GT_STAGGER_ON) { _appendPaneNow(name, txt); return; }
  if (!paneQueues.has(name)) paneQueues.set(name, []);
  paneQueues.get(name).push(txt);
  if (!paneOrder.includes(name)) paneOrder.push(name);
  ensureFlushTimer();
};

// toggle button (terminal modal controls GLOBAL low-power)
document.addEventListener('click', (ev)=>{
  const t = ev.target;
  if (t && t.id === 'g-stagger'){
    try{
      state.lowPower = !state.lowPower;
      localStorage.setItem('mssm_lp', state.lowPower ? '1' : '0');
      // When low-power is ON, also stagger terminal output to lighten the UI.
      if (typeof GT_STAGGER_ON !== 'undefined') window.GT_STAGGER_ON = state.lowPower;
      t.textContent = state.lowPower ? 'کم‌مصرف: روشن' : 'کم‌مصرف: خاموش';
      if (typeof scheduleLoops === 'function') scheduleLoops(state.lastInterval || 3);
      if (typeof applyLowPowerUI === 'function') applyLowPowerUI();
    }catch(e){}
  }
});

document.body.addEventListener('click', (e)=>{ const t=e.target; if (t.dataset && t.dataset.sendOne){ sendOne(t.dataset.sendOne); } });


async function sendAllRaw(payload){
  if (!gSid) { alert('سشن فعال نیست'); return; }
  const headers = {'Content-Type':'application/json'};
  if (state.token) headers['X-Auth-Token'] = state.token;
  await fetch('/api/term/input_all/'+encodeURIComponent(gSid), {method:'POST', headers, body: JSON.stringify({data: payload})});
}
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
  await fetch('/api/term/input_all/'+encodeURIComponent(gSid), {method:'POST', headers, body: JSON.stringify({data: val+"\n"})});
}
async function sendOne(name){
  if (!gSid) return;
  const val = prompt('ورودی فقط برای '+name+':',''); if (val===null) return;
  const headers = {'Content-Type':'application/json'};
  if (state.token) headers['X-Auth-Token'] = state.token;
  await fetch('/api/term/input_one/'+encodeURIComponent(gSid)+'/'+encodeURIComponent(name), {method:'POST', headers, body: JSON.stringify({data: val+"\n"})});
}

async function forceStopAll(){
  if (!gSid){ return; }
  try{ await sendAllRaw('\u0003'); }catch(e){}
  const sid = gSid;
  // آزادسازی فوری UI
  gSid = null;
  if (gES){ try{ gES.close(); }catch(e){} gES = null; }
  const sidEl = document.getElementById('g-sid'); if (sidEl) sidEl.textContent = 'SID: —';
  // fire-and-forget close on server
  const headers = {'Content-Type':'application/json'};
  if (state.token) headers['X-Auth-Token'] = state.token;
  try{ fetch('/api/term/close/'+encodeURIComponent(sid), {method:'POST', headers}); }catch(e){}
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
  if (!r.ok){ alert('Top fetch failed'); return; }
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

<script>
(function(){
  function ensureHelpers(){
    window.state = window.state || {lowPower:false};
    if (typeof applyLowPowerUI !== 'function'){
      window.applyLowPowerUI = function(){
        var b = document.getElementById('lp-toggle');
        if (b) b.textContent = 'کم‌مصرف: ' + (state.lowPower ? 'روشن' : 'خاموش');
      }
    }
    if (typeof setClockTimer !== 'function'){
      window.setClockTimer = function(){
        try{ if (state.clockTimer){ clearInterval(state.clockTimer); } }catch(e){}
        var period = state.lowPower ? 5000 : 1000;
        state.clockTimer = setInterval(window.tickClockTehran || function(){}, period);
      }
    }
    if (typeof scheduleLoops !== 'function'){
      window.scheduleLoops = function(interval){
        state.lastInterval = interval || state.lastInterval || 3;
        try{ if (state.summaryTimer) clearTimeout(state.summaryTimer); }catch(e){}
        try{ if (state.seriesTimer) clearInterval(state.seriesTimer); }catch(e){}
        var k = state.lowPower ? 3 : 1;
        state.summaryTimer = setTimeout(window.fetchSummary || function(){}, (state.lastInterval * k) * 1000);
        var per = state.lowPower ? 6000 : 2500;
        state.seriesTimer = setInterval(window.refreshAllSeries || function(){}, per);
      }
    }
    if (typeof toggleLowPower !== 'function'){
      window.toggleLowPower = function(){
        state.lowPower = !state.lowPower;
        try{ localStorage.setItem('mssm_lp', state.lowPower ? '1' : '0'); }catch(e){}
        applyLowPowerUI(); setClockTimer(); scheduleLoops(state.lastInterval || 3);
      }
    }
  }

  function ensureButton(){
    var btn = document.getElementById('lp-toggle');
    if (!btn){
      btn = document.createElement('button');
      btn.id = 'lp-toggle';
      btn.className = 'btn';
      btn.title = 'کاهش مصرف CPU/شبکه';
      btn.style.marginInlineStart = '6px';
      btn.textContent = 'کم‌مصرف: ' + (window.state && state.lowPower ? 'روشن' : 'خاموش');
      var host = document.querySelector('.chips') || document.querySelector('header') || document.body;
      host.appendChild(btn);
    }
    if (!btn.dataset.bound){
      btn.addEventListener('click', function(){ try{ toggleLowPower(); }catch(e){} });
      btn.dataset.bound = '1';
    }
  }

  function boot(){
    ensureHelpers();
    ensureButton();
    if (window.applyLowPowerUI) applyLowPowerUI();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
</script>


<script>
(function(){
  function bootLP(){
    try{
      var btn = document.getElementById('lp-toggle');
      if(!btn) return;
      if(!btn.dataset.bound){
        btn.addEventListener('click', toggleLowPower);
        btn.dataset.bound = '1';
      }
      if (typeof applyLowPowerUI === 'function') applyLowPowerUI();
      if (typeof setClockTimer  === 'function') setClockTimer();
    }catch(e){ console.error('[LP bind]', e); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootLP);
  else bootLP();
})();
</script>


<script>
/* LP binder */
(function(){
  function bootLP(){
    try{
      var btn = document.getElementById('lp-toggle');
      if(!btn) return;
      if(!btn.dataset.bound){
        btn.addEventListener('click', toggleLowPower);
        btn.dataset.bound = '1';
      }
      if (typeof applyLowPowerUI === 'function') applyLowPowerUI();
      if (typeof setClockTimer  === 'function') setClockTimer();
    }catch(e){ console.error('[LP bind]', e); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootLP);
  else bootLP();
})();
</script>


<!-- Edit Server Modal (lightweight) -->
<div id="editmodal" class="modal" role="dialog" aria-modal="true" style="display:none">
  <div class="box" style="width:min(520px,92vw)">
    <div class="head" style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <div class="title" style="font-weight:700">ویرایش اتصال سرور</div>
      <div class="sub" id="edit-sub"></div>
      <div class="actions" style="margin-inline-start:auto">
        <button class="btn" id="edit-close">بستن</button>
      </div>
    </div>
    <div class="row">
      <div><label>IP/Host</label><input id="edit-host" placeholder="1.2.3.4"></div>
      <div><label>Port</label><input id="edit-port" value="22"></div>
    </div>
    <div class="row" style="margin-top:10px">
      <div><label>Username</label><input id="edit-user" value="root"></div>
      <div><label>Password</label><input id="edit-pass" placeholder="(اختیاری اگر کلید دارید)"></div>
      <div><label>Key path</label><input id="edit-key" placeholder="~/.ssh/id_rsa (اختیاری)"></div>
    </div>
    <div class="actions" style="gap:8px">
      <button class="btn" id="edit-save">ذخیره</button>
    </div>
  </div>
</div>


<script>
let editTargetName = null;
function openOfflineEditor(name){
  editTargetName = name;
  const m = document.getElementById('editmodal');
  const item = (state.servers||[]).find(x=>x.name===name) || null;
  document.getElementById('edit-sub').textContent = '('+name+')';
  const get = (id)=>document.getElementById(id);
  get('edit-host').value = item? (item.host||'') : '';
  get('edit-port').value = item? (item.port||22) : 22;
  get('edit-user').value = item? (item.username||'root') : 'root';
  get('edit-pass').value = item? (item.password||'') : '';
  get('edit-key').value  = item? (item.key_path||'') : '';
  m.style.display='flex';
}
document.getElementById('edit-close').addEventListener('click', ()=>{ document.getElementById('editmodal').style.display='none'; });
document.getElementById('edit-save').addEventListener('click', async ()=>{
  if (!editTargetName) return;
  const headers = {'Content-Type':'application/json'}; if (state.token) headers['X-Auth-Token']=state.token;
  const body = {
    name: editTargetName,
    host: document.getElementById('edit-host').value.trim(),
    port: parseInt(document.getElementById('edit-port').value.trim()||'22', 10),
    username: document.getElementById('edit-user').value.trim(),
  };
  const pwd = document.getElementById('edit-pass').value;
  const key = document.getElementById('edit-key').value.trim();
  if (pwd !== '') body.password = pwd; else body.password = null;
  body.key_path = key || null;
  const r = await fetch('/api/server/update', {method:'POST', headers, body: JSON.stringify(body)});
  if (!r.ok){ alert(await r.text()); return; }
  document.getElementById('editmodal').style.display='none';
  try{ alert('ذخیره شد. در حال تلاش برای اتصال مجدد...'); }catch(e){}
  setTimeout(fetchSummary, 300);
});
</script>


<script>
// --- Terminal pane sizing persistence & init ---
(function(){
  function loadTermSizes(){
    try{ return JSON.parse(localStorage.getItem('mssm_term_sizes')||'{}'); }catch(e){ return {}; }
  }
  function saveTermSizes(map){
    try{ localStorage.setItem('mssm_term_sizes', JSON.stringify(map)); }catch(e){}
  }
  const termSizes = loadTermSizes();

  const _appendPane = appendPane;
  appendPane = function(name, txt){
    const existed = !!document.getElementById('g-out-'+name);
    _appendPane(name, txt);
    if (!existed){
      try{
        const pane = document.getElementById('g-out-'+name)?.closest('.g-pane') || null;
        if (pane){
          if (!pane.querySelector('.corner-grip')){
            const grip = document.createElement('div'); grip.className='corner-grip'; pane.appendChild(grip);
          }
          let sz = termSizes[name];
          if (!sz){
            const card = document.getElementById('card-'+name);
            if (card){
              const r = card.getBoundingClientRect();
              sz = {w: Math.max( Math.round(r.width), 360 ), h: Math.max( Math.round(r.height), 240 )};
            }else{
              sz = {w: 480, h: 300};
            }
          }
          pane.style.width  = (sz.w) + 'px';
          pane.style.height = (sz.h) + 'px';

          if (!pane._ro){
            let t=null;
            pane._ro = new ResizeObserver(()=>{
              if (t) clearTimeout(t);
              t = setTimeout(()=>{
                try{
                  const w = Math.round(pane.getBoundingClientRect().width);
                  const h = Math.round(pane.getBoundingClientRect().height);
                  termSizes[name] = {w,h};
                  saveTermSizes(termSizes);
                }catch(e){}
              }, 220);
            });
            pane._ro.observe(pane);
          }
        }
      }catch(e){ console.error('term pane init size', e); }
    }
  };

  function applySavedSizes(){
    try{
      const sizes = loadTermSizes();
      for(const k in sizes){
        const pane = document.getElementById('g-out-'+k)?.closest('.g-pane');
        if (pane){
          pane.style.width = sizes[k].w + 'px';
          pane.style.height = sizes[k].h + 'px';
        }
      }
    }catch(e){}
  }
  document.addEventListener('DOMContentLoaded', ()=>{
    setTimeout(applySavedSizes, 500);
  });
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
    HOST = os.getenv("HOST", "0.0.0.0"); PORT = int(os.getenv("PORT", "8000"))
    app.run(host=HOST, port=PORT, debug=False)


# ======================= [APPEND PRO UI] Incident Monitor (Ping + TCP + Pro Buttons) =======================
import subprocess, re, socket, json as _json, platform, time as _time
from collections import deque as _deque

UP_PORTS_PATH = os.path.join(PERSIST_DIR, "uptime_ports.json")

def _load_ports():
    try:
        with open(UP_PORTS_PATH, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}

def _save_ports(d):
    try:
        with open(UP_PORTS_PATH, "w", encoding="utf-8") as f:
            _json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[UPCFG] save error:", e)

def _status_path(self, st):
    safe = "".join(c for c in st.cfg.name if c.isalnum() or c in ("-", "_"))
    return os.path.join(PERSIST_DIR, f"status_{safe}.ndjson")

def _append_status(self, st, ok_ping, ms, ok_tcp):
    try:
        with open(self._status_path(st), "a", encoding="utf-8") as f:
            f.write(_json.dumps({"t": time.time(), "ok": bool(ok_ping), "ms": ms, "tcp": bool(ok_tcp)}) + "\\n")
    except Exception as e:
        print(f"[STATUS] append error for {st.cfg.name}: {e}")

def _ensure_probe_attrs(st):
    if not hasattr(st, "probe_hist"):
        st.probe_hist = _deque(maxlen=6*60*24)  # 24h @ 10s
    if not hasattr(st, "last_ping_ms"):
        st.last_ping_ms = None
    if not hasattr(st, "_status_loaded"):
        st._status_loaded = False



def _load_status(self, st, seconds: int = 86400):
    """Load uptime probe history, ensuring dynamic attributes exist first."""
    try:
        # Ensure dynamic attrs
        if not hasattr(st, "probe_hist"):
            from collections import deque as __dq
            st.probe_hist = __dq(maxlen=6*60*24)  # ~24h at 10s resolution
        if not hasattr(st, "last_ping_ms"):
            st.last_ping_ms = None
        if not hasattr(st, "_status_loaded"):
            st._status_loaded = False

        pth = self._status_path(st)
        if not os.path.exists(pth):
            st._status_loaded = True
            return

        cutoff = time.time() - seconds
        with open(pth, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    d = _json.loads(ln)
                    if d.get("t", 0) >= cutoff:
                        # Backward-compatible fields
                        if "ok" not in d: d["ok"] = False
                        if "tcp" not in d: d["tcp"] = False
                        st.probe_hist.append(d)
                        if d.get("ms") is not None:
                            st.last_ping_ms = d["ms"]
                except Exception:
                    continue
        st._status_loaded = True
        print(f"[STATUS] Loaded {len(st.probe_hist)} probes for {st.cfg.name}")
    except Exception as e:
        print(f"[STATUS] load error for {st.cfg.name}: {e}")
        st._status_loaded = True


def _probe_ping_tcp(self, st, tcp_port: int):
    """Single probe: ICMP ping + TCP connect to port (latency fallback)."""
    # Ensure history is ready
    if not hasattr(st, "probe_hist"):
        from collections import deque as __dq
        st.probe_hist = __dq(maxlen=6*60*24)
    if not getattr(st, "_status_loaded", False):
        try: self._load_status(st)
        except Exception: pass

    ok_ping = False
    ms = None
    ok_tcp = False

    # ICMP ping (OS-aware)
    try:
        sys = platform.system().lower()
        if "windows" in sys:
            cmd = ["ping", "-n", "1", "-w", "1000", st.cfg.host]  # 1 try, 1s timeout
        else:
            cmd = ["ping", "-n", "-c", "1", "-w", "1", st.cfg.host]  # linux-like
        r = subprocess.run(cmd, capture_output=True, text=True)
        ok_ping = (r.returncode == 0)
        if ok_ping:
            m = re.search(r"time[=<]([\d\.]+)\s*ms", (r.stdout or "") + " " + (r.stderr or ""))
            if m:
                try:
                    ms = float(m.group(1))
                except Exception:
                    ms = None
    except Exception:
        pass

    # TCP port probe (+ latency hint if ICMP blocked)
    t0 = time.time()
    try:
        with socket.create_connection((st.cfg.host, int(tcp_port)), timeout=1.0) as s:
            ok_tcp = True
    except Exception:
        ok_tcp = False
    tcp_ms = (time.time() - t0) * 1000.0
    if ms is None and ok_tcp:
        ms = tcp_ms

    st.last_ping_ms = ms
    st.probe_hist.append({"t": time.time(), "ok": ok_ping, "ms": ms, "tcp": ok_tcp})
    self._append_status(st, ok_ping, ms, ok_tcp)

Monitor._status_path = _status_path
Monitor._append_status = _append_status
Monitor._load_status = _load_status
Monitor._probe_ping_tcp = _probe_ping_tcp

# Custom loop (adds 10s probes)
_Original_Loop = Monitor._loop
def _loop_with_probe(self):
    self._last_probe_tick = getattr(self, "_last_probe_tick", 0)
    ports_cfg = _load_ports()
    while not self._stop.is_set():
        t0 = time.time()
        for st in list(self.states):
            try:
                if not st.connected or not st.ssh:
                    self._connect(st)
                self._poll_server(st)
            except Exception as e:
                st.last_err = str(e); st.connected = False
                try:
                    if st.ssh: st.ssh.close()
                except Exception: pass
                st.ssh = None
            try:
                if time.time() - self._last_probe_tick >= 10:
                    port = int(ports_cfg.get(st.cfg.name) or ports_cfg.get(st.cfg.host) or 80)
                    self._probe_ping_tcp(st, port)
            except Exception:
                pass
        if time.time() - self._last_probe_tick >= 10:
            self._last_probe_tick = time.time()
        dt = time.time() - t0
        time.sleep(max(0.0, self.interval - dt))

Monitor._loop = _loop_with_probe

# Patch start() to also load uptime status on boot so UI has history instantly
_Original_Start = Monitor.start
def _start_with_status(self):
    try:
        print(f"[DEBUG] Starting monitor for {len(self.states)} servers, interval={self.interval}s (with uptime preload)")
        for st in self.states:
            self._connect(st)
            self._load_persist(st)
            try:
                self._load_status(st, seconds=86400)
            except Exception:
                pass
        self._thread.start()
    except Exception:
        # fallback to original behavior
        _Original_Start(self)
Monitor.start = _start_with_status


# ----------------------------- APIs -----------------------------
@app.get("/api/uptime")
def api_uptime():
    assert MON is not None
    name = request.args.get("name","")
    seconds = int(request.args.get("seconds","600"))
    st = next((x for x in MON.states if x.cfg.name == name or x.cfg.host == name), None)
    if not st: abort(404, "server not found")
    _ensure_probe_attrs(st)
    if not getattr(st, "_status_loaded", False):
        MON._load_status(st, seconds=max(seconds, 3600))
    cutoff = time.time() - seconds
    series = [p for p in list(st.probe_hist) if p["t"] >= cutoff]
    return jsonify({"name": st.cfg.name, "series": series})

@app.get("/api/uptime_summary")
def api_uptime_summary():
    assert MON is not None
    name = request.args.get("name","")
    st = next((x for x in MON.states if x.cfg.name == name or x.cfg.host == name), None)
    if not st: abort(404, "server not found")
    _ensure_probe_attrs(st)
    if not getattr(st, "_status_loaded", False):
        MON._load_status(st)
    def pct(seconds, key):
        cutoff = time.time() - seconds
        pts = [p for p in list(st.probe_hist) if p["t"] >= cutoff]
        if not pts: return None
        ok = sum(1 for p in pts if bool(p.get(key)))
        return (ok/len(pts))*100.0
    def ping_avg(seconds):
        cutoff = time.time() - seconds
        vals = [p.get("ms") for p in list(st.probe_hist) if p["t"] >= cutoff and p.get("ms") is not None]
        if not vals: return None
        return sum(vals)/len(vals)
    ports_cfg = _load_ports()
    port = int(ports_cfg.get(st.cfg.name) or ports_cfg.get(st.cfg.host) or 80)
    return jsonify({
        "uptime_ping_1h": pct(3600, "ok"),
        "uptime_ping_24h": pct(86400, "ok"),
        "uptime_tcp_1h": pct(3600, "tcp"),
        "uptime_tcp_24h": pct(86400, "tcp"),
        "ping_avg_24h": ping_avg(86400),
        "last_ms": getattr(st, "last_ping_ms", None),
        "tcp_port": port
    })

@app.get("/api/uptime_settings")
def api_uptime_settings_get():
    return jsonify(_load_ports())

@app.post("/api/uptime_settings")
def api_uptime_settings_set():
    data = request.get_json(silent=True) or {}
    target = data.get("target"); port = data.get("port")
    if not target or not port:
        return jsonify({"error":"target and port required"}), 400
    ports = _load_ports(); ports[target] = int(port); _save_ports(ports)
    return jsonify({"status":"ok","port":int(port)})

# ======================= [APPEND v7] Robust TCP probe (IPv4/IPv6 + timeout + mode) =======================
import ssl as _ssl

# Extend settings to support: {"port": int, "timeout_ms": int, "mode": "connect|http|tls"}
def _load_ports_v2():
    try:
        with open(UP_PORTS_PATH, "r", encoding="utf-8") as f:
            d = _json.load(f)
        # backward-compat: values may be integers (port)
        out = {}
        for k, v in d.items():
            if isinstance(v, int):
                out[k] = {"port": v, "timeout_ms": 2000, "mode": "connect"}
            elif isinstance(v, dict):
                out[k] = {
                    "port": int(v.get("port", 80)),
                    "timeout_ms": int(v.get("timeout_ms", 2000)),
                    "mode": str(v.get("mode", "connect")).lower()
                }
            else:
                out[k] = {"port": 80, "timeout_ms": 2000, "mode": "connect"}
        return out
    except Exception:
        return {}

def _save_ports_v2(d):
    # keep as dict with port/timeout/mode
    try:
        with open(UP_PORTS_PATH, "w", encoding="utf-8") as f:
            _json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[UPCFG] save error:", e)

def _coerce_port_cfg(ports_cfg, st):
    item = ports_cfg.get(st.cfg.name) or ports_cfg.get(st.cfg.host) or {"port":80,"timeout_ms":2000,"mode":"connect"}
    # sanitize
    port = int(item.get("port", 80))
    timeout_ms = int(item.get("timeout_ms", 2000))
    mode = str(item.get("mode", "connect")).lower()
    if timeout_ms < 500: timeout_ms = 500
    if mode not in ("connect","http","tls"): mode = "connect"
    return port, timeout_ms, mode

def _connect_any(host, port, timeout_s):
    last_err = None
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    for family, socktype, proto, canonname, sockaddr in infos:
        s = None
        try:
            s = socket.socket(family, socktype, proto)
            s.settimeout(timeout_s)
            t0 = time.time()
            s.connect(sockaddr)
            return s, (time.time()-t0)*1000.0  # socket + connect ms
        except Exception as e:
            last_err = e
            try:
                if s: s.close()
            except Exception:
                pass
        # try next
    raise last_err if last_err else OSError("connect failed")

# Replace probe to accept (port, timeout, mode)
def _probe_ping_tcp_v2(self, st, tcp_port: int, timeout_ms: int, mode: str):
    # Ensure history is ready
    if not hasattr(st, "probe_hist"):
        from collections import deque as __dq
        st.probe_hist = __dq(maxlen=6*60*24)
    if not getattr(st, "_status_loaded", False):
        try: self._load_status(st)
        except Exception: pass

    ok_ping = False
    ms = None
    ok_tcp = False

    # ICMP ping (OS-aware)
    try:
        sys = platform.system().lower()
        if "windows" in sys:
            cmd = ["ping", "-n", "1", "-w", str(max(1000, timeout_ms)), st.cfg.host]  # ms
        else:
            # linux uses seconds for -w, but allow ~ceil
            wsec = max(1, int(round(timeout_ms/1000.0)))
            cmd = ["ping", "-n", "-c", "1", "-w", str(wsec), st.cfg.host]
        r = subprocess.run(cmd, capture_output=True, text=True)
        ok_ping = (r.returncode == 0)
        if ok_ping:
            m = re.search(r"time[=<]([\d\.]+)\s*ms", (r.stdout or "") + " " + (r.stderr or ""))
            if m:
                try: ms = float(m.group(1))
                except Exception: ms = None
    except Exception:
        pass

    # TCP probe
    tcp_ms = None
    sock = None
    try:
        sock, conn_ms = _connect_any(st.cfg.host, int(tcp_port), timeout_ms/1000.0)
        ok_tcp = True
        tcp_ms = conn_ms
        if mode == "http":
            try:
                # Minimal HEAD to check app readiness
                req = "HEAD / HTTP/1.1\\r\\nHost: {}\\r\\nConnection: close\\r\\n\\r\\n".format(st.cfg.host)
                sock.sendall(req.encode("ascii", "ignore"))
                sock.settimeout(max(0.5, timeout_ms/1000.0))
                _ = sock.recv(1)  # any byte back is success
            except Exception as e:
                ok_tcp = False
        elif mode == "tls":
            try:
                ctx = _ssl.create_default_context()
                # don't enforce cert validity for liveness
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                t1 = time.time()
                with ctx.wrap_socket(sock, server_hostname=st.cfg.host) as tls:
                    pass  # handshake happens on wrap
                tcp_ms = (time.time()-t1)*1000.0 + (tcp_ms or 0.0)
            except Exception as e:
                ok_tcp = False
    except Exception as e:
        ok_tcp = False
    finally:
        try:
            if sock: sock.close()
        except Exception:
            pass
    # fallback latency disabled: keep ms None when ICMP is blocked
    st.last_ping_ms = ms
    st.probe_hist.append({"t": time.time(), "ok": ok_ping, "ms": ms, "tcp": ok_tcp})
    self._append_status(st, ok_ping, ms, ok_tcp)

# Patch loop to reload settings each probe tick
def _loop_with_probe_v7(self):
    self._last_probe_tick = getattr(self, "_last_probe_tick", 0)
    while not self._stop.is_set():
        t0 = time.time()
        for st in list(self.states):
            try:
                if not st.connected or not st.ssh:
                    self._connect(st)
                self._poll_server(st)
            except Exception as e:
                st.last_err = str(e); st.connected = False
                try:
                    if st.ssh: st.ssh.close()
                except Exception: pass
                st.ssh = None

            try:
                if time.time() - self._last_probe_tick >= 10:
                    cfg = _load_ports_v2()
                    port, to_ms, mode = _coerce_port_cfg(cfg, st)
                    _probe_ping_tcp_v2(self, st, port, to_ms, mode)
            except Exception:
                pass

        if time.time() - self._last_probe_tick >= 10:
            self._last_probe_tick = time.time()

        dt = time.time() - t0
        time.sleep(max(0.0, self.interval - dt))

Monitor._loop = _loop_with_probe_v7

# Update APIs to expose mode/timeout and accept updates
@app.get("/api/uptime_summary")
def api_uptime_summary_v7():
    assert MON is not None
    name = request.args.get("name","")
    st = next((x for x in MON.states if x.cfg.name == name or x.cfg.host == name), None)
    if not st: abort(404, "server not found")
    def pct(seconds, key):
        cutoff = time.time() - seconds
        pts = [p for p in list(getattr(st, "probe_hist", [])) if p["t"] >= cutoff]
        if not pts: return None
        ok = sum(1 for p in pts if bool(p.get(key)))
        return (ok/len(pts))*100.0
    def ping_avg(seconds):
        cutoff = time.time() - seconds
        vals = [p.get("ms") for p in list(getattr(st, "probe_hist", [])) if p["t"] >= cutoff and p.get("ms") is not None]
        if not vals: return None
        return sum(vals)/len(vals)
    cfg = _load_ports_v2()
    port, to_ms, mode = _coerce_port_cfg(cfg, st)
    return jsonify({
        "uptime_ping_1h": pct(3600, "ok"),
        "uptime_ping_24h": pct(86400, "ok"),
        "uptime_tcp_1h": pct(3600, "tcp"),
        "uptime_tcp_24h": pct(86400, "tcp"),
        "ping_avg_24h": ping_avg(86400),
        "last_ms": getattr(st, "last_ping_ms", None),
        "tcp_port": int(port),
        "tcp_timeout_ms": int(to_ms),
        "tcp_mode": mode
    })

@app.get("/api/uptime_settings")
def api_uptime_settings_get_v7():
    return jsonify(_load_ports_v2())

@app.post("/api/uptime_settings")
def api_uptime_settings_set_v7():
    data = request.get_json(silent=True) or {}
    target = data.get("target")
    port = data.get("port")
    timeout_ms = data.get("timeout_ms", 2000)
    mode = str(data.get("mode", "connect")).lower()
    if not target or not port:
        return jsonify({"error":"target and port required"}), 400
    ports = _load_ports_v2()
    ports[target] = {"port": int(port), "timeout_ms": int(timeout_ms), "mode": mode if mode in ("connect","http","tls") else "connect"}
    _save_ports_v2(ports)
    return jsonify({"status":"ok","port":int(port),"timeout_ms":int(timeout_ms),"mode":ports[target]["mode"]})

# UI: extend settings prompt to accept "port[,timeout_ms][,mode]"
# We only modify the uptime page template; we keep look & feel consistent.
def _inject_ui_prompt_upgrade(html):
    # simplified: no-op (UI already directly patched)
    return html

# Try to patch UPTIME_HTML if present
try:
    UPTIME_HTML = _inject_ui_prompt_upgrade(UPTIME_HTML)
except Exception as _e:
    print("[UI] inject prompt upgrade skipped:", _e)
# ======================= [END v7] =======================

# ----------------------------- Pages -----------------------------
NAV_WRAPPER_HTML = r"""
<!doctype html><html lang="fa" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Multi-Server SSH Monitor — ناوبری</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
:root{--bg:#0f1115;--text:#e5e7eb;--border:#262c3e;--r1:#6366f1;--r2:#a855f7;}
*{box-sizing:border-box} html,body{height:100%}
body{margin:0;background:linear-gradient(180deg,#0c0f14,#0f1115 25%,#0f1115);color:var(--text);font-family:Inter,system-ui,Segoe UI,Roboto,Arial,sans-serif}
.top{position:sticky;top:0;z-index:10;backdrop-filter:saturate(160%) blur(8px);background:rgba(16,18,26,.55);border-bottom:1px solid rgba(255,255,255,.06);}
.top .in{display:flex;gap:8px;align-items:center;padding:12px 14px}
.brand{font-weight:800}
.tabs{margin-inline-start:auto;display:flex;gap:10px}
.tab{border:1px solid var(--border);background:linear-gradient(135deg,var(--r1),var(--r2));color:#fff;padding:10px 18px;border-radius:12px;text-decoration:none;font-size:14px;font-weight:700;box-shadow:0 6px 14px rgba(99,102,241,.35);transition:transform .18s ease, box-shadow .18s ease;display:inline-flex;align-items:center;gap:8px}
.tab.secondary{background:linear-gradient(135deg,#8b5cf6,#d946ef)}
.tab:hover{transform:translateY(-1px);box-shadow:0 10px 20px rgba(255,77,109,.45)}
.frame{height:calc(100vh - 52px)} .frame iframe{width:100%;height:100%;border:0}

.btn:hover{transform:translateY(-1px);box-shadow:0 8px 16px rgba(129,140,248,.45)}
.mini canvas{display:block;width:100% !important;height:100% !important;}

/* terminal resize styles */
.g-pane{ resize: both; min-width: 320px; min-height: 200px; overflow: hidden; position: relative; }
.g-body{ height: 100%; overflow: auto; scrollbar-width: none; } /* Firefox hide */
.g-body::-webkit-scrollbar{ width:0; height:0; } /* WebKit hide */
.g-pane .corner-grip{ position:absolute; right:4px; bottom:4px; width:12px; height:12px; border:1px dashed rgba(255,255,255,.25); border-right:none; border-bottom:none; transform: rotate(45deg); pointer-events:none; opacity:.6; }
</style></head><body>
<div class="top"><div class="in">
  <div class="brand">Multi-Server SSH Monitor</div>
  <div class="tabs">
    <a class="tab" href="/home"><i class="fa-solid fa-house"></i><i class="fa-solid fa-house"></i> خانه</a>
    <a class="tab secondary" href="/uptime"><i class="fa-solid fa-wave-square"></i><i class="fa-solid fa-wave-square"></i> پایش اختلال</a>
  </div>
</div></div>
<div class="frame"><iframe src="/"></iframe></div>

<script>
(function(){
  function ensureHelpers(){
    window.state = window.state || {lowPower:false};
    if (typeof applyLowPowerUI !== 'function'){
      window.applyLowPowerUI = function(){
        var b = document.getElementById('lp-toggle');
        if (b) b.textContent = 'کم‌مصرف: ' + (state.lowPower ? 'روشن' : 'خاموش');
      }
    }
    if (typeof setClockTimer !== 'function'){
      window.setClockTimer = function(){
        try{ if (state.clockTimer){ clearInterval(state.clockTimer); } }catch(e){}
        var period = state.lowPower ? 5000 : 1000;
        state.clockTimer = setInterval(window.tickClockTehran || function(){}, period);
      }
    }
    if (typeof scheduleLoops !== 'function'){
      window.scheduleLoops = function(interval){
        state.lastInterval = interval || state.lastInterval || 3;
        try{ if (state.summaryTimer) clearTimeout(state.summaryTimer); }catch(e){}
        try{ if (state.seriesTimer) clearInterval(state.seriesTimer); }catch(e){}
        var k = state.lowPower ? 3 : 1;
        state.summaryTimer = setTimeout(window.fetchSummary || function(){}, (state.lastInterval * k) * 1000);
        var per = state.lowPower ? 6000 : 2500;
        state.seriesTimer = setInterval(window.refreshAllSeries || function(){}, per);
      }
    }
    if (typeof toggleLowPower !== 'function'){
      window.toggleLowPower = function(){
        state.lowPower = !state.lowPower;
        try{ localStorage.setItem('mssm_lp', state.lowPower ? '1' : '0'); }catch(e){}
        applyLowPowerUI(); setClockTimer(); scheduleLoops(state.lastInterval || 3);
      }
    }
  }

  function ensureButton(){
    var btn = document.getElementById('lp-toggle');
    if (!btn){
      btn = document.createElement('button');
      btn.id = 'lp-toggle';
      btn.className = 'btn';
      btn.title = 'کاهش مصرف CPU/شبکه';
      btn.style.marginInlineStart = '6px';
      btn.textContent = 'کم‌مصرف: ' + (window.state && state.lowPower ? 'روشن' : 'خاموش');
      var host = document.querySelector('.chips') || document.querySelector('header') || document.body;
      host.appendChild(btn);
    }
    if (!btn.dataset.bound){
      btn.addEventListener('click', function(){ try{ toggleLowPower(); }catch(e){} });
      btn.dataset.bound = '1';
    }
  }

  function boot(){
    ensureHelpers();
    ensureButton();
    if (window.applyLowPowerUI) applyLowPowerUI();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
</script>


<script>
(function(){
  function bootLP(){
    try{
      var btn = document.getElementById('lp-toggle');
      if(!btn) return;
      if(!btn.dataset.bound){
        btn.addEventListener('click', toggleLowPower);
        btn.dataset.bound = '1';
      }
      if (typeof applyLowPowerUI === 'function') applyLowPowerUI();
      if (typeof setClockTimer  === 'function') setClockTimer();
    }catch(e){ console.error('[LP bind]', e); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootLP);
  else bootLP();
})();
</script>


<script>
/* LP binder */
(function(){
  function bootLP(){
    try{
      var btn = document.getElementById('lp-toggle');
      if(!btn) return;
      if(!btn.dataset.bound){
        btn.addEventListener('click', toggleLowPower);
        btn.dataset.bound = '1';
      }
      if (typeof applyLowPowerUI === 'function') applyLowPowerUI();
      if (typeof setClockTimer  === 'function') setClockTimer();
    }catch(e){ console.error('[LP bind]', e); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootLP);
  else bootLP();
})();
</script>


<!-- Edit Server Modal (lightweight) -->
<div id="editmodal" class="modal" role="dialog" aria-modal="true" style="display:none">
  <div class="box" style="width:min(520px,92vw)">
    <div class="head" style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <div class="title" style="font-weight:700">ویرایش اتصال سرور</div>
      <div class="sub" id="edit-sub"></div>
      <div class="actions" style="margin-inline-start:auto">
        <button class="btn" id="edit-close">بستن</button>
      </div>
    </div>
    <div class="row">
      <div><label>IP/Host</label><input id="edit-host" placeholder="1.2.3.4"></div>
      <div><label>Port</label><input id="edit-port" value="22"></div>
    </div>
    <div class="row" style="margin-top:10px">
      <div><label>Username</label><input id="edit-user" value="root"></div>
      <div><label>Password</label><input id="edit-pass" placeholder="(اختیاری اگر کلید دارید)"></div>
      <div><label>Key path</label><input id="edit-key" placeholder="~/.ssh/id_rsa (اختیاری)"></div>
    </div>
    <div class="actions" style="gap:8px">
      <button class="btn" id="edit-save">ذخیره</button>
    </div>
  </div>
</div>


<script>
let editTargetName = null;
function openOfflineEditor(name){
  editTargetName = name;
  const m = document.getElementById('editmodal');
  const item = (state.servers||[]).find(x=>x.name===name) || null;
  document.getElementById('edit-sub').textContent = '('+name+')';
  const get = (id)=>document.getElementById(id);
  get('edit-host').value = item? (item.host||'') : '';
  get('edit-port').value = item? (item.port||22) : 22;
  get('edit-user').value = item? (item.username||'root') : 'root';
  get('edit-pass').value = item? (item.password||'') : '';
  get('edit-key').value  = item? (item.key_path||'') : '';
  m.style.display='flex';
}
document.getElementById('edit-close').addEventListener('click', ()=>{ document.getElementById('editmodal').style.display='none'; });
document.getElementById('edit-save').addEventListener('click', async ()=>{
  if (!editTargetName) return;
  const headers = {'Content-Type':'application/json'}; if (state.token) headers['X-Auth-Token']=state.token;
  const body = {
    name: editTargetName,
    host: document.getElementById('edit-host').value.trim(),
    port: parseInt(document.getElementById('edit-port').value.trim()||'22', 10),
    username: document.getElementById('edit-user').value.trim(),
  };
  const pwd = document.getElementById('edit-pass').value;
  const key = document.getElementById('edit-key').value.trim();
  if (pwd !== '') body.password = pwd; else body.password = null;
  body.key_path = key || null;
  const r = await fetch('/api/server/update', {method:'POST', headers, body: JSON.stringify(body)});
  if (!r.ok){ alert(await r.text()); return; }
  document.getElementById('editmodal').style.display='none';
  try{ alert('ذخیره شد. در حال تلاش برای اتصال مجدد...'); }catch(e){}
  setTimeout(fetchSummary, 300);
});
</script>


<script>
// --- Terminal pane sizing persistence & init ---
(function(){
  function loadTermSizes(){
    try{ return JSON.parse(localStorage.getItem('mssm_term_sizes')||'{}'); }catch(e){ return {}; }
  }
  function saveTermSizes(map){
    try{ localStorage.setItem('mssm_term_sizes', JSON.stringify(map)); }catch(e){}
  }
  const termSizes = loadTermSizes();

  const _appendPane = appendPane;
  appendPane = function(name, txt){
    const existed = !!document.getElementById('g-out-'+name);
    _appendPane(name, txt);
    if (!existed){
      try{
        const pane = document.getElementById('g-out-'+name)?.closest('.g-pane') || null;
        if (pane){
          if (!pane.querySelector('.corner-grip')){
            const grip = document.createElement('div'); grip.className='corner-grip'; pane.appendChild(grip);
          }
          let sz = termSizes[name];
          if (!sz){
            const card = document.getElementById('card-'+name);
            if (card){
              const r = card.getBoundingClientRect();
              sz = {w: Math.max( Math.round(r.width), 360 ), h: Math.max( Math.round(r.height), 240 )};
            }else{
              sz = {w: 480, h: 300};
            }
          }
          pane.style.width  = (sz.w) + 'px';
          pane.style.height = (sz.h) + 'px';

          if (!pane._ro){
            let t=null;
            pane._ro = new ResizeObserver(()=>{
              if (t) clearTimeout(t);
              t = setTimeout(()=>{
                try{
                  const w = Math.round(pane.getBoundingClientRect().width);
                  const h = Math.round(pane.getBoundingClientRect().height);
                  termSizes[name] = {w,h};
                  saveTermSizes(termSizes);
                }catch(e){}
              }, 220);
            });
            pane._ro.observe(pane);
          }
        }
      }catch(e){ console.error('term pane init size', e); }
    }
  };

  function applySavedSizes(){
    try{
      const sizes = loadTermSizes();
      for(const k in sizes){
        const pane = document.getElementById('g-out-'+k)?.closest('.g-pane');
        if (pane){
          pane.style.width = sizes[k].w + 'px';
          pane.style.height = sizes[k].h + 'px';
        }
      }
    }catch(e){}
  }
  document.addEventListener('DOMContentLoaded', ()=>{
    setTimeout(applySavedSizes, 500);
  });
})();
</script>

</body></html>
"""

UPTIME_HTML = r"""
<!doctype html><html lang="fa" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>پایش اختلال — Multi-Server SSH Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0f1115;--panel:#141824;--text:#e5e7eb;--muted:#9aa0aa;--ok:#16c47f;--err:#ff5d5d;--border:#262c3e;--r1:#6366f1;--r2:#a855f7;}
*{box-sizing:border-box} html,body{height:100%}
body{margin:0;background:linear-gradient(180deg,#0c0f14,#0f1115 25%,#0f1115);color:var(--text);font-family:Inter,system-ui,Segoe UI,Roboto,Arial,sans-serif}
.top{position:sticky;top:0;z-index:10;backdrop-filter:saturate(160%) blur(8px);background:rgba(16,18,26,.55);border-bottom:1px solid rgba(255,255,255,.06);}
.top .in{display:flex;gap:8px;align-items:center;padding:12px 14px}
.brand{font-weight:800}
.tabs{margin-inline-start:auto;display:flex;gap:10px}
.tab{border:1px solid var(--border);background:linear-gradient(135deg,var(--r1),var(--r2));color:#fff;padding:10px 18px;border-radius:12px;text-decoration:none;font-size:14px;font-weight:700;box-shadow:0 6px 14px rgba(99,102,241,.35);transition:transform .18s ease, box-shadow .18s ease;display:inline-flex;align-items:center;gap:8px}
.tab.secondary{background:linear-gradient(135deg,#8b5cf6,#d946ef)}
.tab:hover{transform:translateY(-1px);box-shadow:0 10px 20px rgba(255,77,109,.45)}
.container{max-width:1200px;margin:16px auto;padding:0 16px 60px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:14px}
.card{background:linear-gradient(180deg,#13192a,#121726);border:1px solid #22283a;border-radius:14px;box-shadow:0 6px 18px rgba(0,0,0,.35);padding:12px;overflow:hidden}
.head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.title{font-weight:700}
.sub{color:var(--muted);font-size:12px}
.bars2{display:grid;grid-template-columns:repeat(60,1fr);gap:3px;margin-top:6px}
.bars2 .b{height:10px;border-radius:6px;background:#3a2222}
.bars2 .b.ok{background:var(--ok)}
.rowlbl{display:flex;align-items:center;gap:8px;margin-top:4px;color:var(--muted);font-size:12px}
.kpi{display:flex;gap:14px;margin-top:8px;color:#cfd6e4;font-size:12.5px}
.kpi b{color:#fff}
.canvas-wrap{height:120px;margin-top:8px}
.actions{margin-inline-start:auto;display:flex;gap:6px}
.btn{border:1px solid var(--border);background:linear-gradient(135deg,#818cf8,#a78bfa);color:#fff;padding:8px 12px;border-radius:10px;font-size:12.5px;cursor:pointer;box-shadow:0 4px 10px rgba(129,140,248,.35);transition:transform .15s ease, box-shadow .15s ease;display:inline-flex;align-items:center;gap:6px}
.btn:hover{transform:translateY(-1px);box-shadow:0 8px 16px rgba(255,93,115,.45)}
.mini canvas{display:block;width:100% !important;height:100% !important;}

/* terminal resize styles */
.g-pane{ resize: both; min-width: 320px; min-height: 200px; overflow: hidden; position: relative; }
.g-body{ height: 100%; overflow: auto; scrollbar-width: none; } /* Firefox hide */
.g-body::-webkit-scrollbar{ width:0; height:0; } /* WebKit hide */
.g-pane .corner-grip{ position:absolute; right:4px; bottom:4px; width:12px; height:12px; border:1px dashed rgba(255,255,255,.25); border-right:none; border-bottom:none; transform: rotate(45deg); pointer-events:none; opacity:.6; }
</style>
</head><body>
<div class="top"><div class="in">
  <div class="brand">Multi-Server SSH Monitor</div>
  <div class="tabs">
    <a class="tab" href="/home"><i class="fa-solid fa-house"></i><i class="fa-solid fa-house"></i> خانه</a>
    <a class="tab secondary" href="/uptime"><i class="fa-solid fa-wave-square"></i><i class="fa-solid fa-wave-square"></i> پایش اختلال</a>
  </div>
</div></div>

<div class="container">
  <div id="grid" class="grid"></div>
</div>

<script>
const state = { servers: [], charts:{}, ports:{} };

function makePing(ctx){
  return new Chart(ctx,{
    type:'line',
    data:{labels:[], datasets:[{label:'Ping (ms)', data:[], tension:.35, fill:true}]},
    options:{ responsive:true, maintainAspectRatio:false, animation:false, plugins:{legend:{display:false}}, scales:{x:{display:false}, y:{display:true}} }
  });
}

async function fetchSummary(){
  const [sumR, portsR] = await Promise.all([fetch('/api/summary'), fetch('/api/uptime_settings')]);
  const sumJ = await sumR.json(); state.servers = sumJ.servers;
  state.ports = await portsR.json();
  renderCards(); refreshAll();
}

function cardHtml(s){
  const port = state.ports[s.name] || state.ports[s.host] || 80;
  return `
    <div class="head">
      <div class="title">${s.name}</div>
      <div class="sub" style="margin-inline-start:6px">(${s.host})</div>
      <div class="actions"><button class="btn" data-setport="${s.name}"><i class="fa-solid fa-gear"></i> <i class="fa-solid fa-gear"></i> تنظیم پورت TCP</button></div>
    </div>
    <div class="rowlbl">Ping (60 نقطه اخیر)</div>
    <div class="bars2" id="bars-ping-${s.name}"></div>
    <div class="rowlbl">TCP پورت <b id="lblport-${s.name}" style="color:#fff">${port}</b> (60 نقطه اخیر)</div>
    <div class="bars2" id="bars-tcp-${s.name}"></div>
    <div class="kpi">
      <div>آپ‌تایم Ping (۱h): <b id="upP1-${s.name}">—</b></div>
      <div>آپ‌تایم Ping (۲۴h): <b id="upP24-${s.name}">—</b></div>
      <div>آپ‌تایم TCP (۱h): <b id="upT1-${s.name}">—</b></div>
      <div>آپ‌تایم TCP (۲۴h): <b id="upT24-${s.name}">—</b></div>
      <div>میانگین پینگ (۲۴h): <b id="avgPing-${s.name}">—</b></div>
      <div>آخرین پینگ: <b id="lastPing-${s.name}">—</b></div>
    </div>
    <div class="canvas-wrap"><canvas id="ping-${s.name}"></canvas></div>
  `;
}

function renderCards(){
  const grid = document.getElementById('grid'); grid.innerHTML='';
  for(const s of state.servers){
    const card = document.createElement('div'); card.className='card'; card.id='card-'+s.name;
    card.innerHTML = cardHtml(s);
    grid.appendChild(card);
    state.charts[s.name] = { ping: makePing(document.getElementById('ping-'+s.name).getContext('2d')) };
  }
}

document.addEventListener('click', async (e)=>{
  const t = e.target.closest('[data-setport]'); if (!t) return;
  const name = t.getAttribute('data-setport');
  const curPort = (state.ports[name]?.port || state.ports[name] || 80);
  const curTo = (state.ports[name]?.timeout_ms || 2000);
  const curMode = (state.ports[name]?.mode || 'connect');
  const val = prompt('پورت/تنظیمات برای '+name+' :\nفرمت: port[,timeout_ms][,mode]  (مثال: 443,3000,tls)', `${curPort},${curTo},${curMode}`);
  if (val===null) return;
  const parts = val.split(/\s*,\s*/);
  const port = parseInt(parts[0]||'',10);
  const timeout_ms = parseInt(parts[1]||'2000',10);
  const mode = (parts[2]||'connect').toLowerCase();
  if (!port || port<1 || port>65535) return alert('پورت نامعتبر');
  const body = {target:name, port, timeout_ms, mode};
  const r = await fetch('/api/uptime_settings',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if (!r.ok){ alert(await r.text()); return; }
  state.ports[name]=port;
  const lbl = document.getElementById('lblport-'+name); if (lbl) lbl.textContent = String(port);
  await refreshOne(name);
});

async function refreshOne(name){
  const r = await fetch('/api/uptime?name='+encodeURIComponent(name)+'&seconds=600');
  if (!r.ok) return; const j = await r.json();
  const series = j.series.slice(-60);
  const pingWrap = document.getElementById('bars-ping-'+name);
  const tcpWrap  = document.getElementById('bars-tcp-'+name);
  if (pingWrap){ pingWrap.innerHTML = series.map(p=> `<div class="b${p.ok?' ok':''}"></div>`).join(''); }
  if (tcpWrap){  tcpWrap.innerHTML  = series.map(p=> `<div class="b${p.tcp?' ok':''}"></div>`).join(''); }

  const s2 = await fetch('/api/uptime_summary?name='+encodeURIComponent(name));
  if (s2.ok){
    const x = await s2.json();
    const set = (id, v, suf)=>{ const el=document.getElementById(id); if (el) el.textContent = (v==null?'—': v.toFixed(2)+(suf||'')); };
    set('upP1-'+name, x.uptime_ping_1h, '%'); set('upP24-'+name, x.uptime_ping_24h, '%');
    set('upT1-'+name, x.uptime_tcp_1h, '%');  set('upT24-'+name, x.uptime_tcp_24h, '%');
    const a = document.getElementById('avgPing-'+name); if (a) a.textContent = (x.ping_avg_24h==null?'—':x.ping_avg_24h.toFixed(1)+' ms');
    const l = document.getElementById('lastPing-'+name); if (l) l.textContent = (x.last_ms==null?'—':x.last_ms.toFixed(1)+' ms');
    const lbl = document.getElementById('lblport-'+name); if (lbl) lbl.textContent = x.tcp_port;
    const l2 = document.getElementById('lblmode-'+name); if (l2) l2.textContent = `mode: ${x.tcp_mode} • timeout: ${x.tcp_timeout_ms}ms`;
  }
  const ch = state.charts[name]?.ping;
  if (ch){
    const labels = j.series.map(p=> new Date(p.t*1000).toLocaleTimeString('fa-IR',{hour12:false,timeZone:'Asia/Tehran'}));
    const data = j.series.map(p=> (p.ms==null? null : p.ms));
    ch.data.labels = labels; ch.data.datasets[0].data = data; ch.update('none');
  }
}

async function refreshAll(){
  for(const s of state.servers){ await refreshOne(s.name); }
  setTimeout(refreshAll, 5000);
}

fetchSummary();
</script>

<script>
(function(){
  function ensureHelpers(){
    window.state = window.state || {lowPower:false};
    if (typeof applyLowPowerUI !== 'function'){
      window.applyLowPowerUI = function(){
        var b = document.getElementById('lp-toggle');
        if (b) b.textContent = 'کم‌مصرف: ' + (state.lowPower ? 'روشن' : 'خاموش');
      }
    }
    if (typeof setClockTimer !== 'function'){
      window.setClockTimer = function(){
        try{ if (state.clockTimer){ clearInterval(state.clockTimer); } }catch(e){}
        var period = state.lowPower ? 5000 : 1000;
        state.clockTimer = setInterval(window.tickClockTehran || function(){}, period);
      }
    }
    if (typeof scheduleLoops !== 'function'){
      window.scheduleLoops = function(interval){
        state.lastInterval = interval || state.lastInterval || 3;
        try{ if (state.summaryTimer) clearTimeout(state.summaryTimer); }catch(e){}
        try{ if (state.seriesTimer) clearInterval(state.seriesTimer); }catch(e){}
        var k = state.lowPower ? 3 : 1;
        state.summaryTimer = setTimeout(window.fetchSummary || function(){}, (state.lastInterval * k) * 1000);
        var per = state.lowPower ? 6000 : 2500;
        state.seriesTimer = setInterval(window.refreshAllSeries || function(){}, per);
      }
    }
    if (typeof toggleLowPower !== 'function'){
      window.toggleLowPower = function(){
        state.lowPower = !state.lowPower;
        try{ localStorage.setItem('mssm_lp', state.lowPower ? '1' : '0'); }catch(e){}
        applyLowPowerUI(); setClockTimer(); scheduleLoops(state.lastInterval || 3);
      }
    }
  }

  function ensureButton(){
    var btn = document.getElementById('lp-toggle');
    if (!btn){
      btn = document.createElement('button');
      btn.id = 'lp-toggle';
      btn.className = 'btn';
      btn.title = 'کاهش مصرف CPU/شبکه';
      btn.style.marginInlineStart = '6px';
      btn.textContent = 'کم‌مصرف: ' + (window.state && state.lowPower ? 'روشن' : 'خاموش');
      var host = document.querySelector('.chips') || document.querySelector('header') || document.body;
      host.appendChild(btn);
    }
    if (!btn.dataset.bound){
      btn.addEventListener('click', function(){ try{ toggleLowPower(); }catch(e){} });
      btn.dataset.bound = '1';
    }
  }

  function boot(){
    ensureHelpers();
    ensureButton();
    if (window.applyLowPowerUI) applyLowPowerUI();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
</script>


<script>
(function(){
  function bootLP(){
    try{
      var btn = document.getElementById('lp-toggle');
      if(!btn) return;
      if(!btn.dataset.bound){
        btn.addEventListener('click', toggleLowPower);
        btn.dataset.bound = '1';
      }
      if (typeof applyLowPowerUI === 'function') applyLowPowerUI();
      if (typeof setClockTimer  === 'function') setClockTimer();
    }catch(e){ console.error('[LP bind]', e); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootLP);
  else bootLP();
})();
</script>


<script>
/* LP binder */
(function(){
  function bootLP(){
    try{
      var btn = document.getElementById('lp-toggle');
      if(!btn) return;
      if(!btn.dataset.bound){
        btn.addEventListener('click', toggleLowPower);
        btn.dataset.bound = '1';
      }
      if (typeof applyLowPowerUI === 'function') applyLowPowerUI();
      if (typeof setClockTimer  === 'function') setClockTimer();
    }catch(e){ console.error('[LP bind]', e); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootLP);
  else bootLP();
})();
</script>


<!-- Edit Server Modal (lightweight) -->
<div id="editmodal" class="modal" role="dialog" aria-modal="true" style="display:none">
  <div class="box" style="width:min(520px,92vw)">
    <div class="head" style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <div class="title" style="font-weight:700">ویرایش اتصال سرور</div>
      <div class="sub" id="edit-sub"></div>
      <div class="actions" style="margin-inline-start:auto">
        <button class="btn" id="edit-close">بستن</button>
      </div>
    </div>
    <div class="row">
      <div><label>IP/Host</label><input id="edit-host" placeholder="1.2.3.4"></div>
      <div><label>Port</label><input id="edit-port" value="22"></div>
    </div>
    <div class="row" style="margin-top:10px">
      <div><label>Username</label><input id="edit-user" value="root"></div>
      <div><label>Password</label><input id="edit-pass" placeholder="(اختیاری اگر کلید دارید)"></div>
      <div><label>Key path</label><input id="edit-key" placeholder="~/.ssh/id_rsa (اختیاری)"></div>
    </div>
    <div class="actions" style="gap:8px">
      <button class="btn" id="edit-save">ذخیره</button>
    </div>
  </div>
</div>


<script>
let editTargetName = null;
function openOfflineEditor(name){
  editTargetName = name;
  const m = document.getElementById('editmodal');
  const item = (state.servers||[]).find(x=>x.name===name) || null;
  document.getElementById('edit-sub').textContent = '('+name+')';
  const get = (id)=>document.getElementById(id);
  get('edit-host').value = item? (item.host||'') : '';
  get('edit-port').value = item? (item.port||22) : 22;
  get('edit-user').value = item? (item.username||'root') : 'root';
  get('edit-pass').value = item? (item.password||'') : '';
  get('edit-key').value  = item? (item.key_path||'') : '';
  m.style.display='flex';
}
document.getElementById('edit-close').addEventListener('click', ()=>{ document.getElementById('editmodal').style.display='none'; });
document.getElementById('edit-save').addEventListener('click', async ()=>{
  if (!editTargetName) return;
  const headers = {'Content-Type':'application/json'}; if (state.token) headers['X-Auth-Token']=state.token;
  const body = {
    name: editTargetName,
    host: document.getElementById('edit-host').value.trim(),
    port: parseInt(document.getElementById('edit-port').value.trim()||'22', 10),
    username: document.getElementById('edit-user').value.trim(),
  };
  const pwd = document.getElementById('edit-pass').value;
  const key = document.getElementById('edit-key').value.trim();
  if (pwd !== '') body.password = pwd; else body.password = null;
  body.key_path = key || null;
  const r = await fetch('/api/server/update', {method:'POST', headers, body: JSON.stringify(body)});
  if (!r.ok){ alert(await r.text()); return; }
  document.getElementById('editmodal').style.display='none';
  try{ alert('ذخیره شد. در حال تلاش برای اتصال مجدد...'); }catch(e){}
  setTimeout(fetchSummary, 300);
});
</script>


<script>
// --- Terminal pane sizing persistence & init ---
(function(){
  function loadTermSizes(){
    try{ return JSON.parse(localStorage.getItem('mssm_term_sizes')||'{}'); }catch(e){ return {}; }
  }
  function saveTermSizes(map){
    try{ localStorage.setItem('mssm_term_sizes', JSON.stringify(map)); }catch(e){}
  }
  const termSizes = loadTermSizes();

  const _appendPane = appendPane;
  appendPane = function(name, txt){
    const existed = !!document.getElementById('g-out-'+name);
    _appendPane(name, txt);
    if (!existed){
      try{
        const pane = document.getElementById('g-out-'+name)?.closest('.g-pane') || null;
        if (pane){
          if (!pane.querySelector('.corner-grip')){
            const grip = document.createElement('div'); grip.className='corner-grip'; pane.appendChild(grip);
          }
          let sz = termSizes[name];
          if (!sz){
            const card = document.getElementById('card-'+name);
            if (card){
              const r = card.getBoundingClientRect();
              sz = {w: Math.max( Math.round(r.width), 360 ), h: Math.max( Math.round(r.height), 240 )};
            }else{
              sz = {w: 480, h: 300};
            }
          }
          pane.style.width  = (sz.w) + 'px';
          pane.style.height = (sz.h) + 'px';

          if (!pane._ro){
            let t=null;
            pane._ro = new ResizeObserver(()=>{
              if (t) clearTimeout(t);
              t = setTimeout(()=>{
                try{
                  const w = Math.round(pane.getBoundingClientRect().width);
                  const h = Math.round(pane.getBoundingClientRect().height);
                  termSizes[name] = {w,h};
                  saveTermSizes(termSizes);
                }catch(e){}
              }, 220);
            });
            pane._ro.observe(pane);
          }
        }
      }catch(e){ console.error('term pane init size', e); }
    }
  };

  function applySavedSizes(){
    try{
      const sizes = loadTermSizes();
      for(const k in sizes){
        const pane = document.getElementById('g-out-'+k)?.closest('.g-pane');
        if (pane){
          pane.style.width = sizes[k].w + 'px';
          pane.style.height = sizes[k].h + 'px';
        }
      }
    }catch(e){}
  }
  document.addEventListener('DOMContentLoaded', ()=>{
    setTimeout(applySavedSizes, 500);
  });
})();
</script>

</body></html>
"""

@app.get("/uptime")
def view_uptime():
    return render_template_string(UPTIME_HTML)

@app.get("/home")
def view_home():
    return Response(NAV_WRAPPER_HTML, mimetype="text/html")
# ===================== [END APPEND PRO UI] =====================



# ======================= [APPEND v6] Socket.IO 404 silencer + SSH connect tweak =======================
from flask import Response as _FlaskResponse

# Stub routes to silence extensions/pages that try to hit /socket.io/*
@app.get("/socket.io/")
@app.get("/socket.io/<path:path>")
def _socketio_stub(path=None):
    return _FlaskResponse(status=204)

# Gentle tweak: increase Paramiko timeouts to reduce "Error reading SSH protocol banner" on slow/filtered links
_Original_Connect = Monitor._connect
def _connect_with_timeouts(self, st):
    print(f"[DEBUG] Connecting to {st.cfg.name} ({st.cfg.host})...")
    if st.ssh:
        try: st.ssh.close()
        except Exception: pass
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
                banner_timeout=20,   # was 10
                auth_timeout=20,     # was 10
                timeout=20,          # was 10
            )
        else:
            cli.connect(
                hostname=st.cfg.host,
                port=st.cfg.port,
                username=st.cfg.username,
                password=st.cfg.password,
                banner_timeout=20,
                auth_timeout=20,
                timeout=20,
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

Monitor._connect = _connect_with_timeouts
# ======================= [END v6] =======================


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
