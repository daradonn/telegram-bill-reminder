FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY bot.py /app/bot.py
RUN useradd --system --uid 10001 bot \
    && mkdir -p /data/invoices \
    && chown -R bot:bot /app /data

USER bot
VOLUME ["/data"]
CMD ["python", "/app/bot.py"]
