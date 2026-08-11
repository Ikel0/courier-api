FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY web ./web
RUN mkdir -p /app/data

EXPOSE 10000

CMD ["sh", "-c", "uvicorn courier_api.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
