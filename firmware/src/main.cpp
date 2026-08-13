// ESP32 VM Monitor — placa ESP32-2432S028 "Cheap Yellow Display"
//
// Consulta periodicamente o /health/summary do agente instalado na VM e
// mostra o resultado na tela de 2.8", no LED RGB embutido e numa pagina web.
//
// Estados possiveis, do pior para o melhor:
//   SEM WI-FI       nao conectou na rede
//   SEM RESPOSTA    nao consegui falar com a VM (DNS/TLS/timeout)
//   TOKEN INVALIDO  a VM devolveu 401/403
//   FORA DO AR      algum servico systemd ou container esta fora
//   DEGRADADO       recurso estourado, mas nada caiu
//   OK              tudo de pe

#include <Arduino.h>

// Precisa vir antes de qualquer #if que use as suas macros.
//
// O nome e proposital: um "config.h" generico colide com o config.h do mbedtls
// que vem no framework ESP32, e o __has_include casaria com o arquivo errado.
#if __has_include("monitor_config.h")
#include "monitor_config.h"
#else
#error "Copie include/monitor_config.example.h para include/monitor_config.h e preencha seus dados."
#endif

// Rede de seguranca caso o arquivo exista mas esteja incompleto.
#if !defined(WIFI_SSID) || !defined(HEALTH_URL) || !defined(HEALTH_TOKEN) ||   \
    !defined(RGB_R_PIN) || !defined(VERIFY_TLS) || !defined(ENABLE_WEB_UI) || \
    !defined(ENABLE_DISPLAY)
#error "monitor_config.h esta incompleto — compare com monitor_config.example.h."
#endif

#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>

#include "display.h"
#include "monitor_state.h"

#if ENABLE_WEB_UI
#include <WebServer.h>
#endif

#if VERIFY_TLS
#include "ca_cert.h"
#endif

// ---------------------------------------------------------------------------
// Estado global
// ---------------------------------------------------------------------------

static MonitorState g_state;
static Status g_prevStatus = ST_BOOT;
static uint32_t g_lastOkFetchMs = 0;
static uint32_t g_lastAttemptMs = 0;
static bool g_timeSynced = false;

// ---------------------------------------------------------------------------
// LED RGB embutido — maquina de estados nao bloqueante
// ---------------------------------------------------------------------------
//
// Cada padrao e uma sequencia de passos de `stepMs`. O bit i de `mask`
// (do menos significativo para o mais) diz se o LED fica aceso no passo i.

struct Pattern {
  uint32_t mask;
  uint8_t len;
  uint16_t stepMs;
};

// OK        : um pisco curto a cada 3 s (batimento discreto)
// DEGRADADO : dois piscos rapidos, depois pausa
// FORA DO AR: pisca forte e continuo
// TOKEN     : tres piscos, pausa longa
// SEM RESP. : pisco longo alternado
// SEM WI-FI : tremido rapido
static Pattern patternFor(Status s) {
  switch (s) {
    case ST_OK:        return {0b1u, 30, 100};
    case ST_DEGRADED:  return {0b101u, 16, 100};
    case ST_DOWN:      return {0b10u, 2, 200};
    case ST_AUTH_ERR:  return {0b10101u, 24, 120};
    case ST_FETCH_ERR: return {0b1100u, 8, 250};
    case ST_WIFI_DOWN: return {0b10u, 2, 80};
    default:           return {0b1010u, 4, 500};
  }
}

static uint8_t g_ledStep = 0;
static uint32_t g_ledLastMs = 0;

// O LED RGB da CYD e de anodo comum: nivel BAIXO acende.
static inline void rgbPin(int pin, bool on) {
#if RGB_ACTIVE_HIGH
  digitalWrite(pin, on ? HIGH : LOW);
#else
  digitalWrite(pin, on ? LOW : HIGH);
#endif
}

static void rgbSet(bool r, bool g, bool b) {
  rgbPin(RGB_R_PIN, r);
  rgbPin(RGB_G_PIN, g);
  rgbPin(RGB_B_PIN, b);
}

