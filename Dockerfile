FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/shop.sqlite3
COPY bot.py .
CMD ["python", "bot.py"]
