#!/usr/bin/env python3
"""Contrato entre o agente da VM e o firmware do ESP32.

O firmware le o JSON de /health/summary por chaves literais. Se alguem
renomear uma chave no agente, nada quebra em tempo de compilacao — o ESP32
so passa a mostrar campos vazios. Este teste liga os dois lados.

    python3 test_contract.py
"""
import json
import os
import re
import unittest

import health_agent as ha

_FIRMWARE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "firmware"
)
FIRMWARE_MAIN = os.path.join(_FIRMWARE, "src", "main.cpp")
# As structs Health/MonitorState vivem no header compartilhado, nao no main.
FIRMWARE_STATE = os.path.join(_FIRMWARE, "include", "monitor_state.h")

# Chaves do payload lidas pelo firmware, ex.: doc["cpu"], doc["svc"][0]
DOC_KEY_RE = re.compile(r'doc\["([a-z_]+)"\]')

# Campos que o agente publica para outros consumidores (curl, uptime checks,
# dashboards) e que o firmware deliberadamente nao le:
#   ts — carimbo de tempo, informativo
#   ok — booleano redundante com 'st', que o firmware ja interpreta
INFORMATIONAL_KEYS = {"ts", "ok"}


def read_firmware():
    with open(FIRMWARE_MAIN, encoding="utf-8") as fh:
        return fh.read()


def read_state_header():
    with open(FIRMWARE_STATE, encoding="utf-8") as fh:
        return fh.read()


def summary_keys():
    full = ha.Collector(json.loads(json.dumps(ha.DEFAULTS))).build()
    return set(ha.summarize(full).keys())


def firmware_read_keys():
    """Chaves que o firmware LE do payload da VM (funcao fetchHealth)."""
    source = read_firmware()
    start = source.index("static Status fetchHealth()")
    end = source.index("// ---", start + 100)
    return set(DOC_KEY_RE.findall(source[start:end]))


class TestContract(unittest.TestCase):
    def test_firmware_so_le_chaves_que_o_agente_envia(self):
        missing = firmware_read_keys() - summary_keys()
        self.assertEqual(
            missing, set(),
            f"o firmware le chaves que o agente nao envia: {sorted(missing)}",
        )

    def test_chaves_uteis_do_agente_sao_consumidas(self):
        # Tirando os campos informativos, tudo que o agente envia
        # precisa ser consumido pelo firmware.
        unused = summary_keys() - firmware_read_keys() - INFORMATIONAL_KEYS
        self.assertEqual(
            unused, set(),
            f"o agente envia campos que o firmware ignora: {sorted(unused)}",
        )

    def test_status_bate_com_as_strings_do_firmware(self):
        source = read_firmware()
        for status in ("ok", "degraded", "down"):
            self.assertIn(
                f'strcmp(st, "{status}")', source,
                f"o firmware nao trata o status '{status}' que o agente emite",
            )

    def test_endpoint_do_firmware_bate_com_o_do_agente(self):
        source = read_firmware()
        example = os.path.join(
            os.path.dirname(FIRMWARE_MAIN), "..", "include",
            "monitor_config.example.h",
        )
        with open(example, encoding="utf-8") as fh:
            cfg = fh.read()
        self.assertIn("/health/summary", cfg,
                      "HEALTH_URL de exemplo deve apontar para /health/summary")
        self.assertIn("Bearer", source,
                      "o firmware deve mandar o token como Bearer")

    def test_bad_cabe_no_buffer_do_firmware(self):
        self.assertIn("String bad[8]", read_state_header())
        # O agente trunca em 8; o firmware reserva 8. Os dois devem casar.
        full = {"status": "down", "ok": False, "ts": 1, "host": "h",
                "problems": [f"docker:c{i}" for i in range(30)], "warnings": []}
        self.assertEqual(len(ha.summarize(full)["bad"]), 8)

    def test_firmware_nao_estoura_o_campo_host(self):
        match = re.search(r"char host\[(\d+)\]", read_state_header())
        self.assertIsNotNone(match, "campo host nao encontrado no firmware")
        firmware_cap = int(match.group(1)) - 1  # -1 pelo terminador nulo
        # O agente corta o hostname em 24 chars.
        full = {"status": "ok", "ok": True, "ts": 1, "host": "x" * 100,
                "problems": [], "warnings": []}
        self.assertLessEqual(len(ha.summarize(full)["host"]), firmware_cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
