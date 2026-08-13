#include "display.h"

#if __has_include("monitor_config.h")
#include "monitor_config.h"
#endif

#if ENABLE_DISPLAY

#include <SPI.h>
#include <TFT_eSPI.h>

#if ENABLE_TOUCH
#include <XPT2046_Touchscreen.h>

// Pinos do controlador de toque da CYD. Ele fica num barramento SPI
// diferente do display, por isso precisa de um SPIClass proprio.
#define XPT2046_IRQ  36
#define XPT2046_MOSI 32
#define XPT2046_MISO 39
#define XPT2046_CLK  25
#define XPT2046_CS   33

static SPIClass touchSPI(VSPI);
static XPT2046_Touchscreen ts(XPT2046_CS, XPT2046_IRQ);
#endif

static TFT_eSPI tft;

// --------------------------------------------------------------------------
// Paleta (RGB565)
// --------------------------------------------------------------------------

static const uint16_t COL_BG     = 0x10A3;  // #12151A
static const uint16_t COL_PANEL  = 0x18E4;  // #1B1F27
static const uint16_t COL_TEXT   = 0xFFFF;
static const uint16_t COL_MUTED  = 0x9D16;  // #9AA3B2
static const uint16_t COL_OK     = 0x1CEA;  // #1F9D55
static const uint16_t COL_WARN   = 0xCC40;  // #C98A00
static const uint16_t COL_BAD    = 0xC1C5;  // #C0392B
static const uint16_t COL_INFO   = 0x2C17;  // #2980B9
static const uint16_t COL_ALERT  = 0xF9A6;  // #FF8A80

static uint16_t statusColor(Status s) {
  switch (s) {
    case ST_OK:        return COL_OK;
    case ST_DEGRADED:  return COL_WARN;
    case ST_DOWN:      return COL_BAD;
    case ST_WIFI_DOWN: return COL_INFO;
    default:           return COL_BAD;
  }
}

// --------------------------------------------------------------------------
// Geometria (320x240 em paisagem)
// --------------------------------------------------------------------------

static const int16_t SCR_W = 320;
static const int16_t SCR_H = 240;

static const int16_t HEADER_Y = 0, HEADER_H = 46;
static const int16_t METRIC_Y = 52, METRIC_H = 66;
static const int16_t COUNT_Y = 122, COUNT_H = 24;
static const int16_t PROB_Y = 150, PROB_H = 64;
static const int16_t FOOT_Y = 218, FOOT_H = 22;

static const int16_t PAD = 8;

// --------------------------------------------------------------------------
// Cache: so repinta o que mudou, senao a tela pisca a cada consulta
// --------------------------------------------------------------------------

struct Cache {
  bool primed = false;
  Screen screen = SCREEN_OVERVIEW;
  Status status = ST_BOOT;
  String host;
  String stale;
  float cpu = -1, mem = -1, disk = -1;
  int svcBad = -1, svcTotal = -1, dkrBad = -1, dkrTotal = -1;
  String problems;
  String footer;
  String focusSig;
};

static Cache cache;

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

static String pctText(float v) {
  if (isnan(v)) return "-";
  char buf[12];
  snprintf(buf, sizeof(buf), "%.0f%%", v);
  return String(buf);
}

static String truncate(const String &s, uint16_t maxChars) {
  if (s.length() <= maxChars) return s;
  return s.substring(0, maxChars - 1) + "~";
}

// Barra de proporcao com a cor mudando conforme o valor.
static void drawBar(int16_t x, int16_t y, int16_t w, int16_t h, float value) {
  tft.fillRoundRect(x, y, w, h, 2, COL_PANEL);
  if (isnan(value) || value <= 0) return;
  float clamped = value > 100.0f ? 100.0f : value;
  int16_t filled = (int16_t)(w * clamped / 100.0f);
  if (filled < 2) filled = 2;
  uint16_t color = clamped >= 90 ? COL_BAD : (clamped >= 75 ? COL_WARN : COL_OK);
  tft.fillRoundRect(x, y, filled, h, 2, color);
}

// --------------------------------------------------------------------------
// Secoes
// --------------------------------------------------------------------------

static void drawHeader(const MonitorState &s) {
  uint16_t color = statusColor(s.status);
  tft.fillRect(0, HEADER_Y, SCR_W, HEADER_H, color);

  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(COL_TEXT, color);
  String host = s.health.valid ? String(s.health.host) : String("ESP32 VM Monitor");
  tft.drawString(truncate(host, 26), PAD, HEADER_Y + 3, 2);
  tft.drawString(statusName(s.status), PAD, HEADER_Y + 20, 4);
}

