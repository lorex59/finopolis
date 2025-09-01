# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

RUN apt-get update \
  && apt-get install -y --no-install-recommends nginx \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Больше НЕ генерируем self-signed!
# COPY корректную конфигурацию Nginx
COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 80 443
ENV BACKEND_URL=https://176-108-244-31.sslip.io
ENV PYTHONPATH="/app/app"

CMD ["bash", "-c", "python -m app.bot & python -m uvicorn app.webapp:app --host 0.0.0.0 --port 8000 & nginx -g 'daemon off;'"]
