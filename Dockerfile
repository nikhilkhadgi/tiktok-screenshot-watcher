FROM python:3.12-slim

# The watch directory and state directory are bind mounts from the host. On
# Linux the container user's numeric UID has to match the host owner or the
# mounts are unreadable/unwritable; Docker Desktop maps ownership itself and
# ignores these.
ARG PUID=1000
ARG PGID=1000

RUN groupadd -g "${PGID}" watcher \
 && useradd -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin watcher \
 && mkdir -p /watch /state \
 && chown watcher:watcher /watch /state

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py ./

ENV PYTHONUNBUFFERED=1 \
    WATCH_DIRECTORY=/watch \
    STATE_DIR=/state

USER watcher

CMD ["python", "watcher.py"]
