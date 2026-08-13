#!/usr/bin/env python3
"""Testes dos parsers do health_agent (rodam em qualquer SO).

    python3 test_health_agent.py
"""
import json
import subprocess
import sys
import unittest
from unittest import mock

import health_agent as ha

SYSTEMCTL_OUT = """\
docker.service            loaded active   running Docker Application Container Engine
nginx.service             loaded failed   failed  A high performance web server
postgresql.service        loaded active   running PostgreSQL RDBMS
ssh.service               loaded active   running OpenBSD Secure Shell server
apt-daily.service         loaded inactive dead    Daily apt download activities
cloud-final.service       loaded active   exited  Execute cloud user/final scripts
"""

DOCKER_OUT = "\n".join(
    json.dumps(c)
    for c in [
        {"Names": "api", "Image": "meu/api:1.2", "State": "running",
         "Status": "Up 3 hours (healthy)"},
        {"Names": "worker", "Image": "meu/worker:1.2", "State": "running",
         "Status": "Up 3 hours"},
        {"Names": "redis", "Image": "redis:7", "State": "running",
         "Status": "Up 2 days (unhealthy)"},
        {"Names": "batch", "Image": "meu/batch:1", "State": "exited",
         "Status": "Exited (1) 5 minutes ago"},
    ]
)


class TestSystemd(unittest.TestCase):
    def collect(self, watch):
        with mock.patch.object(ha, "run", return_value=SYSTEMCTL_OUT):
            return ha.collect_systemd(watch, 5)

    def test_sem_watchlist_reporta_apenas_falhas(self):
        res = self.collect([])
        self.assertEqual(res["bad"], ["nginx.service"])
        self.assertEqual(res["total"], 6)
        # A lista detalhada só traz os problemáticos.
        self.assertEqual([s["name"] for s in res["services"]], ["nginx.service"])

    def test_sub_exited_conta_como_saudavel(self):
        res = self.collect(["cloud-final.service"])
        self.assertEqual(res["bad"], [])

    def test_inactive_em_watchlist_e_falha(self):
        res = self.collect(["apt-daily.service"])
        self.assertEqual(res["bad"], ["apt-daily.service"])

    def test_unit_inexistente_em_watchlist_e_falha(self):
        res = self.collect(["naoexiste.service"])
        self.assertEqual(res["bad"], ["naoexiste.service"])
        self.assertEqual(res["services"][0]["active"], "not-found")

    def test_watchlist_ignora_falhas_fora_dela(self):
        res = self.collect(["docker.service", "ssh.service"])
        self.assertEqual(res["bad"], [])  # nginx falhou mas não é observado

    def test_systemd_ausente(self):
        with mock.patch.object(ha, "run", return_value=None):
            self.assertIsNone(ha.collect_systemd([], 5))


class TestDocker(unittest.TestCase):
    def collect(self, watch):
        with mock.patch.object(ha, "run", return_value=DOCKER_OUT):
            return ha.collect_docker(watch, 5)

    def test_detecta_unhealthy_e_exited(self):
        res = self.collect([])
        self.assertEqual(sorted(res["bad"]), ["batch", "redis"])
        self.assertEqual(res["total"], 4)

    def test_parse_do_campo_health(self):
        by_name = {c["name"]: c for c in self.collect([])["containers"]}
        self.assertEqual(by_name["api"]["health"], "healthy")
        self.assertEqual(by_name["worker"]["health"], "none")
        self.assertEqual(by_name["redis"]["health"], "unhealthy")
        self.assertTrue(by_name["worker"]["ok"])  # running sem healthcheck = ok

    def test_watchlist_filtra(self):
        res = self.collect(["api", "worker"])
        self.assertEqual(res["bad"], [])
        self.assertEqual(res["total"], 2)

    def test_container_ausente_da_watchlist_e_falha(self):
        res = self.collect(["api", "fantasma"])
        self.assertEqual(res["bad"], ["fantasma"])
        missing = [c for c in res["containers"] if c["name"] == "fantasma"][0]
        self.assertEqual(missing["state"], "missing")

    def test_docker_ausente(self):
        with mock.patch.object(ha, "run", return_value=None):
            self.assertIsNone(ha.collect_docker([], 5))