// O "ha Ns" muda a cada segundo — fica numa area propria para nao repintar
// o cabecalho inteiro o tempo todo.
static void drawStale(const MonitorState &s) {
  uint16_t color = statusColor(s.status);
  String text = s.staleS == 0 && s.polls == 0 ? "" : ("ha " + String(s.staleS) + "s");
  tft.fillRect(SCR_W - 88, HEADER_Y + 16, 88 - PAD, 20, color);
  tft.setTextDatum(TR_DATUM);
  tft.setTextColor(COL_TEXT, color);
  tft.drawString(text, SCR_W - PAD, HEADER_Y + 18, 2);
  tft.setTextDatum(TL_DATUM);
}

static void drawMetrics(const MonitorState &s) {
  tft.fillRect(0, METRIC_Y, SCR_W, METRIC_H, COL_BG);

  const char *labels[3] = {"CPU", "MEMORIA", "DISCO"};
  const float values[3] = {s.health.cpu, s.health.mem, s.health.disk};
  const int16_t colW = (SCR_W - PAD * 2) / 3;

  tft.setTextDatum(TL_DATUM);
  for (int i = 0; i < 3; i++) {
    int16_t x = PAD + i * colW;
    tft.setTextColor(COL_MUTED, COL_BG);
    tft.drawString(labels[i], x, METRIC_Y, 2);
    tft.setTextColor(COL_TEXT, COL_BG);
    tft.drawString(pctText(values[i]), x, METRIC_Y + 18, 4);
    drawBar(x, METRIC_Y + 50, colW - 12, 8, values[i]);
  }
}

static void drawCounts(const MonitorState &s) {
  tft.fillRect(0, COUNT_Y, SCR_W, COUNT_H, COL_BG);
  tft.setTextDatum(TL_DATUM);

  const Health &h = s.health;
  uint16_t svcColor = h.svcBad > 0 ? COL_ALERT : COL_MUTED;
  uint16_t dkrColor = h.dkrBad > 0 ? COL_ALERT : COL_MUTED;

  tft.setTextColor(svcColor, COL_BG);
  tft.drawString("Servicos " + String(h.svcBad) + "/" + String(h.svcTotal),
                 PAD, COUNT_Y + 2, 2);

  tft.setTextColor(dkrColor, COL_BG);
  tft.drawString("Containers " + String(h.dkrBad) + "/" + String(h.dkrTotal),
                 PAD + 150, COUNT_Y + 2, 2);
}

static void drawProblems(const MonitorState &s) {
  tft.fillRect(0, PROB_Y, SCR_W, PROB_H, COL_BG);
  tft.setTextDatum(TL_DATUM);

  // Erro de rede/token tem prioridade: nesse caso nao ha lista de problemas
  // da VM para mostrar, e sim o motivo de nao termos conseguido falar com ela.
  if (!statusHasVmData(s.status)) {
    tft.setTextColor(COL_MUTED, COL_BG);
    tft.drawString("DIAGNOSTICO", PAD, PROB_Y, 2);
    tft.setTextColor(COL_ALERT, COL_BG);
    tft.drawString(truncate(s.lastError, 40), PAD, PROB_Y + 18, 2);
    if (s.failures > 0) {
      tft.setTextColor(COL_MUTED, COL_BG);
      tft.drawString("falhas seguidas: " + String(s.failures), PAD,
                     PROB_Y + 36, 2);
    }
    return;
  }

  if (s.health.badCount == 0) {
    tft.setTextColor(COL_MUTED, COL_BG);
    tft.drawString("Nenhum problema detectado", PAD, PROB_Y + 18, 2);
    return;
  }

  tft.setTextColor(COL_MUTED, COL_BG);
  tft.drawString("PROBLEMAS", PAD, PROB_Y, 2);

  const int maxLines = 2;
  int shown = s.health.badCount < maxLines ? s.health.badCount : maxLines;
  tft.setTextColor(COL_ALERT, COL_BG);
  for (int i = 0; i < shown; i++) {
    tft.drawString(truncate(s.health.bad[i], 40), PAD, PROB_Y + 18 + i * 17, 2);
  }
  if (s.health.nbad > shown) {
    tft.setTextColor(COL_MUTED, COL_BG);
    tft.drawString("+ " + String(s.health.nbad - shown) + " outros", PAD,
                   PROB_Y + 18 + shown * 17, 2);
  }
}

static String footerText(const MonitorState &s) {
  String text = s.wifiUp ? ("Wi-Fi " + String(s.rssi) + "dBm") : "Wi-Fi off";
  if (s.health.valid && s.health.uptime > 0) {
    uint32_t days = s.health.uptime / 86400;
    uint32_t hours = (s.health.uptime % 86400) / 3600;
    text += days > 0 ? ("  up " + String(days) + "d" + String(hours) + "h")
                     : ("  up " + String(hours) + "h");
  }
  text += "  #" + String(s.polls);
  return text;
}

