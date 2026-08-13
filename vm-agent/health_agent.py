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
from urllib.parse import unquote_plus

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
    # Serviços com tela dedicada no ESP32 (logs + anomalias). Funciona como
    # whitelist: o endpoint /service só aceita nomes que estejam aqui, para
    # que um token vazado não vire leitura arbitrária de log.
    "focus_services": [],
    "focus_log_lines": 300,
    # Regex customizáveis; vazio usa DEFAULT_ERROR_RE / DEFAULT_WARN_RE.
    "error_pattern": "",
    "warn_pattern": "",
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
    env_focus = os.environ.get("HEALTH_FOCUS_SERVICES")
    if env_focus:
        cfg["focus_services"] = [s.strip() for s in env_focus.split(",")
                                 if s.strip()]
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


def run_merged(cmd, timeout):
    """Como run(), mas junta stderr ao stdout.

    Necessário para logs: `docker logs` reproduz os fluxos do container, e o
    stderr da aplicação sai no stderr do CLI. É justamente lá que ficam as
    exceptions — ler só o stdout perderia quase toda anomalia.
    """
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Aqui o código de saída é o único sinal confiável. Com o stderr mesclado,
    # a mensagem de falha do próprio CLI ("Error: no such service") viria como
    # se fosse log da aplicação — e seria contada como anomalia dela.
    if proc.returncode != 0:
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


# Duas camadas: 'error' pega falha de verdade, 'warn' pega ruído que merece
# atenção. Palavras genéricas como "failed" ficam de fora de propósito — elas
# aparecem em log saudável ("failed to find cache, building") e afogariam o
# sinal numa tela de 3 linhas.
DEFAULT_ERROR_RE = (
    r"(?i)(\bERROR\b|\bFATAL\b|\bCRITICAL\b|\bPANIC\b|\bException\b|"
    r"\bTraceback\b|\bECONNREFUSED\b|\bUnhandled\b|\bsegfault\b|"
    r"\bForced shutdown\b|\bOOMKilled\b|\bout of memory\b)"
)
DEFAULT_WARN_RE = (
    r"(?i)(\bWARN\b|\bWARNING\b|\bdeprecated\b|\bretrying\b|"
    r"\bSIGTERM\b|\bshutting down\b)"
)

# Muito backend não prefixa nível nenhum: o log é de acesso HTTP puro
# ("GET /api/x 503 12.3 ms"). Sem isto, um serviço devolvendo 500 para todo
# mundo apareceria como log limpo. O método e a rota antes do código evitam
# casar com durações e outros números soltos da linha.
HTTP_5XX_RE = re.compile(
    r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+\s+(5\d{2})\b"
)

# Prefixo ISO que o --timestamps do docker coloca em cada linha.
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})\S*\s+(.*)$")


def _strip_timestamp(line):
    """Separa o carimbo do docker do texto. Devolve (hh:mm:ss, mensagem)."""
    match = _TS_RE.match(line)
    if match:
        return match.group(2), match.group(3)
    return "", line


def collect_service_logs(name, cfg, timeout, swarm=True):
    """Conta anomalias nas últimas linhas de log de um serviço."""
    tail = str(cfg.get("focus_log_lines", 300))
    cmd = (["docker", "service", "logs", name] if swarm
           else ["docker", "logs", name])
    cmd += ["--tail", tail, "--timestamps"]

    out = run_merged(cmd, timeout)
    if out is None:
        return None

    error_re = re.compile(cfg.get("error_pattern") or DEFAULT_ERROR_RE)
    warn_re = re.compile(cfg.get("warn_pattern") or DEFAULT_WARN_RE)

    errors, warns, scanned = 0, 0, 0
    recent = []
    tail = []
    last_error_at = ""

    for raw in out.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        scanned += 1
        stamp, message = _strip_timestamp(raw)
        # Swarm prefixa cada linha com o id da task; só polui a tela.
        message = re.sub(r"^\S+\.\d+\.\S+\s*\|\s*", "", message)

        http5xx = cfg.get("detect_http_5xx", True) and HTTP_5XX_RE.search(message)
        if error_re.search(message) or http5xx:
            errors += 1
            last_error_at = stamp or last_error_at
            recent.append({"t": stamp, "lvl": "err", "m": message[:120]})
        elif warn_re.search(message):
            warns += 1
            recent.append({"t": stamp, "lvl": "warn", "m": message[:120]})

        # Guarda as últimas linhas de qualquer nível. Sem isto, um serviço sem
        # anomalias mostraria uma tela vazia — e tela vazia não distingue
        # "está tudo bem" de "meu detector está cego".
        tail.append({"t": stamp, "lvl": "info", "m": message[:120]})
        if len(tail) > 4:
            tail.pop(0)

    # As mais novas primeiro: é o que interessa numa tela pequena.
    recent.reverse()
    tail.reverse()

    return {
        "scanned": scanned,
        "errors": errors,
        "warnings": warns,
        "last_error_at": last_error_at,
        "recent": recent[:12],
        "tail": tail,
    }