SWARM_OUT = "\n".join(
    json.dumps(s)
    for s in [
        {"Name": "gob_elasticsearch", "Image": "elasticsearch:8",
         "Mode": "replicated", "Replicas": "1/1"},
        {"Name": "gob_grafana", "Image": "grafana/grafana",
         "Mode": "replicated", "Replicas": "1/1"},
        {"Name": "gob_shipsafe", "Image": "meu/shipsafe",
         "Mode": "replicated", "Replicas": "0/1"},
        {"Name": "gob_aniversario_benicio", "Image": "meu/aniv",
         "Mode": "replicated", "Replicas": "0/1"},
        {"Name": "gob_desligado", "Image": "meu/x",
         "Mode": "replicated", "Replicas": "0/0"},
        {"Name": "gob_global", "Image": "node-exporter",
         "Mode": "global", "Replicas": "2/2"},
    ]
)


class TestSwarm(unittest.TestCase):
    """Em Swarm, `docker ps -a` mente: tasks antigas ficam em 'exited' apos
    cada redeploy. A verdade esta nas replicas de `docker service ls`."""

    def collect(self, watch):
        with mock.patch.object(ha, "run", return_value=SWARM_OUT):
            return ha.collect_swarm(watch, 5)

    def test_replicas_zero_de_um_e_falha(self):
        res = self.collect([])
        self.assertEqual(sorted(res["bad"]),
                         ["gob_aniversario_benicio", "gob_shipsafe"])
        self.assertEqual(res["mode"], "swarm")
        self.assertEqual(res["total"], 6)

    def test_servico_escalado_para_zero_nao_e_falha(self):
        # 0/0 = parado, nao quebrado. Sai em 'stopped', nao em 'bad'.
        res = self.collect([])
        entry = [c for c in res["containers"] if c["name"] == "gob_desligado"][0]
        self.assertTrue(entry["ok"])
        self.assertTrue(entry["stopped"])
        self.assertIn("gob_desligado", res["stopped"])
        self.assertNotIn("gob_desligado", res["bad"])

    def test_parado_e_diferente_de_zero_de_um(self):
        res = self.collect([])
        # 0/1 = deveria ter 1 replica e nao tem -> falha
        self.assertIn("gob_shipsafe", res["bad"])
        self.assertNotIn("gob_shipsafe", res["stopped"])

    def test_modo_global_conta_replicas(self):
        entry = [c for c in self.collect([])["containers"]
                 if c["name"] == "gob_global"][0]
        self.assertTrue(entry["ok"])
        self.assertEqual((entry["running"], entry["desired"]), (2, 2))

    def test_tasks_antigas_nao_geram_falso_positivo(self):
        # Este era o bug: 4 tasks 'exited' do elasticsearch viravam 4 falhas,
        # mesmo com o servico rodando 1/1.
        res = self.collect([])
        self.assertNotIn("gob_elasticsearch", res["bad"])

    def test_watchlist_filtra(self):
        res = self.collect(["gob_grafana", "gob_shipsafe"])
        self.assertEqual(res["bad"], ["gob_shipsafe"])
        self.assertEqual(res["total"], 2)

    def test_servico_ausente_da_watchlist_e_falha(self):
        res = self.collect(["gob_fantasma"])
        self.assertEqual(res["bad"], ["gob_fantasma"])

    def test_replicas_ilegivel_nao_quebra(self):
        weird = json.dumps({"Name": "x", "Replicas": "sei la", "Mode": "r"})
        with mock.patch.object(ha, "run", return_value=weird):
            res = ha.collect_swarm([], 5)
        self.assertTrue(res["containers"][0]["ok"])  # desired=0 -> nao alarma

    def test_fora_do_swarm_cai_para_docker_ps(self):
        # `docker service ls` falha em daemon sem swarm; o agente deve usar
        # `docker ps -a` sem ficar cego.
        calls = []

        def fake_run(cmd, timeout):
            calls.append(cmd)
            if "service" in cmd:
                return None          # nao e swarm manager
            return DOCKER_OUT

        with mock.patch.object(ha, "run", side_effect=fake_run):
            res = ha.collect_containers([], 5)
        self.assertEqual(res["mode"], "standalone")
        self.assertEqual(sorted(res["bad"]), ["batch", "redis"])
        self.assertEqual(len(calls), 2)


