FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY newsbot ./newsbot
COPY config ./config

# Run as an unprivileged user; data/ and logs/ are volumes.
RUN useradd --create-home --uid 10001 bot && mkdir -p data logs && chown -R bot:bot /app
USER bot

# Container health = the bot wrote an equity snapshot in the last 5 minutes.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import sqlite3,sys,datetime as d; c=sqlite3.connect('/app/data/newsbot.db'); \
t=c.execute('select max(ts) from equity').fetchone()[0]; \
now=d.datetime.now(d.timezone.utc).replace(tzinfo=None); \
sys.exit(0 if t and now-d.datetime.fromisoformat(t)<d.timedelta(minutes=5) else 1)"

CMD ["python", "-m", "newsbot", "run"]
