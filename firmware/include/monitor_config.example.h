// Copie este arquivo para include/monitor_config.h e preencha com os seus dados.
//
//     cp include/monitor_config.example.h include/monitor_config.h
//
// monitor_config.h esta no .gitignore — suas senhas nao vao para o repositorio.
//
// Placa alvo: ESP32-2432S028 "Cheap Yellow Display".
#pragma once

// ---------------------------------------------------------------------------
// Wi-Fi
// ---------------------------------------------------------------------------
// ATENCAO: o ESP32 so fala Wi-Fi 2.4 GHz. Se o seu roteador tem uma unica rede
// "band steering" 5 GHz + 2.4 GHz, pode ser preciso separar as bandas.
#define WIFI_SSID       "NomeDaSuaRedeWiFi"
#define WIFI_PASSWORD   "senhaDoSeuWiFi"

// Nome que a placa registra na rede: http://esp32-monitor.local
#define DEVICE_HOSTNAME "esp32-monitor"

// ---------------------------------------------------------------------------
// VM
// ---------------------------------------------------------------------------
// URL do endpoint compacto. Use https:// em producao.
#define HEALTH_URL      "https://SEU-DOMINIO.com/health/summary"

// Token gerado pelo install.sh na VM (ele imprime no final da instalacao).
#define HEALTH_TOKEN    "cole-aqui-o-token-do-install.sh"

// De quanto em quanto tempo consultar a VM (ms). 15s e um bom equilibrio.
#define POLL_INTERVAL_MS        15000UL
// Intervalo mais curto usado enquanto a VM esta com problema.
#define POLL_INTERVAL_ALERT_MS  5000UL
// Timeout de cada requisicao HTTP (ms).
#define HTTP_TIMEOUT_MS         10000

// ---------------------------------------------------------------------------
// TLS
// ---------------------------------------------------------------------------
// 1 = valida o certificado do servidor contra include/ca_cert.h (recomendado).
// 0 = aceita qualquer certificado. So use em teste: sem validacao, o token
//     pode ser capturado por um intermediario.
#define VERIFY_TLS 1

// ---------------------------------------------------------------------------
// Display
// ---------------------------------------------------------------------------
// A pinagem do TFT fica no platformio.ini (flags do TFT_eSPI), nao aqui.
#define ENABLE_DISPLAY 1
// 1 = paisagem com os conectores a esquerda; 3 = paisagem invertida.
// Use 0 ou 2 se quiser a tela em pe (retrato).
#define DISPLAY_ROTATION 1
// Toque para forcar uma atualizacao imediata.
#define ENABLE_TOUCH 1

// ---------------------------------------------------------------------------
// LED RGB embutido da CYD
// ---------------------------------------------------------------------------
// Estes sao os pinos reais da placa — nao invente outros. O LED e de anodo
// comum: nivel BAIXO acende. Nao use GPIO 2 para nada: ele e o TFT_DC.
#define RGB_R_PIN       4
#define RGB_G_PIN       16
#define RGB_B_PIN       17
#define RGB_ACTIVE_HIGH 0

// Buzzer/alto-falante no conector "SPEAK" da placa. -1 desativa.
// A CYD tem um amplificador ligado ao GPIO 26; precisa de um alto-falante
// pequeno no conector. Deixe -1 se nao ligou nada.
#define BUZZER_PIN      -1
#define BUZZER_BEEPS    3

// ---------------------------------------------------------------------------
// Robustez
// ---------------------------------------------------------------------------
// Apos quantas falhas seguidas de rede a placa se reinicia sozinha.
#define MAX_FAILURES_BEFORE_REBOOT 20
// Servidor web embutido (http://esp32-monitor.local). 0 desativa.
#define ENABLE_WEB_UI 1
// Fuso horario para os horarios exibidos. -3 = horario de Brasilia.
#define TZ_OFFSET_HOURS -3
