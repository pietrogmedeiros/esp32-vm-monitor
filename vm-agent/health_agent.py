#!/usr/bin/env python3
"""
vm-health-agent — expõe a saúde da VM (systemd, docker, recursos) em HTTP JSON.

Somente biblioteca padrão do Python 3.8+. Sem dependências externas.

Endpoints:
  GET /health          JSON completo (para humanos / dashboards)
  GET /health/summary  JSON compacto de chaves curtas (para o ESP32)
  GET /ping            "pong" sem autenticação (health check do proxy)

Autenticação: header `Authorization: Bearer <token>`.
"""

import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATHS = [
    os.environ.get("HEALTH_AGENT_CONFIG", ""),
    "/etc/vm-health-agent/config.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
]

DEFAULTS = {
    "bind": "0.0.0.0",
    "port": 9099,
    "token": "",
    "hostname_override": None,
    # Units observadas explicitamente. Vazio = observa apenas as que falharam.
    "watch_services": [],
    "watch_disks": ["/"],
    # Containers que DEVEM estar rodando. Vazio = observa todos os existentes.
    "watch_containers": [],
    # O que fazer com servicos swarm em 0/0 (parados de proposito — no
    # Easypanel e a bolinha vermelha). "failure" derruba o status, "warning"
    # degrada, "ignore" nao reporta.
    "stopped_services": "warning",
    "thresholds": {
        "cpu_pct": 90.0,
        "mem_pct": 90.0,
        "disk_pct": 85.0,
        "load_per_core": 2.0,
    },
    "collect_interval_s": 10,
    "subprocess_timeout_s": 8,
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    for path in CONFIG_PATHS:
        if path and os.path.isfile(path):
            with open(path) as fh:
                user = json.load(fh)
            for key, value in user.items():
                if key == "thresholds" and isinstance(value, dict):
                    cfg["thresholds"].update(value)
                else:
                    cfg[key] = value
            cfg["_source"] = path
            break
    env_token = os.environ.get("HEALTH_TOKEN")
    if env_token:
        cfg["token"] = env_token
    # Num container, socket.gethostname() devolve o ID gerado pelo Docker
    # ("0afc4df8dd7e"), que é o que acaba virando título na tela do ESP32.
    env_host = os.environ.get("HEALTH_HOSTNAME")
    if env_host:
        cfg["hostname_override"] = env_host
    return cfg


def run(cmd, timeout):
    """Executa um comando e devolve stdout, ou None se falhar/não existir."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 and not proc.stdout.strip():
        return None
    return proc.stdout


# --------------------------------------------------------------------------
# Coletores
# --------------------------------------------------------------------------


def read_cpu_times():
    """Devolve (total, idle) de /proc/stat, ou None fora do Linux."""
    try:
        with open("/proc/stat") as fh:
            line = fh.readline()
    except OSError:
        return None
    parts = [float(x) for x in line.split()[1:]]
    if len(parts) < 4:
        return None
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)  # idle + iowait
    return sum(parts), idle


def collect_memory():
    try:
        with open("/proc/meminfo") as fh:
            info = {}
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = float(rest.split()[0]) * 1024  # kB -> bytes
    except OSError:
        return None
    total = info.get("MemTotal", 0.0)
    available = info.get("MemAvailable", info.get("MemFree", 0.0))
    if total <= 0:
        return None
    used = total - available
    return {
        "total_bytes": int(total),
        "used_bytes": int(used),
        "used_pct": round(used / total * 100, 1),
        "swap_total_bytes": int(info.get("SwapTotal", 0.0)),
        "swap_used_pct": (
            round(
                (info["SwapTotal"] - info.get("SwapFree", 0.0))
                / info["SwapTotal"]
                * 100,
                1,
            )
            if info.get("SwapTotal", 0.0) > 0
            else 0.0
        ),
    }


def collect_disks(mounts):
    disks = []
    for mount in mounts:
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        pct = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
        disks.append(
            {
                "mount": mount,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "used_pct": pct,
            }
        )
    return disks


def collect_load():
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):
        return None
    cores = os.cpu_count() or 1
    return {
        "load1": round(one, 2),
        "load5": round(five, 2),
        "load15": round(fifteen, 2),
        "cores": cores,
        "load_per_core": round(one / cores, 2),
    }


def collect_uptime():
    try:
        with open("/proc/uptime") as fh:
            return int(float(fh.readline().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def collect_systemd(watch, timeout):
    """Estado das units systemd. Devolve None se systemd não existir."""
    out = run(
        [
            "systemctl",
            "list-units",
            "--type=service",
            "--all",
            "--no-legend",
            "--no-pager",
            "--plain",
        ],
        timeout,
    )
    if out is None:
        return None

    units = {}
    for line in out.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 4 or not fields[0].endswith(".service"):
            continue
        units[fields[0]] = {
            "name": fields[0],
            "load": fields[1],
            "active": fields[2],
            "sub": fields[3],
            "description": fields[4].strip() if len(fields) > 4 else "",
        }

    services, bad = [], []
    # Se watch_services estiver definido, ele é a lista crítica; senão usamos
    # todas as units carregadas e reportamos as que falharam.
    names = watch if watch else sorted(units)
    for name in names:
        unit = units.get(name)
        if unit is None:
            # Unit observada que nem existe conta como falha.
            entry = {
                "name": name,
                "active": "not-found",
                "sub": "missing",
                "ok": False,
                "watched": True,
            }
        else:
            if watch:
                # Unit explicitamente observada: precisa estar de pé.
                # ('exited' cobre oneshots que rodaram com sucesso.)
                healthy = unit["active"] == "active" or unit["sub"] == "exited"
            else:
                # Varredura geral: 'inactive/dead' é normal em dezenas de units
                # (timers, oneshots desabilitados). Só 'failed' é problema.
                healthy = unit["active"] != "failed" and unit["sub"] != "failed"
            entry = {
                "name": unit["name"],
                "active": unit["active"],
                "sub": unit["sub"],
                "ok": healthy,
                "watched": bool(watch),
            }
        services.append(entry)
        if not entry["ok"]:
            bad.append(entry["name"])

    if not watch:
        # Sem lista explícita: só interessa quem está em failed.
        services = [s for s in services if not s["ok"]]
        bad = [s["name"] for s in services]

    return {
        "total": len(units),
        "bad": bad,
        "services": services,
    }


_HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting)\)")
_REPLICAS_RE = re.compile(r"^(\d+)\s*/\s*(\d+)")


def collect_swarm(watch, timeout):
    """Estado dos services do Docker Swarm.

    Em Swarm (Easypanel, Portainer, etc) a fonte de verdade e `docker service
    ls`, nao `docker ps`. Cada redeploy deixa para tras as tasks antigas em
    estado 'exited'; conta-las como falha gera alarme falso para servicos que
    estao perfeitamente de pe.

    Devolve None se o daemon nao estiver em modo swarm.
    """
    out = run(["docker", "service", "ls", "--format", "{{json .}}"], timeout)
    if out is None:
        return None

    services, bad, stopped = [], [], []
    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue

        name = raw.get("Name", "")
        replicas = raw.get("Replicas", "")
        match = _REPLICAS_RE.match(replicas)
        if match:
            running, desired = int(match.group(1)), int(match.group(2))
        else:
            running = desired = 0
        seen.add(name)

        if watch and name not in watch:
            continue

        # 0/0 nao e o mesmo que 0/1: o servico esta parado, nao caiu.
        # Quem decide a gravidade disso e a config 'stopped_services'.
        is_stopped = desired == 0
        healthy = is_stopped or running >= desired
        entry = {
            "name": name,
            "image": raw.get("Image", ""),
            "mode": raw.get("Mode", ""),
            "replicas": replicas,
            "running": running,
            "desired": desired,
            "stopped": is_stopped,
            "ok": healthy,
        }
        services.append(entry)
        if not healthy:
            bad.append(name)
        elif is_stopped:
            stopped.append(name)

    for name in watch:
        if name not in seen:
            services.append({
                "name": name, "image": "", "mode": "", "replicas": "0/0",
                "running": 0, "desired": 0, "stopped": False, "ok": False,
            })
            bad.append(name)

    return {"mode": "swarm", "total": len(services), "bad": bad,
            "stopped": stopped, "containers": services}


def collect_docker(watch, timeout):
    """Estado dos containers. Devolve None se o docker não estiver disponível."""
    out = run(
        ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], timeout
    )
    if out is None:
        return None

    containers, bad = [], []
    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = raw.get("Names", "").split(",")[0]
        state = raw.get("State", "")
        status = raw.get("Status", "")
        match = _HEALTH_RE.search(status)
        health = match.group(1) if match else "none"
        seen.add(name)

        if watch and name not in watch:
            continue

        healthy = state == "running" and health != "unhealthy"
        entry = {
            "name": name,
            "image": raw.get("Image", ""),
            "state": state,
            "status": status,
            "health": health,
            "ok": healthy,
        }
        containers.append(entry)
        if not healthy:
            bad.append(name)

    for name in watch:
        if name not in seen:
            containers.append(
                {
                    "name": name,
                    "image": "",
                    "state": "missing",
                    "status": "container não existe",
                    "health": "none",
                    "ok": False,
                }
            )
            bad.append(name)

    return {"mode": "standalone", "total": len(containers), "bad": bad,
            "stopped": [], "containers": containers}


def collect_containers(watch, timeout):
    """Swarm quando disponivel, senao containers avulsos."""
    swarm = collect_swarm(watch, timeout)
    if swarm is not None:
        return swarm
    return collect_docker(watch, timeout)


# --------------------------------------------------------------------------
# Amostrador em background
# --------------------------------------------------------------------------


class Collector(threading.Thread):
    """Coleta o snapshot periodicamente para que o HTTP responda instantaneamente."""

    daemon = True

    def __init__(self, cfg):
        super().__init__(name="collector")
        self.cfg = cfg
        self.lock = threading.Lock()
        self.snapshot = {"status": "unknown", "ok": False, "ts": int(time.time())}
        self._prev_cpu = read_cpu_times()
        self._stop = threading.Event()

    def cpu_percent(self):
        current = read_cpu_times()
        if current is None or self._prev_cpu is None:
            self._prev_cpu = current
            return None
        total_delta = current[0] - self._prev_cpu[0]
        idle_delta = current[1] - self._prev_cpu[1]
        self._prev_cpu = current
        if total_delta <= 0:
            return None
        return round((1.0 - idle_delta / total_delta) * 100, 1)

    def build(self):
        cfg = self.cfg
        timeout = cfg["subprocess_timeout_s"]
        thresholds = cfg["thresholds"]

        cpu = self.cpu_percent()
        memory = collect_memory()
        disks = collect_disks(cfg["watch_disks"])
        load = collect_load()
        uptime = collect_uptime()
        systemd = collect_systemd(cfg["watch_services"], timeout)
        docker = collect_containers(cfg["watch_containers"], timeout)

        problems = []
        # Serviços e containers derrubados são falha dura.
        if systemd and systemd["bad"]:
            problems += ["systemd:" + n for n in systemd["bad"]]
        if docker and docker["bad"]:
            problems += ["docker:" + n for n in docker["bad"]]

        # Recursos estourados degradam, mas não derrubam.
        warnings = []

        # Servicos parados de proposito: a gravidade e escolha do operador.
        policy = cfg.get("stopped_services", "warning")
        stopped = (docker or {}).get("stopped", [])
        if stopped and policy == "failure":
            problems += ["parado:" + n for n in stopped]
        elif stopped and policy == "warning":
            warnings += ["parado:" + n for n in stopped]

        down = bool(problems)
        if cpu is not None and cpu >= thresholds["cpu_pct"]:
            warnings.append(f"cpu:{cpu}%")
        if memory and memory["used_pct"] >= thresholds["mem_pct"]:
            warnings.append(f"mem:{memory['used_pct']}%")
        for disk in disks:
            if disk["used_pct"] >= thresholds["disk_pct"]:
                warnings.append(f"disk:{disk['mount']}:{disk['used_pct']}%")
        if load and load["load_per_core"] >= thresholds["load_per_core"]:
            warnings.append(f"load:{load['load_per_core']}")

        if down:
            status = "down"
        elif warnings:
            status = "degraded"
        else:
            status = "ok"

        return {
            "status": status,
            "ok": status == "ok",
            "ts": int(time.time()),
            "host": cfg["hostname_override"] or socket.gethostname(),
            "uptime_s": uptime,
            "agent_version": "1.0.0",
            "cpu_pct": cpu,
            "memory": memory,
            "disks": disks,
            "load": load,
            "systemd": systemd,
            "docker": docker,
            "problems": problems,
            "warnings": warnings,
        }

    def run(self):
        while not self._stop.is_set():
            try:
                built = self.build()
            except Exception as exc:  # nunca deixa a thread morrer
                built = {
                    "status": "down",
                    "ok": False,
                    "ts": int(time.time()),
                    "host": socket.gethostname(),
                    "problems": [f"agent:{type(exc).__name__}: {exc}"],
                    "warnings": [],
                }
            with self.lock:
                self.snapshot = built
            self._stop.wait(self.cfg["collect_interval_s"])

    def get(self):
        with self.lock:
            return self.snapshot


def summarize(full):
    """Versão compacta para o ESP32: chaves curtas, payload pequeno."""
    memory = full.get("memory") or {}
    load = full.get("load") or {}
    systemd = full.get("systemd") or {}
    docker = full.get("docker") or {}
    disks = full.get("disks") or []
    worst_disk = max((d["used_pct"] for d in disks), default=None)

    # A telinha do ESP32 mostra 'bad' literalmente. Avisos entram na lista
    # junto com as falhas — o campo 'st' ja diferencia a gravidade, e um
    # "DEGRADADO" sem dizer o porque nao ajuda ninguem.
    issues = list(full.get("problems", [])) + list(full.get("warnings", []))
    return {
        "ok": full.get("ok", False),
        "st": full.get("status", "unknown"),
        "ts": full.get("ts"),
        "host": full.get("host", "")[:24],
        "up": full.get("uptime_s"),
        "cpu": full.get("cpu_pct"),
        "mem": memory.get("used_pct"),
        "disk": worst_disk,
        "ld": load.get("load_per_core"),
        "svc": [len(systemd.get("services", [])) if systemd else 0,
                len(systemd.get("bad", [])) if systemd else 0],
        "dkr": [docker.get("total", 0) if docker else 0,
                len(docker.get("bad", [])) if docker else 0],
        # Limitado para caber com folga na RAM do ESP32.
        "bad": [i[:40] for i in issues[:8]],
        "nbad": len(issues),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "vm-health-agent/1.0"
    protocol_version = "HTTP/1.1"

    # injetados pelo main()
    collector = None
    token = ""

    def log_message(self, fmt, *args):
        sys.stderr.write(
            "%s - %s\n" % (self.address_string(), fmt % args)
        )

    def _send(self, code, payload, content_type="application/json"):
        body = (
            json.dumps(payload, ensure_ascii=False).encode()
            if content_type == "application/json"
            else payload.encode()
        )
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not self.token:
            return True  # token vazio = agente aberto (só use atrás de VPN/LAN)
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), self.token)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/ping":
            self._send(200, "pong", "text/plain")
            return

        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="health"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        full = self.collector.get()
        if path == "/health":
            self._send(200, full)
        elif path == "/health/summary":
            self._send(200, summarize(full))
        else:
            self._send(404, {"error": "not found", "paths": ["/health", "/health/summary", "/ping"]})


def main():
    cfg = load_config()
    if not cfg["token"]:
        sys.stderr.write(
            "AVISO: nenhum token configurado — o agente está aberto a qualquer "
            "requisição. Defina 'token' no config.json ou HEALTH_TOKEN.\n"
        )

    collector = Collector(cfg)
    collector.build()  # primeira coleta síncrona para não servir 'unknown'
    with collector.lock:
        collector.snapshot = collector.build()  # segunda: já com delta de CPU
    collector.start()

    Handler.collector = collector
    Handler.token = cfg["token"]

    server = ThreadingHTTPServer((cfg["bind"], cfg["port"]), Handler)
    sys.stderr.write(
        f"vm-health-agent ouvindo em http://{cfg['bind']}:{cfg['port']} "
        f"(config: {cfg.get('_source', 'padrões embutidos')})\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