static void drawFooter(const MonitorState &s, const String &text) {
  tft.fillRect(0, FOOT_Y, SCR_W, FOOT_H, COL_PANEL);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(COL_MUTED, COL_PANEL);
  tft.drawString(truncate(text, 44), PAD, FOOT_Y + 3, 2);
}

// --------------------------------------------------------------------------
// Tela 2 — detalhe de um serviço
// --------------------------------------------------------------------------

static void drawFocus(const MonitorState &s) {
  const Focus &f = s.focus;
  uint16_t color = statusColor(f.valid ? f.status : ST_FETCH_ERR);

  // Cabeçalho: nome do serviço e réplicas.
  tft.fillRect(0, HEADER_Y, SCR_W, HEADER_H, color);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(COL_TEXT, color);
  tft.drawString(truncate(String(f.name), 30), PAD, HEADER_Y + 3, 2);
  tft.drawString(statusName(f.valid ? f.status : ST_FETCH_ERR), PAD,
                 HEADER_Y + 20, 4);
  tft.setTextDatum(TR_DATUM);
  tft.drawString(String(f.replicas), SCR_W - PAD, HEADER_Y + 18, 4);
  tft.setTextDatum(TL_DATUM);

  if (!f.valid) {
    tft.fillRect(0, METRIC_Y, SCR_W, SCR_H - METRIC_Y - FOOT_H, COL_BG);
    tft.setTextColor(COL_MUTED, COL_BG);
    tft.drawString("sem dados deste servico", PAD, METRIC_Y + 20, 2);
    return;
  }

  // Contadores: erros, avisos e reinícios de task.
  tft.fillRect(0, METRIC_Y, SCR_W, METRIC_H - 12, COL_BG);
  const char *labels[3] = {"ERROS", "AVISOS", "REINICIOS"};
  const int values[3] = {f.errors, f.warnings, f.failedTasks};
  const uint16_t colors[3] = {f.errors > 0 ? COL_ALERT : COL_TEXT,
                              f.warnings > 0 ? COL_WARN : COL_TEXT,
                              f.failedTasks > 0 ? COL_ALERT : COL_TEXT};
  const int16_t colW = (SCR_W - PAD * 2) / 3;
  for (int i = 0; i < 3; i++) {
    int16_t x = PAD + i * colW;
    tft.setTextColor(COL_MUTED, COL_BG);
    tft.drawString(labels[i], x, METRIC_Y, 2);
    tft.setTextColor(colors[i], COL_BG);
    tft.drawString(String(values[i]), x, METRIC_Y + 18, 4);
  }

  // Últimas anomalias — o miolo desta tela.
  const int16_t listY = METRIC_Y + METRIC_H - 6;
  tft.fillRect(0, listY, SCR_W, FOOT_Y - listY, COL_BG);
  // Sem anomalia, o agente manda as últimas linhas comuns. O título muda para
  // deixar claro o que se está olhando — senão log rotineiro pareceria alerta.
  const bool anomalias = f.errors > 0 || f.warnings > 0;
  tft.setTextColor(COL_MUTED, COL_BG);
  tft.drawString(f.lineCount == 0 ? "Sem logs recentes"
                 : anomalias      ? "ULTIMAS ANOMALIAS"
                                  : "LOG RECENTE (sem anomalias)",
                 PAD, listY, 2);

  for (int i = 0; i < f.lineCount && i < 4; i++) {
    int16_t y = listY + 18 + i * 17;
    if (y + 16 > FOOT_Y) break;
    const LogLine &line = f.lines[i];
    // O horário fica cinza para o olho ir direto à mensagem.
    tft.setTextColor(COL_MUTED, COL_BG);
    tft.drawString(line.time.substring(0, 5), PAD, y, 2);
    uint16_t color = line.level == "err"    ? COL_ALERT
                     : line.level == "warn" ? COL_WARN
                                            : COL_MUTED;
    tft.setTextColor(color, COL_BG);
    tft.drawString(truncate(line.text, 34), PAD + 42, y, 2);
  }

  // Rodapé com o tamanho da janela analisada.
  String foot = String(f.scanned) + " linhas";
  if (f.lastErrorAt.length()) foot += "  ultimo erro " + f.lastErrorAt;
  foot += "  " + String(f.runningTasks) + " task";
  drawFooter(s, foot);
}

// --------------------------------------------------------------------------
// API
// --------------------------------------------------------------------------

