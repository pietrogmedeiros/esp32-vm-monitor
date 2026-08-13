# Agente de saude para rodar como container (deploy via Easypanel/Docker).
#
# O agente em si so usa a biblioteca padrao do Python. A unica dependencia
# externa e o cliente docker, usado para ler o estado dos containers atraves
# do socket montado em /var/run/docker.sock.
#
# Dentro de um container, /proc/stat e /proc/meminfo ja refletem o host (nao
# sao isolados por namespace), entao CPU e memoria saem corretos sem montagem
# extra. O systemd do host nao e visivel — num servidor gerenciado por
# Easypanel isso nao importa, porque os servicos sao todos containers.
FROM python:3.12-alpine

RUN apk add --no-cache docker-cli

WORKDIR /app
COPY health_agent.py /app/health_agent.py

ENV PYTHONUNBUFFERED=1
ENV HEALTH_AGENT_CONFIG=/etc/vm-health-agent/config.json

EXPOSE 9099

# Sem shell no meio: o sinal de parada chega direto no Python.
CMD ["python3", "/app/health_agent.py"]
