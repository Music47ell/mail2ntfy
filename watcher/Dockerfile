FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates passwd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system watcher \
    && useradd --system --gid watcher --uid 1000 \
        --home-dir /app --shell /usr/sbin/nologin watcher

WORKDIR /app
COPY watcher.py /app/watcher.py
RUN chown -R watcher:watcher /app

USER watcher
CMD ["python", "-u", "/app/watcher.py"]
