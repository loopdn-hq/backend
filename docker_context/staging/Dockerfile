# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=loopd.settings

WORKDIR /app

# Dependencies first, so a code change does not invalidate the pip layer.
# No compiler or -dev packages are needed: psycopg-binary bundles libpq and
# Pillow bundles its codecs, both as manylinux wheels - including for arm64,
# which is what makes the Graviton build cheap to produce under emulation.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Ahead of the source copy so editing application code does not rebuild it.
COPY docker_context/staging/django-entrypoint.sh /usr/local/bin/django-entrypoint.sh
RUN chmod +x /usr/local/bin/django-entrypoint.sh

COPY . .

# Avatars and chat attachments land in S3 in staging; this stays for the local
# FileSystemStorage fallback when AWS_STORAGE_BUCKET_NAME is unset.
RUN mkdir -p /app/var/media /app/staticfiles

# Static files are collected at build time, not on start-up: it keeps container
# start fast and makes the image self-contained.
#
# DEBUG=False is forced here on purpose - it is what selects whitenoise's
# CompressedManifestStaticFilesStorage, which writes the staticfiles.json
# manifest. Without the manifest, every {% static %} tag raises at runtime. The
# SECRET_KEY is a build-time placeholder; the real one arrives from SSM.
RUN DEBUG=False \
    SECRET_KEY=build-time-placeholder-not-used-at-runtime \
    ALLOWED_HOSTS='*' \
    python manage.py collectstatic --noinput --clear

EXPOSE 8000

# The entrypoint waits for the database and then execs daphne. Passing any
# command overrides that, which is how the CI migration task and any future
# Celery service reuse this image unchanged.
ENTRYPOINT ["/usr/local/bin/django-entrypoint.sh"]