def collect_service_tasks(name, timeout):
    """Histórico de tasks do serviço — revela reinícios e crash loops."""
    out = run(["docker", "service", "ps", name, "--no-trunc",
               "--format", "{{json .}}"], timeout)
    if out is None:
        return None

    running, failed, last_error = 0, 0, ""
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = (raw.get("CurrentState") or "")
        if state.startswith("Running"):
            running += 1
        elif state.startswith(("Failed", "Rejected")):
            failed += 1
            if not last_error:
                last_error = (raw.get("Error") or "")[:120]

    return {"running_tasks": running, "failed_tasks": failed,
            "last_task_error": last_error}


def collect_focus(name, cfg, timeout, docker):
    """Visão detalhada de um único serviço: réplicas, tasks e anomalias."""
    swarm = bool(docker) and docker.get("mode") == "swarm"

    replicas, state_ok = "?", None
    for entry in (docker or {}).get("containers", []):
        if entry.get("name") == name:
            replicas = entry.get("replicas") or entry.get("state", "?")
            state_ok = entry.get("ok")
            break

    logs = collect_service_logs(name, cfg, timeout, swarm=swarm)
    tasks = collect_service_tasks(name, timeout) if swarm else None

    errors = (logs or {}).get("errors", 0)
    failed_tasks = (tasks or {}).get("failed_tasks", 0)

    if state_ok is False:
        status = "down"
    elif errors > 0 or failed_tasks > 0:
        status = "degraded"
    elif state_ok is None:
        status = "unknown"
    else:
        status = "ok"

    return {
        "name": name,
        "status": status,
        "replicas": replicas,
        "logs": logs,
        "tasks": tasks,
    }


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

        # Serviços com tela dedicada. Coletados aqui junto do resto para que o
        # HTTP continue respondendo de cache, sem disparar `docker logs` a
        # cada requisição do ESP32.
        focus = {}
        for name in cfg.get("focus_services", []):
            try:
                focus[name] = collect_focus(name, cfg, timeout, docker)
            except Exception as exc:
                focus[name] = {"name": name, "status": "unknown",
                               "replicas": "?", "logs": None, "tasks": None,
                               "error": f"{type(exc).__name__}: {exc}"}

        return {
            "status": status,
            "ok": status == "ok",
            "ts": int(time.time()),
            "focus": focus,
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


def summarize_focus(entry):
    """Versão compacta da tela de serviço, com chaves curtas.

    A tela do ESP32 mostra poucas linhas: mandamos 4 mensagens já cortadas em
    vez do log inteiro, para o payload continuar cabendo com folga na RAM.
    """
    logs = entry.get("logs") or {}
    tasks = entry.get("tasks") or {}
    # Sem anomalia, manda as últimas linhas: a tela mostra que o serviço está
    # vivo e logando, em vez de um vazio ambíguo.
    recent = logs.get("recent", [])[:4] or logs.get("tail", [])[:4]
    return {
        "name": entry.get("name", "")[:28],
        "st": entry.get("status", "unknown"),
        "rep": entry.get("replicas", "?"),
        "err": logs.get("errors", 0),
        "wrn": logs.get("warnings", 0),
        "scan": logs.get("scanned", 0),
        "lerr": logs.get("last_error_at", ""),
        "rt": tasks.get("running_tasks", 0),
        "ft": tasks.get("failed_tasks", 0),
        "msgs": [{"t": m.get("t", ""), "l": m.get("lvl", ""),
                  "m": m.get("m", "")[:64]} for m in recent],
    }


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
        elif path in ("/service", "/service/summary"):
            self._send_service(full, path.endswith("/summary"))
        else:
            self._send(404, {"error": "not found",
                             "paths": ["/health", "/health/summary",
                                       "/service", "/service/summary",
                                       "/ping"]})

    def _send_service(self, full, compact):
        """Detalhe de um serviço. Sem ?name=, devolve o primeiro configurado."""
        focus = full.get("focus") or {}
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        wanted = ""
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key == "name":
                wanted = unquote_plus(value)

        if not focus:
            self._send(404, {"error": "nenhum focus_service configurado"})
            return

        # Só nomes já coletados — o que equivale à whitelist de focus_services.
        # Sem isso, o parâmetro viraria leitura arbitrária de log.
        if wanted and wanted not in focus:
            self._send(404, {"error": "serviço não está em focus_services",
                             "available": sorted(focus)})
            return

        name = wanted or sorted(focus)[0]
        entry = focus[name]
        self._send(200, summarize_focus(entry) if compact else entry)


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