LOGS_OUT = "\n".join([
    "2026-08-13T00:10:01.123456789Z lemon-meet_backend.1.abc123 | INFO servidor ouvindo em :3000",
    "2026-08-13T00:10:05.000000000Z lemon-meet_backend.1.abc123 | WARN deprecated api /v1/rooms",
    "2026-08-13T00:11:02.000000000Z lemon-meet_backend.1.abc123 | ERROR ECONNREFUSED postgres:5432",
    "2026-08-13T00:11:02.100000000Z lemon-meet_backend.1.abc123 | Traceback (most recent call last):",
    "2026-08-13T00:12:30.000000000Z lemon-meet_backend.1.abc123 | INFO reconectado ao banco",
    "",
    "2026-08-13T00:13:00.000000000Z lemon-meet_backend.1.abc123 | info: failed to find cache, building fresh",
])


class TestServiceLogs(unittest.TestCase):
    CFG = {"focus_log_lines": 300, "error_pattern": "", "warn_pattern": ""}

    def collect(self):
        with mock.patch.object(ha, "run_merged", return_value=LOGS_OUT):
            return ha.collect_service_logs("lemon-meet_backend", self.CFG, 5)

    def test_conta_erros_e_avisos(self):
        res = self.collect()
        self.assertEqual(res["errors"], 2)    # ECONNREFUSED + Traceback
        self.assertEqual(res["warnings"], 1)  # deprecated
        self.assertEqual(res["scanned"], 6)   # linha vazia nao conta

    def test_failed_generico_nao_vira_erro(self):
        # "failed to find cache, building fresh" e log saudavel. Se contasse,
        # afogaria o sinal numa tela de 3 linhas.
        res = self.collect()
        textos = " ".join(m["m"] for m in res["recent"])
        self.assertNotIn("failed to find cache", textos)

    def test_remove_carimbo_e_prefixo_da_task(self):
        primeiro = self.collect()["recent"][0]
        self.assertNotIn("lemon-meet_backend.1.", primeiro["m"])
        self.assertNotIn("2026-08-13T", primeiro["m"])
        self.assertRegex(primeiro["t"], r"^\d{2}:\d{2}:\d{2}$")

    def test_mais_recentes_primeiro(self):
        recent = self.collect()["recent"]
        self.assertIn("Traceback", recent[0]["m"])
        self.assertIn("deprecated", recent[-1]["m"])

    def test_marca_o_horario_do_ultimo_erro(self):
        self.assertEqual(self.collect()["last_error_at"], "00:11:02")

    def test_le_o_stderr_do_container(self):
        # Regressao: usar run() em vez de run_merged() perderia as exceptions,
        # que saem no stderr da aplicacao.
        with mock.patch.object(ha, "run_merged", return_value=LOGS_OUT) as merged:
            ha.collect_service_logs("x", self.CFG, 5)
        merged.assert_called_once()
        self.assertIn("--timestamps", merged.call_args[0][0])

    def test_docker_sem_logs_nao_quebra(self):
        with mock.patch.object(ha, "run_merged", return_value=None):
            self.assertIsNone(ha.collect_service_logs("x", self.CFG, 5))

    def test_erro_do_cli_nao_vira_anomalia_da_aplicacao(self):
        # Com stderr mesclado, "Error: no such service" chegaria como se fosse
        # log da aplicacao e seria contado como erro dela. O codigo de saida
        # e o unico sinal confiavel.
        falha = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout="Error response from daemon: no such service: xyz\n")
        with mock.patch.object(subprocess, "run", return_value=falha):
            self.assertIsNone(ha.run_merged(["docker", "service", "logs"], 5))

    def test_saida_valida_com_codigo_zero_passa(self):
        okproc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="linha de log\n")
        with mock.patch.object(subprocess, "run", return_value=okproc):
            self.assertEqual(ha.run_merged(["docker"], 5), "linha de log\n")

    def test_regex_customizada(self):
        cfg = dict(self.CFG, error_pattern=r"(?i)\bdeprecated\b")
        with mock.patch.object(ha, "run_merged", return_value=LOGS_OUT):
            res = ha.collect_service_logs("x", cfg, 5)
        self.assertEqual(res["errors"], 1)

    def test_config_pode_desligar_deteccao_de_5xx(self):
        cfg = dict(self.CFG, detect_http_5xx=False)
        with mock.patch.object(ha, "run_merged", return_value=LOGS_OUT):
            base = ha.collect_service_logs("x", cfg, 5)
        self.assertEqual(base["errors"], 2)  # so os do regex textual