static void rgbForStatus(Status s, bool on) {
  if (!on) {
    rgbSet(false, false, false);
    return;
  }
  switch (s) {
    case ST_OK:        rgbSet(false, true, false); break;   // verde
    case ST_DEGRADED:  rgbSet(true, true, false); break;    // amarelo
    case ST_DOWN:      rgbSet(true, false, false); break;   // vermelho
    case ST_AUTH_ERR:  rgbSet(true, false, true); break;    // magenta
    case ST_FETCH_ERR: rgbSet(true, false, true); break;    // magenta
    case ST_WIFI_DOWN: rgbSet(false, false, true); break;   // azul
    default:           rgbSet(true, true, true); break;     // branco
  }
}

static void ledTask() {
  Pattern p = patternFor(g_state.status);
  uint32_t now = millis();
  if (now - g_ledLastMs < p.stepMs) return;
  g_ledLastMs = now;
  g_ledStep = (g_ledStep + 1) % p.len;
  rgbForStatus(g_state.status, (p.mask >> g_ledStep) & 1u);
}

// ---------------------------------------------------------------------------
// Buzzer opcional — tambem nao bloqueante
// ---------------------------------------------------------------------------

static int g_beepsLeft = 0;
static bool g_beepOn = false;
static uint32_t g_beepNextMs = 0;

static void beep(int times) {
#if BUZZER_PIN >= 0
  g_beepsLeft = times * 2;  // cada beep = liga + desliga
  g_beepNextMs = millis();
#else
  (void)times;
#endif
}

static void beepTask() {
#if BUZZER_PIN >= 0
  if (g_beepsLeft <= 0) return;
  uint32_t now = millis();
  if (now < g_beepNextMs) return;
  g_beepOn = !g_beepOn;
  digitalWrite(BUZZER_PIN, g_beepOn ? HIGH : LOW);
  g_beepNextMs = now + (g_beepOn ? 120 : 180);
  g_beepsLeft--;
  if (g_beepsLeft == 0) digitalWrite(BUZZER_PIN, LOW);
#endif
}

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------

static String humanUptime(uint32_t seconds) {
  if (seconds == 0) return "-";
  uint32_t d = seconds / 86400;
  uint32_t h = (seconds % 86400) / 3600;
  uint32_t m = (seconds % 3600) / 60;
  char buf[32];
  if (d > 0) snprintf(buf, sizeof(buf), "%lud %luh", (unsigned long)d, (unsigned long)h);
  else if (h > 0) snprintf(buf, sizeof(buf), "%luh %lum", (unsigned long)h, (unsigned long)m);
  else snprintf(buf, sizeof(buf), "%lum", (unsigned long)m);
  return String(buf);
}

static String pct(float v) {
  if (isnan(v)) return "-";
  char buf[16];
  snprintf(buf, sizeof(buf), "%.1f%%", v);
  return String(buf);
}

static uint32_t staleSeconds() {
  if (g_lastOkFetchMs == 0) return 0;
  return (millis() - g_lastOkFetchMs) / 1000;
}

// Mantem os campos derivados do MonitorState em dia antes de desenhar.
static void refreshDerived() {
  g_state.staleS = staleSeconds();
  g_state.wifiUp = WiFi.status() == WL_CONNECTED;
  g_state.rssi = g_state.wifiUp ? WiFi.RSSI() : 0;
}

// ---------------------------------------------------------------------------
// Wi-Fi + NTP
// ---------------------------------------------------------------------------

static void connectWifi() {
  Serial.printf("[wifi] conectando em \"%s\"", WIFI_SSID);
  displayBootMessage("Conectando ao Wi-Fi...");
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // latencia estavel; consumo e irrelevante no USB
  WiFi.setAutoReconnect(true);
  WiFi.setHostname(DEVICE_HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
    ledTask();
    delay(250);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] conectado — IP %s, RSSI %d dBm\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
    displayBootMessage(WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[wifi] FALHOU. Confira SSID/senha e se a rede e 2.4 GHz.");
    displayBootMessage("Wi-Fi falhou — confira SSID/senha (2.4 GHz)");
  }
}

