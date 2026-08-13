// Tipos compartilhados entre a logica de rede (main.cpp) e o desenho da
// tela (display.cpp).
#pragma once

#include <Arduino.h>

// Do pior para o melhor. A ordem importa: o codigo compara com <= / >=
// em alguns pontos para decidir se ha dados vindos da VM.
enum Status : uint8_t {
  ST_BOOT,
  ST_WIFI_DOWN,
  ST_FETCH_ERR,
  ST_AUTH_ERR,
  ST_DOWN,
  ST_DEGRADED,
  ST_OK,
};

inline const char *statusName(Status s) {
  switch (s) {
    case ST_OK:        return "OK";
    case ST_DEGRADED:  return "DEGRADADO";
    case ST_DOWN:      return "FORA DO AR";
    case ST_AUTH_ERR:  return "TOKEN INVALIDO";
    case ST_FETCH_ERR: return "SEM RESPOSTA";
    case ST_WIFI_DOWN: return "SEM WI-FI";
    default:           return "INICIANDO";
  }
}

// Verdadeiro quando o estado veio de fato de uma leitura da VM, e nao de
// um erro de rede ou do boot.
inline bool statusHasVmData(Status s) {
  return s == ST_OK || s == ST_DEGRADED || s == ST_DOWN;
}

// Espelha o payload de /health/summary.
struct Health {
  bool valid = false;
  char host[28] = "";
  float cpu = NAN, mem = NAN, disk = NAN, load = NAN;
  uint32_t uptime = 0;
  int svcTotal = 0, svcBad = 0, dkrTotal = 0, dkrBad = 0;
  int nbad = 0;
  String bad[8];
  int badCount = 0;
};

// Uma linha de anomalia vinda do log do serviço em foco.
struct LogLine {
  String time;   // "00:11:02"
  String level;  // "err" | "warn"
  String text;
};

// Segunda tela: detalhe de um único serviço.
struct Focus {
  bool valid = false;
  char name[30] = "";
  char replicas[12] = "";
  Status status = ST_BOOT;
  int errors = 0, warnings = 0, scanned = 0;
  int runningTasks = 0, failedTasks = 0;
  String lastErrorAt;
  LogLine lines[4];
  int lineCount = 0;
};

// Qual painel está no ar.
enum Screen : uint8_t {
  SCREEN_OVERVIEW,
  SCREEN_FOCUS,
};

// Tudo que a tela precisa saber para se desenhar.
struct MonitorState {
  Status status = ST_BOOT;
  Health health;
  String lastError;
  uint32_t staleS = 0;   // segundos desde a ultima leitura boa
  int rssi = 0;
  uint32_t polls = 0;
  uint32_t failures = 0;
  bool wifiUp = false;
  Focus focus;
  Screen screen = SCREEN_OVERVIEW;
};
