// Painel na tela de 2.8" da CYD (ILI9341, 320x240 em paisagem).
#pragma once

#include "monitor_state.h"

// Liga o backlight e pinta a tela inicial. Seguro chamar mesmo com
// ENABLE_DISPLAY 0 — nesse caso todas as funcoes viram no-op.
void displayBegin();

// Linha de status durante o boot (Wi-Fi, NTP, etc).
void displayBootMessage(const char *line);

// Redesenha o painel. So repinta as secoes cujo conteudo mudou, para a tela
// nao piscar a cada consulta.
void displayRender(const MonitorState &state);

// Consome um toque na tela, se houver. Usado para forcar uma atualizacao.
bool displayTouchConsumed();