TASKS_OUT = "\n".join(json.dumps(t) for t in [
    {"CurrentState": "Running 3 hours ago", "Error": ""},
    {"CurrentState": "Shutdown 2 days ago", "Error": ""},
    {"CurrentState": "Failed 2 days ago", "Error": "task: non-zero exit (1)"},
])


# Linhas copiadas do log real do lemon-meet_backend rodando na VM.
REAL_LOGS = "\n".join([
    "2026-08-10T00:22:56.707Z b.1.x | [INFO] [CalendarCron] User 590a25d8 sem assinatura ativa",
    "2026-08-10T00:22:57.000Z b.1.x | GET /api/meetings?limit=9999 304 1247.434 ms - -",
    "2026-08-10T00:22:58.000Z b.1.x | GET /metrics 200 2.690 ms - -",
    "2026-08-10T00:22:59.000Z b.1.x | GET /health 200 0.615 ms - 77",
    "2026-08-10T00:23:00.000Z b.1.x | SIGTERM received, shutting down gracefully...",
    "2026-08-10T00:23:40.088Z b.1.x | Forced shutdown after timeout",
])


class TestPadroesDoLogReal(unittest.TestCase):
    """Os padrões precisam caber no formato que o backend realmente usa:
    access log sem prefixo de nível."""

    CFG = {"focus_log_lines": 300, "error_pattern": "", "warn_pattern": ""}

    def collect(self, text):
        with mock.patch.object(ha, "run_merged", return_value=text):
            return ha.collect_service_logs("lemon-meet_backend", self.CFG, 5)

    def test_forced_shutdown_e_erro(self):
        res = self.collect(REAL_LOGS)
        self.assertEqual(res["errors"], 1)
        self.assertIn("Forced shutdown", res["recent"][0]["m"])

    def test_sigterm_e_aviso(self):
        res = self.collect(REAL_LOGS)
        self.assertEqual(res["warnings"], 1)

    def test_status_2xx_3xx_nao_alarma(self):
        # 304 e 200 sao trafego normal; e o 1247.434 ms nao pode virar "5xx".
        res = self.collect(REAL_LOGS)
        textos = " ".join(m["m"] for m in res["recent"])
        self.assertNotIn("/api/meetings", textos)
        self.assertNotIn("/metrics", textos)

    def test_http_5xx_vira_erro(self):
        log = "2026-08-10T00:00:00Z b | POST /api/rooms 503 8.2 ms - -"
        self.assertEqual(self.collect(log)["errors"], 1)

    def test_duracao_parecida_com_5xx_nao_alarma(self):
        # "500" aqui e a duracao, nao o status: o status e 200.
        log = "2026-08-10T00:00:00Z b | GET /api/x 200 500 ms - -"
        self.assertEqual(self.collect(log)["errors"], 0)

    def test_numero_solto_nao_alarma(self):
        log = "2026-08-10T00:00:00Z b | processados 502 registros"
        self.assertEqual(self.collect(log)["errors"], 0)


class TestServiceTasks(unittest.TestCase):
    def test_conta_reinicios_e_guarda_o_erro(self):
        with mock.patch.object(ha, "run", return_value=TASKS_OUT):
            res = ha.collect_service_tasks("lemon-meet_backend", 5)
        self.assertEqual(res["running_tasks"], 1)
        self.assertEqual(res["failed_tasks"], 1)
        self.assertIn("non-zero exit", res["last_task_error"])


