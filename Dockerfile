FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY app.py db_engine.py jlc_fetcher.py ./
COPY static/ ./static/

EXPOSE 8080
CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8080", "--no-browser"]
