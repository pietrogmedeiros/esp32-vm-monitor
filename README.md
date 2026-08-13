# ESP32 VM Monitor — Cheap Yellow Display

Um painel de mesa que mostra a saúde da sua VM numa tela de 2.8". Ele consulta
um agente instalado na VM a cada 15 segundos e traduz o resultado em tela, LED
RGB e uma página web local.

```
   VM (Linux)                      internet                ESP32-2432S028
┌────────────────┐                                       ┌──────────────────┐
│ health_agent   │  systemd + docker + CPU/RAM/disco      │  TFT 2.8" 320x240│
│ :9099 (local)  │ ───┐                                   │  LED RGB + toque │
└────────────────┘    │                                   │  http://...local │
┌────────────────┐    │  HTTPS + Bearer token             └──────────────────┘
│ nginx :443     │ ◄──┘  GET /health/summary  ◄──────────────────┘
└────────────────┘
```

## A placa

**ESP32-2432S028**, conhecida como *Cheap Yellow Display* (CYD).

| Componente | Detalhe |
|---|---|
| MCU | ESP32-WROOM-32 (por isso o alvo é `esp32dev`) |
| Tela | TFT 2.8" ILI9341, 320×240 |
| Toque | XPT2046, num barramento SPI separado do display |
| USB-serial | CH340C — o macOS 13+ tem driver nativo, não precisa instalar nada |
| LED RGB | GPIO 4 / 16 / 17, **ânodo comum: nível BAIXO acende** |
| Portas USB | uma USB-C e uma micro-USB |

### ⚠️ Grave pela porta micro-USB

A USB-C dessa placa **não tem os resistores de 5,1 kΩ nos pinos CC**. Sem eles,
uma porta USB-C (como as do MacBook) nunca libera os 5 V, e a placa fica
completamente morta — sem LED, sem tela, sem enumerar no USB.

Use a **micro-USB** com um cabo de dados, ou a USB-C através de um adaptador
USB-A (que entrega 5 V sem negociação). Cabo de carregador raramente serve:
muitos não têm os fios de dados.

Para diagnosticar a conexão a qualquer momento:

```bash
./scripts/detectar-esp32.sh
```

### Pinos ocupados

Não use estes GPIOs para mais nada — a tela e o toque dependem deles:

| Função | GPIO |
|---|---|
| TFT MISO / MOSI / SCLK / CS / DC / BL | 12 / 13 / 14 / 15 / **2** / 21 |
| Toque CLK / CS / MOSI / MISO / IRQ | 25 / 33 / 32 / 39 / 36 |
| LED RGB R / G / B | 4 / 16 / 17 |
| Cartão SD CS / MOSI / MISO / SCK | 5 / 23 / 19 / 18 |
| Alto-falante | 26 |

GPIO 2 é o `TFT_DC`. Usá-lo como LED de status — o padrão em DevKits comuns —
embaralha o display.

---

## O que aparece na tela

```
┌────────────────────────────────────────┐
│ vm-prod-01                             │  ← barra colorida pelo estado
│ FORA DO AR                     ha 3s   │
├────────────────────────────────────────┤
│ CPU        MEMORIA      DISCO          │
│ 23%        61%          44%            │
│ ▓▓▓░░░░░   ▓▓▓▓▓░░░     ▓▓▓░░░░░       │  ← barra verde/âmbar/vermelha
├────────────────────────────────────────┤
│ Servicos 1/42      Containers 1/8      │
├────────────────────────────────────────┤
│ PROBLEMAS                              │
│ systemd:nginx.service                  │
│ docker:api                             │
├────────────────────────────────────────┤
│ Wi-Fi -51dBm  up 10d15h  #37           │
└────────────────────────────────────────┘
```

**Toque a tela** para forçar uma consulta imediata, sem esperar o intervalo.

O painel só repinta as seções cujo conteúdo mudou, então a tela não pisca a
cada consulta.

| Estado | Significado | LED RGB |
|---|---|---|
| **OK** | tudo de pé, recursos abaixo do limite | verde, 1 pisco a cada 3 s |
| **DEGRADADO** | CPU/RAM/disco/load estourados, nada caiu | amarelo, 2 piscos |
| **FORA DO AR** | serviço systemd ou container fora | vermelho, contínuo |
| **TOKEN INVALIDO** | a VM devolveu 401/403 | magenta, 3 piscos |
| **SEM RESPOSTA** | DNS/TLS/timeout | magenta, pisco longo |
| **SEM WI-FI** | não conectou na rede | azul, tremido rápido |

---

## Parte 1 — Instalar o agente na VM

Copie a pasta `vm-agent/` para a VM e rode o instalador:

```bash
scp -r vm-agent/ usuario@sua-vm:/tmp/
ssh usuario@sua-vm
sudo bash /tmp/vm-agent/install.sh
```

O instalador cria o usuário `healthagent`, instala o serviço systemd, gera um
**token aleatório** e o imprime no final. **Guarde esse token.**

O agente escuta só em `127.0.0.1:9099` — ele não fica exposto sozinho.

### Escolher o que monitorar

Edite `/etc/vm-health-agent/config.json`:

```jsonc
{
  // Vazio = reporta qualquer unit em estado 'failed'.
  // Preenchido = essas units viram a lista crítica e precisam estar ativas.
  "watch_services": ["docker.service", "nginx.service", "postgresql.service"],

  // Vazio = observa todos os containers existentes.
  // Preenchido = esses containers precisam existir E estar rodando.
  "watch_containers": ["api", "worker"],

  "watch_disks": ["/", "/var/lib/docker"],

  "thresholds": {
    "cpu_pct": 90.0, "mem_pct": 90.0, "disk_pct": 85.0, "load_per_core": 2.0
  }
}
```

