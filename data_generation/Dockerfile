# Imagem do gerador de massa de dados (Fase 1). Isolada do host para evitar
# conflitos de ambiente Python local (ex.: venv aninhado com conda) — ver
# data_generation/README.md, seção "Como executar".

FROM python:3.11-slim

WORKDIR /app

# libgomp1: runtime OpenMP exigido pela extensão compilada de `implicit`
# (ausente na imagem slim por padrão).
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY generator ./generator

RUN pip install --no-cache-dir -e ".[dev]"

COPY tests ./tests

# `data/` é montado como volume em docker-compose.yml — não faz parte da imagem.
ENTRYPOINT ["python", "-m", "generator"]
CMD ["--help"]