class TestFocusSummary(unittest.TestCase):
    def build(self, logs, tasks, replicas="1/1", ok=True):
        docker = {"mode": "swarm", "containers": [
            {"name": "lemon-meet_backend", "replicas": replicas, "ok": ok}]}
        with mock.patch.object(ha, "collect_service_logs", return_value=logs), \
             mock.patch.object(ha, "collect_service_tasks", return_value=tasks):
            return ha.collect_focus("lemon-meet_backend", {}, 5, docker)

    HEALTHY_LOGS = {"scanned": 100, "errors": 0, "warnings": 2,
                    "last_error_at": "", "recent": []}
    HEALTHY_TASKS = {"running_tasks": 1, "failed_tasks": 0,
                     "last_task_error": ""}

    def test_servico_de_pe_sem_erros_e_ok(self):
        res = self.build(self.HEALTHY_LOGS, self.HEALTHY_TASKS)
        self.assertEqual(res["status"], "ok")

    def test_erro_no_log_degrada_mesmo_com_replica_de_pe(self):
        logs = dict(self.HEALTHY_LOGS, errors=3)
        res = self.build(logs, self.HEALTHY_TASKS)
        self.assertEqual(res["status"], "degraded")

    def test_task_falha_degrada(self):
        tasks = dict(self.HEALTHY_TASKS, failed_tasks=2)
        res = self.build(self.HEALTHY_LOGS, tasks)
        self.assertEqual(res["status"], "degraded")

    def test_replica_fora_derruba(self):
        res = self.build(self.HEALTHY_LOGS, self.HEALTHY_TASKS,
                         replicas="0/1", ok=False)
        self.assertEqual(res["status"], "down")

    def test_resumo_cabe_na_tela_do_esp32(self):
        logs = {"scanned": 300, "errors": 5, "warnings": 9,
                "last_error_at": "00:11:02",
                "recent": [{"t": "00:11:02", "lvl": "err", "m": "x" * 200}] * 10}
        s = ha.summarize_focus(self.build(logs, self.HEALTHY_TASKS))
        self.assertEqual(len(s["msgs"]), 4)          # so as 4 mais recentes
        self.assertLessEqual(len(s["msgs"][0]["m"]), 64)
        size = len(json.dumps(s))
        self.assertLess(size, 700, f"payload grande demais: {size}B")


class TestStatus(unittest.TestCase):
    """Verifica a classificação ok / degraded / down."""

    def build(self, systemd_out, docker_out, cpu, mem_pct, disk_pct, load_pc):
        cfg = json.loads(json.dumps(ha.DEFAULTS))
        cfg["token"] = "x"
        collector = ha.Collector(cfg)
        collector.cpu_percent = lambda: cpu
        patches = [
            mock.patch.object(ha, "collect_memory",
                              return_value={"total_bytes": 1, "used_bytes": 1,
                                            "used_pct": mem_pct,
                                            "swap_total_bytes": 0,
                                            "swap_used_pct": 0.0}),
            mock.patch.object(ha, "collect_disks",
                              return_value=[{"mount": "/", "total_bytes": 1,
                                             "used_bytes": 1,
                                             "used_pct": disk_pct}]),
            mock.patch.object(ha, "collect_load",
                              return_value={"load1": 1.0, "load5": 1.0,
                                            "load15": 1.0, "cores": 4,
                                            "load_per_core": load_pc}),
            mock.patch.object(ha, "collect_uptime", return_value=1000),
            mock.patch.object(ha, "collect_systemd", return_value=systemd_out),
            mock.patch.object(ha, "collect_docker", return_value=docker_out),
        ]
        for p in patches:
            p.start()
        try:
            return collector.build()
        finally:
            for p in patches:
                p.stop()

    HEALTHY_SVC = {"total": 3, "bad": [], "services": []}
    HEALTHY_DKR = {"total": 2, "bad": [], "containers": []}

    def test_tudo_ok(self):
        res = self.build(self.HEALTHY_SVC, self.HEALTHY_DKR, 10.0, 30.0, 40.0, 0.3)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["ok"])

    def test_recurso_estourado_degrada(self):
        res = self.build(self.HEALTHY_SVC, self.HEALTHY_DKR, 95.0, 30.0, 40.0, 0.3)
        self.assertEqual(res["status"], "degraded")
        self.assertIn("cpu:95.0%", res["warnings"])

    def test_disco_cheio_degrada(self):
        res = self.build(self.HEALTHY_SVC, self.HEALTHY_DKR, 10.0, 30.0, 92.0, 0.3)
        self.assertEqual(res["status"], "degraded")
        self.assertIn("disk:/:92.0%", res["warnings"])

    def test_servico_caido_derruba(self):
        svc = {"total": 3, "bad": ["nginx.service"], "services": []}
        res = self.build(svc, self.HEALTHY_DKR, 10.0, 30.0, 40.0, 0.3)
        self.assertEqual(res["status"], "down")
        self.assertEqual(res["problems"], ["systemd:nginx.service"])

    def test_container_caido_derruba(self):
        dkr = {"total": 2, "bad": ["api"], "containers": []}
        res = self.build(self.HEALTHY_SVC, dkr, 10.0, 30.0, 40.0, 0.3)
        self.assertEqual(res["status"], "down")
        self.assertEqual(res["problems"], ["docker:api"])

    def test_down_tem_prioridade_sobre_degraded(self):
        dkr = {"total": 2, "bad": ["api"], "containers": []}
        res = self.build(self.HEALTHY_SVC, dkr, 99.0, 99.0, 99.0, 9.0)
        self.assertEqual(res["status"], "down")

    def test_coletores_ausentes_nao_quebram(self):
        res = self.build(None, None, None, 30.0, 40.0, 0.3)
        self.assertEqual(res["status"], "ok")