// O TLS so valida certificados se o relogio estiver certo.
static void syncTime() {
  configTime(TZ_OFFSET_HOURS * 3600, 0, "pool.ntp.org", "time.google.com");
  Serial.print("[ntp] sincronizando relogio");
  uint32_t start = millis();
  while (time(nullptr) < 1700000000 && millis() - start < 20000) {
    ledTask();
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  g_timeSynced = time(nullptr) >= 1700000000;
  if (g_timeSynced) {
    time_t now = time(nullptr);
    Serial.printf("[ntp] relogio: %s", ctime(&now));
  } else {
    Serial.println("[ntp] FALHOU — a validacao de TLS vai falhar sem hora certa.");
  }
}

// ---------------------------------------------------------------------------
// Consulta ao agente da VM
// ---------------------------------------------------------------------------

static Status fetchHealth() {
  if (WiFi.status() != WL_CONNECTED) {
    g_state.lastError = "Wi-Fi desconectado";
    return ST_WIFI_DOWN;
  }

  const bool useTls = strncmp(HEALTH_URL, "https://", 8) == 0;
  WiFiClientSecure secureClient;
  WiFiClient plainClient;
  HTTPClient http;

  if (useTls) {
#if VERIFY_TLS
    if (!g_timeSynced) {
      g_state.lastError = "relogio nao sincronizado (TLS nao valida)";
      return ST_FETCH_ERR;
    }
    secureClient.setCACert(HEALTH_CA_CERT);
#else
    secureClient.setInsecure();
#endif
    secureClient.setTimeout(HTTP_TIMEOUT_MS / 1000);
  }

  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setReuse(false);

  // WiFiClientSecure herda de WiFiClient, mas passamos o objeto concreto para
  // que o HTTPClient use a pilha TLS correta.
  const bool began = useTls ? http.begin(secureClient, HEALTH_URL)
                            : http.begin(plainClient, HEALTH_URL);
  if (!began) {
    g_state.lastError = "URL invalida em HEALTH_URL";
    return ST_FETCH_ERR;
  }
  http.addHeader("Authorization", "Bearer " HEALTH_TOKEN);
  http.addHeader("Accept", "application/json");

  int code = http.GET();

  if (code == 401 || code == 403) {
    http.end();
    g_state.lastError = "HTTP " + String(code) + " — token rejeitado";
    return ST_AUTH_ERR;
  }
  if (code != 200) {
    String detail = code > 0 ? ("HTTP " + String(code))
                             : String(http.errorToString(code));
    http.end();
    g_state.lastError = detail;
    return ST_FETCH_ERR;
  }

  String payload = http.getString();
  http.end();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    g_state.lastError = String("JSON invalido: ") + err.c_str();
    return ST_FETCH_ERR;
  }

  Health h;
  h.valid = true;
  strlcpy(h.host, doc["host"] | "?", sizeof(h.host));
  h.cpu = doc["cpu"].isNull() ? NAN : doc["cpu"].as<float>();
  h.mem = doc["mem"].isNull() ? NAN : doc["mem"].as<float>();
  h.disk = doc["disk"].isNull() ? NAN : doc["disk"].as<float>();
  h.load = doc["ld"].isNull() ? NAN : doc["ld"].as<float>();
  h.uptime = doc["up"] | 0UL;
  h.svcTotal = doc["svc"][0] | 0;
  h.svcBad = doc["svc"][1] | 0;
  h.dkrTotal = doc["dkr"][0] | 0;
  h.dkrBad = doc["dkr"][1] | 0;
  h.nbad = doc["nbad"] | 0;

  JsonArrayConst bad = doc["bad"].as<JsonArrayConst>();
  h.badCount = 0;
  for (JsonVariantConst item : bad) {
    if (h.badCount >= 8) break;
    h.bad[h.badCount++] = item.as<const char *>();
  }

  const char *st = doc["st"] | "unknown";
  g_state.health = h;
  g_state.lastError = "";
  g_lastOkFetchMs = millis();

  if (strcmp(st, "ok") == 0) return ST_OK;
  if (strcmp(st, "degraded") == 0) return ST_DEGRADED;
  if (strcmp(st, "down") == 0) return ST_DOWN;
  return ST_FETCH_ERR;
}

