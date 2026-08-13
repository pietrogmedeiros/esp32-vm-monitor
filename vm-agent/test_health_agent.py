#!/usr/bin/env python3
"""Testes dos parsers do health_agent (rodam em qualquer SO).

    python3 test_health_agent.py
"""
import json
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
        # 0/0 = desligado de proposito, nao quebrado.
        entry = [c for c in self.collect([])["containers"]
                 if c["name"] == "gob_desligado"][0]
        self.assertTrue(entry["ok"])

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
