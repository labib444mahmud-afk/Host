FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_ROOT=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm tini ca-certificates \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py README.md env.example gitignore railway.toml ./
COPY start.sh ./start.sh
RUN chmod +x ./start.sh

# Persistent user deployments/database/logs are mounted at /data by Railway.
RUN mkdir -p /data && chmod 777 /data

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