void displayBegin() {
  tft.init();
  tft.setRotation(DISPLAY_ROTATION);
  tft.fillScreen(COL_BG);

  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, TFT_BACKLIGHT_ON);

#if ENABLE_TOUCH
  touchSPI.begin(XPT2046_CLK, XPT2046_MISO, XPT2046_MOSI, XPT2046_CS);
  ts.begin(touchSPI);
  ts.setRotation(DISPLAY_ROTATION);
#endif

  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(COL_TEXT, COL_BG);
  tft.drawString("ESP32 VM Monitor", PAD, PAD, 4);
  tft.setTextColor(COL_MUTED, COL_BG);
  tft.drawString("iniciando...", PAD, PAD + 34, 2);
}

void displayBootMessage(const char *line) {
  tft.fillRect(0, PAD + 56, SCR_W, 24, COL_BG);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(COL_MUTED, COL_BG);
  tft.drawString(truncate(String(line), 40), PAD, PAD + 58, 2);
}

// Zera o cache para que a proxima renderizacao repinte tudo.
static void invalidateCache() {
  cache.status = ST_BOOT;
  cache.host = "\x01";
  cache.stale = "\x01";
  cache.cpu = cache.mem = cache.disk = -1;
  cache.svcBad = cache.svcTotal = cache.dkrBad = cache.dkrTotal = -1;
  cache.problems = "\x01";
  cache.footer = "\x01";
  cache.focusSig = "\x01";
}

void displayRender(const MonitorState &s) {
  // A primeira renderizacao limpa a tela de boot inteira.
  if (!cache.primed) {
    tft.fillScreen(COL_BG);
    cache.primed = true;
    invalidateCache();
  }

  // Trocar de painel exige repintura completa: as secoes nao se sobrepoem.
  if (s.screen != cache.screen) {
    tft.fillScreen(COL_BG);
    cache.screen = s.screen;
    invalidateCache();
  }

  if (s.screen == SCREEN_FOCUS) {
    const Focus &f = s.focus;
    String sig = String(f.valid) + "|" + f.name + "|" + f.replicas + "|" +
                 String((int)f.status) + "|" + String(f.errors) + "|" +
                 String(f.warnings) + "|" + String(f.failedTasks) + "|" +
                 String(f.scanned) + "|" + f.lastErrorAt + "|";
    for (int i = 0; i < f.lineCount; i++) sig += f.lines[i].text + ";";
    if (sig != cache.focusSig) {
      drawFocus(s);
      cache.focusSig = sig;
    }
    return;
  }

  String host = s.health.valid ? String(s.health.host) : String("");
  if (s.status != cache.status || host != cache.host) {
    drawHeader(s);
    drawStale(s);
    cache.status = s.status;
    cache.host = host;
    cache.stale = "ha " + String(s.staleS) + "s";
  } else {
    String stale = "ha " + String(s.staleS) + "s";
    if (stale != cache.stale) {
      drawStale(s);
      cache.stale = stale;
    }
  }

  if (s.health.cpu != cache.cpu || s.health.mem != cache.mem ||
      s.health.disk != cache.disk) {
    drawMetrics(s);
    cache.cpu = s.health.cpu;
    cache.mem = s.health.mem;
    cache.disk = s.health.disk;
  }

  if (s.health.svcBad != cache.svcBad || s.health.svcTotal != cache.svcTotal ||
      s.health.dkrBad != cache.dkrBad || s.health.dkrTotal != cache.dkrTotal) {
    drawCounts(s);
    cache.svcBad = s.health.svcBad;
    cache.svcTotal = s.health.svcTotal;
    cache.dkrBad = s.health.dkrBad;
    cache.dkrTotal = s.health.dkrTotal;
  }

  // Assinatura barata do conteudo da area de problemas.
  String problems = String((int)s.status) + "|" + s.lastError + "|";
  for (int i = 0; i < s.health.badCount; i++) problems += s.health.bad[i] + ";";
  if (problems != cache.problems) {
    drawProblems(s);
    cache.problems = problems;
  }

  String footer = footerText(s);
  if (footer != cache.footer) {
    drawFooter(s, footer);
    cache.footer = footer;
  }
}

bool displayTouchConsumed() {
#if ENABLE_TOUCH
  static uint32_t lastTouchMs = 0;
  if (!ts.tirqTouched() || !ts.touched()) return false;
  uint32_t now = millis();
  if (now - lastTouchMs < 600) return false;  // debounce
  lastTouchMs = now;
  return true;
#else
  return false;
#endif
}

#else  // ENABLE_DISPLAY == 0

void displayBegin() {}
void displayBootMessage(const char *) {}
void displayRender(const MonitorState &) {}
bool displayTouchConsumed() { return false; }

#endif