Depois: `sudo systemctl restart vm-health-agent`

### Publicar com HTTPS

```bash
sudo cp vm-agent/nginx.conf.example /etc/nginx/sites-available/health
sudo nano /etc/nginx/sites-available/health          # troque SEU-DOMINIO.com
sudo ln -s /etc/nginx/sites-available/health /etc/nginx/sites-enabled/
sudo certbot --nginx -d SEU-DOMINIO.com
sudo nginx -t && sudo systemctl reload nginx
```

Leia o cabeçalho do arquivo: a diretiva `limit_req_zone` precisa ir no bloco
`http{}`, não no `server{}`.

Teste de fora:

```bash
curl -H "Authorization: Bearer SEU_TOKEN" https://SEU-DOMINIO.com/health/summary
```

---

## Parte 2 — Gravar o firmware

O PlatformIO já está instalado (`brew install platformio`).

```bash
cd firmware
cp include/monitor_config.example.h include/monitor_config.h
nano include/monitor_config.h
```

Preencha:

```c
#define WIFI_SSID     "SuaRede"        // 2.4 GHz — o ESP32 não fala 5 GHz
#define WIFI_PASSWORD "suaSenha"
#define HEALTH_URL    "https://SEU-DOMINIO.com/health/summary"
#define HEALTH_TOKEN  "token-que-o-install.sh-imprimiu"
```

Grave:

```bash
pio run -t upload --upload-port /dev/cu.usbserial-110
```

Para ver o log serial, use um terminal de verdade (o `pio device monitor`
precisa de TTY):

```bash
pio device monitor -p /dev/cu.usbserial-110 -b 115200
```

Se o certificado da VM **não** for Let's Encrypt:

```bash
./scripts/gerar-ca.sh SEU-DOMINIO.com    # regenera firmware/include/ca_cert.h
cd firmware && pio run -t upload
```

### Testar sem a VM pronta

Dá para validar a cadeia inteira apontando o ESP32 para o agente rodando no
próprio Mac, na mesma rede:

```bash
cd vm-agent && HEALTH_TOKEN=teste python3 health_agent.py
ipconfig getifaddr en0        # descubra o IP do Mac
```

E no `monitor_config.h`:

```c
#define HEALTH_URL  "http://192.168.0.10:9099/health/summary"   // http, não https
#define HEALTH_TOKEN "teste"
#define VERIFY_TLS   0
```

---

## Se a tela não acender ou sair errada

| Sintoma | Causa provável |
|---|---|
| Tela preta, LED aceso | backlight — confira `-DTFT_BL=21` no `platformio.ini` |
| Cores invertidas (negativo) | troque `-DILI9341_2_DRIVER=1` por `-DILI9341_DRIVER=1` |
| Imagem espelhada / de cabeça | mude `DISPLAY_ROTATION` para 3 |
| Toque não responde | algumas CYD vêm sem o controlador; use `ENABLE_TOUCH 0` |
| Texto embaralhado | algo está usando GPIO 2 (é o `TFT_DC`) |

O aviso `addApbChangeCallback(): duplicate func` no boot é normal: os dois
barramentos SPI (tela e toque) registram o mesmo callback de mudança de clock.
Não afeta o funcionamento.

---

## Estrutura

```
esp32/
├── firmware/                       PlatformIO, env "cyd"
│   ├── platformio.ini              pinagem do TFT vai aqui, via build_flags
│   ├── include/
│   │   ├── monitor_config.example.h  modelo — copie para monitor_config.h
│   │   ├── monitor_state.h           tipos compartilhados
│   │   ├── display.h
│   │   └── ca_cert.h                 ISRG Root X1 (Let's Encrypt)
│   └── src/
│       ├── main.cpp                Wi-Fi, HTTP, LED, web server
│       └── display.cpp             painel na tela
├── vm-agent/                       Python 3 stdlib, zero dependências
│   ├── health_agent.py
│   ├── install.sh                  instalador para a VM
│   ├── vm-health-agent.service     unit systemd com hardening
│   ├── nginx.conf.example          TLS + rate limit
│   ├── config.example.json
│   ├── test_health_agent.py        21 testes dos parsers
│   └── test_contract.py            6 testes agente ↔ firmware
└── scripts/
    ├── detectar-esp32.sh           diagnostica a porta serial no macOS
    └── gerar-ca.sh                 extrai o CA real do seu servidor
```

## Rodar os testes

```bash
cd vm-agent && python3 test_health_agent.py && python3 test_contract.py
cd ../firmware && pio run
```

Os testes de contrato leem `firmware/src/main.cpp` e conferem que toda chave
JSON que o firmware lê é realmente enviada pelo agente — renomear um campo de
um lado quebra o teste em vez de virar um campo vazio silencioso na tela.

## Segurança

- O agente escuta só em `127.0.0.1`; quem fala com a internet é o nginx.
- Autenticação por Bearer token comparado com `hmac.compare_digest`.
- O firmware **valida** o certificado TLS contra `ca_cert.h` (`VERIFY_TLS 1`).
  Com `VERIFY_TLS 0` o token trafega sem garantia de que o servidor é o seu.
- O serviço roda como usuário dedicado com `ProtectSystem=strict`,
  `CapabilityBoundingSet=` vazio e syscalls filtradas.
- `monitor_config.h` e `vm-agent/config.json` estão no `.gitignore` — senha do
  Wi-Fi e token não vazam para o repositório.

O agente precisa pertencer ao grupo `docker` para ler o estado dos containers,
e pertencer a esse grupo equivale a root na prática. Por isso ele não fica
exposto diretamente e só faz leitura.
