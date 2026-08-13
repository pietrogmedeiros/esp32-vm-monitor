#!/usr/bin/env bash
#
# Instala o vm-health-agent na VM. Execute como root NA VM (nao no Mac):
#
#   sudo bash install.sh
#
set -euo pipefail

APP_DIR=/opt/vm-health-agent
CFG_DIR=/etc/vm-health-agent
CFG_FILE="$CFG_DIR/config.json"
SERVICE=vm-health-agent.service
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "erro: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "rode como root (sudo bash install.sh)"
command -v python3 >/dev/null || die "python3 nao encontrado"
command -v systemctl >/dev/null || die "systemd nao encontrado"

echo "==> criando usuario de servico 'healthagent'"
if ! id -u healthagent >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin healthagent
fi

if getent group docker >/dev/null 2>&1; then
  usermod -aG docker healthagent
  echo "    adicionado ao grupo docker"
else
  echo "    grupo docker inexistente — monitoramento de containers ficara inativo"
  # Sem o grupo docker o systemd falha ao resolver SupplementaryGroups.
  sed -i '/^SupplementaryGroups=docker$/d' "$SRC_DIR/$SERVICE" 2>/dev/null || true
fi

echo "==> instalando arquivos em $APP_DIR"
install -d -m 0755 "$APP_DIR" "$CFG_DIR"
install -m 0755 "$SRC_DIR/health_agent.py" "$APP_DIR/health_agent.py"

if [[ -f "$CFG_FILE" ]]; then
  echo "==> $CFG_FILE ja existe, preservando (token mantido)"
  TOKEN="$(python3 -c "import json;print(json.load(open('$CFG_FILE')).get('token',''))")"
else
  echo "==> gerando config e token novo"
  TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  python3 - "$SRC_DIR/config.example.json" "$CFG_FILE" "$TOKEN" <<'PY'
import json, sys
src, dst, token = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = json.load(open(src))
cfg["token"] = token
cfg["bind"] = "127.0.0.1"
json.dump(cfg, open(dst, "w"), indent=2, ensure_ascii=False)
PY
fi
chown -R root:healthagent "$CFG_DIR"
chmod 0750 "$CFG_DIR"
chmod 0640 "$CFG_FILE"

echo "==> instalando unit systemd"
install -m 0644 "$SRC_DIR/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable --now "$SERVICE"

sleep 2
systemctl is-active --quiet "$SERVICE" || {
  journalctl -u "$SERVICE" -n 30 --no-pager
  die "o servico nao subiu — veja o log acima"
}

PORT="$(python3 -c "import json;print(json.load(open('$CFG_FILE'))['port'])")"
echo
echo "==> teste local:"
curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health/summary" || true
echo
echo
echo "======================================================================"
echo " Agente ativo. Guarde este token — ele vai no config.h do ESP32:"
echo
echo "   $TOKEN"
echo
echo " O agente escuta apenas em 127.0.0.1:$PORT."
echo " Publique-o com TLS usando o nginx.conf.example deste diretorio."
echo "======================================================================"
