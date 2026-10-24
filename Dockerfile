FROM python:3.12-slim

WORKDIR /app

# Instalação de dependências do sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar arquivos do projeto
COPY pyproject.toml .
COPY src/ src/
COPY scripts/ scripts/

# Instalar pacote e dependências
RUN pip install --no-cache-dir -e .

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV GATEWAY_DATABASE_URL=sqlite:///./gateway.db
ENV GATEWAY_BILLING_MODE=request
ENV GATEWAY_UNIT_PRICE_SAT=1

CMD ["uvicorn", "ln_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