// ---------------------------------------------------------------------------
// Log serial
// ---------------------------------------------------------------------------

static void printDashboard() {
  const Health &h = g_state.health;
  Serial.println();
  Serial.println("======================================================");
  Serial.printf("  VM: %-20s   ESTADO: %s\n",
                h.valid ? h.host : "-", statusName(g_state.status));
  Serial.println("------------------------------------------------------");

  if (h.valid && statusHasVmData(g_state.status)) {
    Serial.printf("  CPU %-8s  RAM %-8s  DISCO %-8s  LOAD %.2f\n",
                  pct(h.cpu).c_str(), pct(h.mem).c_str(), pct(h.disk).c_str(),
                  isnan(h.load) ? 0.0f : h.load);
    Serial.printf("  Servicos: %d falhos / %d      Containers: %d falhos / %d\n",
                  h.svcBad, h.svcTotal, h.dkrBad, h.dkrTotal);
    Serial.printf("  Uptime da VM: %s\n", humanUptime(h.uptime).c_str());
    if (h.badCount > 0) {
      Serial.println("  PROBLEMAS:");
      for (int i = 0; i < h.badCount; i++) {
        Serial.printf("    - %s\n", h.bad[i].c_str());
      }
      if (h.nbad > h.badCount) {
        Serial.printf("    ... e mais %d\n", h.nbad - h.badCount);
      }
    }
  } else if (g_state.lastError.length()) {
    Serial.printf("  ERRO: %s\n", g_state.lastError.c_str());
    if (g_lastOkFetchMs) {
      Serial.printf("  Ultima leitura boa: ha %lus\n",
                    (unsigned long)staleSeconds());
    }
  }

  Serial.println("------------------------------------------------------");
  Serial.printf("  Wi-Fi %s (%d dBm)  IP %s  RAM livre %u B  polls %lu\n",
                g_state.wifiUp ? "ok" : "off", g_state.rssi,
                WiFi.localIP().toString().c_str(), ESP.getFreeHeap(),
                (unsigned long)g_state.polls);
  Serial.println("======================================================");
}

// ---------------------------------------------------------------------------
// Servidor web local
// ---------------------------------------------------------------------------

#if ENABLE_WEB_UI
static WebServer server(80);

static const char *statusCssColor(Status s) {
  switch (s) {
    case ST_OK:       return "#1f9d55";
    case ST_DEGRADED: return "#c98a00";
    default:          return "#c0392b";
  }
}

