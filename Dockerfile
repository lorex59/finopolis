# syntax=docker/dockerfile:1

# PROD-образ: запускает Telegram-бота (aiogram), FastAPI (uvicorn),
# и nginx как TLS-терминатор (HTTPS). Для ACME (HTTP-01) используется webroot.

FROM python:3.12-slim AS base

RUN apt-get update \
  && apt-get install -y --no-install-recommends nginx openssl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python зависимости
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY . .

# Каталог для ACME webroot (HTTP-01)
RUN mkdir -p /var/www/certbot

# ВРЕМЕННЫЙ (placeholder) сертификат, чтобы nginx стартовал до LE
# Позже будет заменён symlink-ами на файлы из /etc/letsencrypt/live/<domain>/
RUN mkdir -p /etc/ssl/certs /etc/ssl/private && \
    openssl req -x509 -nodes -days 3 -newkey rsa:2048 \
      -keyout /etc/ssl/private/privkey.pem \
      -out /etc/ssl/certs/fullchain.pem \
      -subj "/CN=176-108-244-31.sslip.io"

# Конфиг nginx
COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 80 443

ENV BACKEND_URL="https://176-108-244-31.sslip.io"
ENV PYTHONPATH="/app/app"

# Запуск: бот + uvicorn + nginx в форграунде
CMD bash -lc "python -m app.bot & \
  python -m uvicorn app.webapp:app --host 0.0.0.0 --port 8000 & \
  nginx -g 'daemon off;'"
