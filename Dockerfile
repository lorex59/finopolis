# syntax=docker/dockerfile:1

# Этот Dockerfile собирает образ для Telegram‑бота и FastAPI мини‑приложения
# проекта Finopolis. В контейнере одновременно запускаются bot.py (с
# использованием aiogram) и веб‑приложение на Uvicorn, а Nginx
# проксирует запросы по HTTPS на FastAPI. Сертификат генерируется
# автоматически при сборке.

FROM python:3.12-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем Python‑зависимости
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код
COPY . .

# Генерируем сертификат и устанавливаем конфигурацию Nginx
RUN mkdir -p /etc/ssl/certs /etc/ssl/private && \
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout /etc/ssl/private/privkey.pem \
        -out /etc/ssl/certs/fullchain.pem \
        -subj "/CN=176-108-244-31.sslip.io"

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 80 443
ENV BACKEND_URL=https://176-108-244-31.sslip.io
ENV PYTHONPATH="/app/app"
CMD ["bash", "-c", "python -m app.bot & python -m uvicorn app.webapp:app --host 0.0.0.0 --port 8000 & nginx -g 'daemon off;'"]

