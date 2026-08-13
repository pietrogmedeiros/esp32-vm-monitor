#!/usr/bin/env bash
#
# Descobre em qual porta serial o ESP32 apareceu no macOS e diagnostica
# o motivo quando ele nao aparece.
#
#   ./scripts/detectar-esp32.sh
#
set -uo pipefail

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$*"; }
info() { printf "    %s\n" "$*"; }

bold "==> Portas seriais USB encontradas"

# O ESP32 aparece como usbserial (CP2102/CH340) ou usbmodem (USB nativo).
# Nada de mapfile aqui: o macOS ainda vem com bash 3.2.
PORTS=""
FIRST_PORT=""
for p in /dev/cu.usbserial-* /dev/cu.usbmodem* /dev/cu.SLAB_USBtoUART* \
         /dev/cu.wchusbserial*; do
  [ -e "$p" ] || continue
  PORTS="$PORTS $p"
  [ -n "$FIRST_PORT" ] || FIRST_PORT="$p"
done

if [ -n "$FIRST_PORT" ]; then
  for p in $PORTS; do ok "$p"; done
  echo
  bold "==> Use esta porta"
  info "pio run -t upload --upload-port $FIRST_PORT"
  info "pio device monitor -p $FIRST_PORT -b 115200"
  exit 0
fi

bad "nenhuma porta serial de ESP32 encontrada"
echo

bold "==> Dispositivos USB conectados"
CHIP_FOUND=0
while IFS= read -r line; do
  info "$line"
  if grep -qiE "cp210|ch34|ch910|ftdi|espressif|silicon labs|qinheng|usb-serial|usb2.0-serial" <<<"$line"; then
    CHIP_FOUND=1
  fi
done < <(system_profiler SPUSBDataType 2>/dev/null | grep -E "^\s{6,}[A-Za-z0-9].*:$" | sed 's/://g' | sed 's/^ *//' | sort -u)

if [[ -z "$(system_profiler SPUSBDataType 2>/dev/null)" ]]; then
  info "(system_profiler nao retornou nada — rode este script direto no Terminal)"
  echo
  bold "==> Fallback via ioreg"
  ioreg -p IOUSB -w0 -l 2>/dev/null \
    | grep -E '"(USB Product Name|USB Vendor Name|idVendor|idProduct)"' \
    | sed 's/^ *//' | while IFS= read -r l; do info "$l"; done
fi

echo
bold "==> Diagnostico"

if [[ $CHIP_FOUND -eq 1 ]]; then
  bad "o chip USB-serial aparece, mas nenhuma porta foi criada"
  info "Isso normalmente e driver. Instale o driver do chip:"
  info "  CH340/CH341 : https://www.wch-ic.com/downloads/CH34XSER_MAC_ZIP.html"
  info "  CP210x      : https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers"
  info "Depois reinicie o Mac e aprove a extensao em"
  info "Ajustes > Privacidade e Seguranca > Seguranca."
else
  bad "o Mac nao ve NENHUM chip USB-serial"
  echo
  info "Em ordem de probabilidade:"
  info ""
  info "1. CABO SO DE CARGA (causa mais comum, ~70% dos casos)"
  info "   Muitos cabos USB nao tem os fios de dados. Troque por um cabo"
  info "   que voce sabe que transfere dados (ex: o de um celular/HD externo)."
  info ""
  info "2. HUB USB / DOCK no meio do caminho"
  info "   Ligue o ESP32 DIRETO numa porta do Mac, sem hub e sem adaptador."
  info ""
  info "3. Placa sem alimentacao"
  info "   O LED vermelho de power do ESP32 esta aceso? Se nao, e cabo ou placa."
  info ""
  info "4. Porta USB-C: use um adaptador USB-C->USB-A de dados, nao so de carga."
fi

echo
bold "==> Depois de corrigir, rode este script de novo"
exit 1
