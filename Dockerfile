FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_HOST=0.0.0.0

WORKDIR /app
COPY x_lark_bot.py /app/x_lark_bot.py
COPY web_app.py /app/web_app.py
COPY web /app/web

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /var/data \
    && chown -R bot:bot /app/data /var/data
USER bot

EXPOSE 10000

CMD ["python", "/app/web_app.py"]
