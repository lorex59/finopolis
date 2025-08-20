# syntax=docker/dockerfile:1

# Этот Dockerfile собирает образ для Telegram‑бота и FastAPI мини‑приложения
# проекта Finopolis. В контейнере одновременно запускаются bot.py (с
# использованием aiogram) и веб‑приложение на Uvicorn, а Nginx
# проксирует запросы по HTTPS на FastAPI. Сертификат генерируется
# автоматически при сборке.

FROM python:3.12-slim AS base

# Устанавливаем системные зависимости
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

## Устанавливаем Python‑зависимости отдельно, чтобы кешировалось
COPY finopolis-main/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

## Копируем исходный код приложения
COPY finopolis-main/ .

## Генерируем самоподписанный TLS‑сертификат. В реальном деплое
## рекомендуется заменить на сертификат от доверенного центра (Let's Encrypt).
RUN mkdir -p /etc/ssl/certs /etc/ssl/private && \
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout /etc/ssl/private/privkey.pem \
        -out /etc/ssl/certs/fullchain.pem \
        -subj "/CN=176-108-244-31.nip.io"

## Настройка Nginx: заменяем конфигурацию на нашу
COPY finopolis-main/nginx.conf /etc/nginx/nginx.conf

## Открываем порты HTTP и HTTPS
EXPOSE 80 443

## Устанавливаем переменные окружения. BACKEND_URL определяет URL, который
## будет использоваться ботом и мини‑приложением для формирования ссылок.
ENV BACKEND_URL=https://176-108-244-31.nip.io

## Команда запуска: запускаем Uvicorn, бот и затем Nginx. Uvicorn и бот
## работают на фоне (&), nginx остаётся в первом плане для корректного
## управления процессом.
CMD ["bash", "-c", \
     "python -m app.bot & python -m uvicorn app.webapp:app --host 0.0.0.0 --port 8000 & nginx -g 'daemon off;'"]
