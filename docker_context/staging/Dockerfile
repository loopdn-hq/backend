# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not invalidate the pip layer.
# No compiler or -dev packages are needed: psycopg-binary bundles libpq and
# Pillow bundles its codecs, both as manylinux wheels.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Avatars and chat attachments are written here.
RUN mkdir -p /app/var/media /app/staticfiles

EXPOSE 8000

# Daphne, not gunicorn: this app serves websockets, and a WSGI server
# cannot. The compose file overrides this to run migrations first.
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "loopd.asgi:application"]