static void handleRoot() {
  const Health &h = g_state.health;
  String html;
  html.reserve(4096);
  html += F("<!doctype html><html lang=pt-BR><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta http-equiv=refresh content=10><title>ESP32 VM Monitor</title>"
            "<style>"
            "body{font-family:system-ui,-apple-system,sans-serif;margin:0;"
            "background:#12151a;color:#e8eaed;padding:24px}"
            ".card{max-width:640px;margin:0 auto;background:#1b1f27;"
            "border-radius:12px;padding:24px}"
            ".badge{display:inline-block;padding:6px 14px;border-radius:999px;"
            "font-weight:600;color:#fff}"
            ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));"
            "gap:12px;margin:20px 0}"
            ".m{background:#232833;border-radius:8px;padding:12px}"
            ".m b{display:block;font-size:24px;margin-top:4px}"
            ".m span{font-size:12px;color:#9aa3b2;text-transform:uppercase}"
            "ul{padding-left:20px}li{margin:4px 0;color:#ff8a80}"
            ".foot{color:#7d8695;font-size:12px;margin-top:20px;"
            "border-top:1px solid #2b3140;padding-top:12px}"
            "</style></head><body><div class=card>");

  html += F("<h1 style='margin:0 0 12px'>");
  html += h.valid ? h.host : "ESP32 VM Monitor";
  html += F("</h1><span class=badge style='background:");
  html += statusCssColor(g_state.status);
  html += F("'>");
  html += statusName(g_state.status);
  html += F("</span>");

  if (h.valid) {
    html += F("<div class=grid>");
    html += "<div class=m><span>CPU</span><b>" + pct(h.cpu) + "</b></div>";
    html += "<div class=m><span>RAM</span><b>" + pct(h.mem) + "</b></div>";
    html += "<div class=m><span>Disco</span><b>" + pct(h.disk) + "</b></div>";
    html += "<div class=m><span>Servicos</span><b>" + String(h.svcBad) + "/" +
            String(h.svcTotal) + "</b></div>";
    html += "<div class=m><span>Containers</span><b>" + String(h.dkrBad) + "/" +
            String(h.dkrTotal) + "</b></div>";
    html += "<div class=m><span>Uptime VM</span><b>" + humanUptime(h.uptime) +
            "</b></div>";
    html += F("</div>");

    if (h.badCount > 0) {
      html += F("<h3>Problemas</h3><ul>");
      for (int i = 0; i < h.badCount; i++) html += "<li>" + h.bad[i] + "</li>";
      if (h.nbad > h.badCount) {
        html += "<li>... e mais " + String(h.nbad - h.badCount) + "</li>";
      }
      html += F("</ul>");
    }
  }

  if (g_state.lastError.length()) {
    html += "<p style='color:#ff8a80'><b>Erro:</b> " + g_state.lastError + "</p>";
  }

  html += F("<div class=foot>");
  html += "Ultima leitura boa ha " + String(g_state.staleS) + "s &middot; ";
  html += "Wi-Fi " + String(g_state.rssi) + " dBm &middot; ";
  html += "RAM livre " + String(ESP.getFreeHeap()) + " B &middot; ";
  html += "polls " + String(g_state.polls) + " &middot; falhas " +
          String(g_state.failures);
  html += F("<br><a style='color:#7d8695' href='/json'>/json</a> &middot; "
            "<a style='color:#7d8695' href='/refresh'>forcar atualizacao</a>");
  html += F("</div></div></body></html>");

  server.send(200, "text/html; charset=utf-8", html);
}

static void handleJson() {
  const Health &h = g_state.health;
  JsonDocument doc;
  doc["device_status"] = statusName(g_state.status);
  doc["vm_host"] = h.host;
  doc["cpu"] = h.cpu;
  doc["mem"] = h.mem;
  doc["disk"] = h.disk;
  doc["svc_bad"] = h.svcBad;
  doc["svc_total"] = h.svcTotal;
  doc["dkr_bad"] = h.dkrBad;
  doc["dkr_total"] = h.dkrTotal;
  doc["stale_s"] = g_state.staleS;
  doc["last_error"] = g_state.lastError;
  doc["polls"] = g_state.polls;
  doc["failures"] = g_state.failures;
  doc["free_heap"] = ESP.getFreeHeap();
  doc["rssi"] = g_state.rssi;
  JsonArray bad = doc["problems"].to<JsonArray>();
  for (int i = 0; i < h.badCount; i++) bad.add(h.bad[i]);

  String out;
  serializeJson(doc, out);
  server.send(200, "application/json", out);
}

static void handleRefresh() {
  g_lastAttemptMs = 0;  // faz o loop consultar imediatamente
  server.sendHeader("Location", "/");
  server.send(303, "text/plain", "");
}
#endif  // ENABLE_WEB_UI

// ---------------------------------------------------------------------------
// setup / loop
// ---------------------------------------------------------------------------

// mDNS e servidor web so podem subir com Wi-Fi ativo. Se a rede so aparecer
// depois do boot, o loop chama isto de novo.
static bool g_servicesUp = false;