class TestStoppedPolicy(unittest.TestCase):
    """Serviços parados de propósito: quem decide a gravidade é o operador."""

    DOCKER = {"mode": "swarm", "total": 3, "bad": [],
              "stopped": ["gob_shipsafe"], "containers": []}

    def build(self, policy):
        cfg = json.loads(json.dumps(ha.DEFAULTS))
        cfg["stopped_services"] = policy
        collector = ha.Collector(cfg)
        collector.cpu_percent = lambda: 10.0
        patches = [
            mock.patch.object(ha, "collect_memory",
                              return_value={"used_pct": 30.0, "total_bytes": 1,
                                            "used_bytes": 1,
                                            "swap_total_bytes": 0,
                                            "swap_used_pct": 0.0}),
            mock.patch.object(ha, "collect_disks", return_value=[]),
            mock.patch.object(ha, "collect_load", return_value=None),
            mock.patch.object(ha, "collect_uptime", return_value=1),
            mock.patch.object(ha, "collect_systemd", return_value=None),
            mock.patch.object(ha, "collect_containers", return_value=self.DOCKER),
        ]
        for p in patches:
            p.start()
        try:
            return collector.build()
        finally:
            for p in patches:
                p.stop()

    def test_warning_degrada_sem_derrubar(self):
        res = self.build("warning")
        self.assertEqual(res["status"], "degraded")
        self.assertIn("parado:gob_shipsafe", res["warnings"])
        self.assertEqual(res["problems"], [])

    def test_failure_derruba(self):
        res = self.build("failure")
        self.assertEqual(res["status"], "down")
        self.assertIn("parado:gob_shipsafe", res["problems"])

    def test_ignore_nao_reporta(self):
        res = self.build("ignore")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["problems"], [])

    def test_resumo_nomeia_o_aviso_para_a_tela(self):
        # "DEGRADADO" sem dizer o porquê não ajuda quem olha o ESP32.
        s = ha.summarize(self.build("warning"))
        self.assertEqual(s["st"], "degraded")
        self.assertIn("parado:gob_shipsafe", s["bad"])
        self.assertEqual(s["nbad"], 1)


class TestSummary(unittest.TestCase):
    def test_payload_compacto_e_pequeno(self):
        full = {
            "status": "down", "ok": False, "ts": 1700000000,
            "host": "vm-prod-01", "uptime_s": 918273, "cpu_pct": 23.4,
            "memory": {"used_pct": 61.2}, "load": {"load_per_core": 0.8},
            "disks": [{"mount": "/", "used_pct": 44.0},
                      {"mount": "/var", "used_pct": 71.5}],
            "systemd": {"total": 12, "bad": ["nginx.service"],
                        "services": [{}] * 1},
            "docker": {"total": 8, "bad": ["api"], "containers": []},
            "problems": ["systemd:nginx.service", "docker:api"],
            "warnings": [],
        }
        s = ha.summarize(full)
        self.assertEqual(s["st"], "down")
        self.assertFalse(s["ok"])
        self.assertEqual(s["disk"], 71.5)  # pior disco
        self.assertEqual(s["dkr"], [8, 1])
        self.assertEqual(s["nbad"], 2)
        size = len(json.dumps(s))
        self.assertLess(size, 512, f"payload grande demais para o ESP32: {size}B")

    def test_trunca_lista_de_problemas(self):
        full = {"status": "down", "ok": False, "ts": 1, "host": "h",
                "problems": [f"docker:c{i}" for i in range(50)], "warnings": []}
        s = ha.summarize(full)
        self.assertEqual(len(s["bad"]), 8)
        self.assertEqual(s["nbad"], 50)

    def test_full_vazio_nao_quebra(self):
        s = ha.summarize({})
        self.assertEqual(s["st"], "unknown")
        self.assertFalse(s["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=False)
