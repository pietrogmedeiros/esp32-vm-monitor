#!/usr/bin/env bash
#
# Regenera firmware/include/ca_cert.h a partir do certificado REAL do seu
# servidor. Use quando o certificado da VM nao for Let's Encrypt (Cloudflare,
# ZeroSSL, CA interna, autoassinado...).
#
#   ./scripts/gerar-ca.sh seu-dominio.com
#   ./scripts/gerar-ca.sh seu-dominio.com 8443
#
set -euo pipefail

HOST="${1:-}"
PORT="${2:-443}"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/firmware/include/ca_cert.h"

if [ -z "$HOST" ]; then
  echo "uso: $0 <dominio> [porta]" >&2
  exit 1
fi

command -v openssl >/dev/null || { echo "openssl nao encontrado" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> baixando a cadeia de certificados de $HOST:$PORT"
openssl s_client -showcerts -servername "$HOST" -connect "$HOST:$PORT" \
  </dev/null 2>/dev/null > "$TMP/chain.txt" || {
    echo "erro: nao consegui conectar em $HOST:$PORT" >&2
    exit 1
  }

# O ultimo certificado da cadeia e o mais proximo da raiz — e o que o
# ESP32 precisa como ancora de confianca. (O awk do BSD nao aceita
# 'print > expr', entao o split vai em Python.)
LAST="$TMP/anchor.pem"
python3 - "$TMP/chain.txt" "$LAST" <<'PY'
import re, sys
text = open(sys.argv[1], errors="replace").read()
certs = re.findall(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)
if not certs:
    sys.exit("erro: nenhum certificado na resposta (o servidor usa TLS mesmo?)")
open(sys.argv[2], "w").write(certs[-1] + "\n")
print(f"    ({len(certs)} certificado(s) na cadeia, usando o ultimo)")
PY

if [ ! -s "$LAST" ]; then
  exit 1
fi

echo "==> certificado escolhido:"
openssl x509 -in "$LAST" -noout -subject -issuer -dates | sed 's/^/    /'

FINGERPRINT="$(openssl x509 -in "$LAST" -noout -fingerprint -sha256 | cut -d= -f2)"

python3 - "$LAST" "$OUT" "$HOST" "$FINGERPRINT" <<'PY'
import sys
pem_path, out_path, host, fingerprint = sys.argv[1:5]
lines = open(pem_path).read().strip().splitlines()
body = "\n".join('  "%s\\n"' % l for l in lines)
open(out_path, "w").write(f"""// Gerado automaticamente por scripts/gerar-ca.sh — NAO EDITE A MAO.
//
// Ancora de confianca extraida de {host}.
// SHA-256: {fingerprint}
#pragma once

static const char HEALTH_CA_CERT[] PROGMEM =
{body};
""")
print(f"==> escrito em {out_path}")
PY

echo "==> agora recompile:  cd firmware && pio run -t upload"