static void startNetworkServices() {
  if (g_servicesUp || WiFi.status() != WL_CONNECTED) return;

  syncTime();

  if (MDNS.begin(DEVICE_HOSTNAME)) {
    MDNS.addService("http", "tcp", 80);
    Serial.printf("[mdns] http://%s.local\n", DEVICE_HOSTNAME);
  }
#if ENABLE_WEB_UI
  server.on("/", handleRoot);
  server.on("/json", handleJson);
  server.on("/refresh", handleRefresh);
  server.begin();
  Serial.printf("[web] http://%s\n", WiFi.localIP().toString().c_str());
#endif
  g_servicesUp = true;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("=== ESP32 VM Monitor (Cheap Yellow Display) ===");
  Serial.printf("Alvo: %s\n", HEALTH_URL);
#if !VERIFY_TLS
  Serial.println("AVISO: VERIFY_TLS=0 — o certificado da VM nao sera validado.");
#endif

  pinMode(RGB_R_PIN, OUTPUT);
  pinMode(RGB_G_PIN, OUTPUT);
  pinMode(RGB_B_PIN, OUTPUT);
  rgbSet(false, false, false);
#if BUZZER_PIN >= 0
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
#endif

  displayBegin();

  connectWifi();
  startNetworkServices();
}

void loop() {
  ledTask();
  beepTask();
#if ENABLE_WEB_UI
  server.handleClient();
#endif

  // Toque na tela força uma consulta imediata.
  if (displayTouchConsumed()) {
    Serial.println("[touch] atualizacao forcada");
    g_lastAttemptMs = 0;
  }

  // Reconexao de Wi-Fi.
  static uint32_t lastWifiCheck = 0;
  if (millis() - lastWifiCheck > 5000) {
    lastWifiCheck = millis();
    if (WiFi.status() != WL_CONNECTED) {
      g_state.status = ST_WIFI_DOWN;
      g_state.lastError = "Wi-Fi desconectado";
      Serial.println("[wifi] caiu — tentando reconectar");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    } else {
      startNetworkServices();  // no-op se ja estiverem de pe
      if (!g_timeSynced) syncTime();
    }
  }

  // O contador "ha Ns" da tela precisa andar mesmo entre consultas.
  static uint32_t lastRenderMs = 0;
  if (millis() - lastRenderMs > 1000) {
    lastRenderMs = millis();
    refreshDerived();
    displayRender(g_state);
  }

  // Quando algo esta errado consultamos com mais frequencia.
  uint32_t interval = (g_state.status == ST_OK || g_state.status == ST_BOOT)
                          ? POLL_INTERVAL_MS
                          : POLL_INTERVAL_ALERT_MS;

  if (g_lastAttemptMs != 0 && millis() - g_lastAttemptMs < interval) return;
  g_lastAttemptMs = millis();
  g_state.polls++;

  Status next = fetchHealth();

  if (statusHasVmData(next)) {
    g_state.failures = 0;
  } else {
    g_state.failures++;
    Serial.printf("[erro] %s (falha %lu de %d antes de reiniciar)\n",
                  g_state.lastError.c_str(), (unsigned long)g_state.failures,
                  MAX_FAILURES_BEFORE_REBOOT);
  }

  g_prevStatus = g_state.status;
  g_state.status = next;
  g_ledStep = 0;  // reinicia o padrao para a troca ser percebida na hora

  // Apita apenas na transicao para FORA DO AR, nao a cada consulta.
  if (g_state.status == ST_DOWN && g_prevStatus != ST_DOWN) {
    beep(BUZZER_BEEPS);
  }

  refreshDerived();
  displayRender(g_state);
  printDashboard();

  // Ultimo recurso: se a rede nunca voltar, reinicia a placa.
  if (g_state.failures >= MAX_FAILURES_BEFORE_REBOOT) {
    Serial.println("[sys] falhas demais seguidas — reiniciando o ESP32");
    Serial.flush();
    delay(200);
    ESP.restart();
  }
}
