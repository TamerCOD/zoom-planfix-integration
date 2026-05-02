FROM python:3.12-slim

WORKDIR /app

# Копируем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY *.py ./

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8000/health').raise_for_status()"

EXPOSE 8000

# Railway передаёт порт через $PORT — используем shell для подстановки
CMD ["sh", "-c", "uvicorn webhook_server:app --host 0.0.0.0 --port ${PORT:-8000}"]
