FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# postgresql-client provides pg_dump/pg_restore for the verified backup command
# (psycopg[binary] bundles libpq). Set INSTALL_PG_CLIENT=0 only if backups run elsewhere.
ARG INSTALL_PG_CLIENT=1
RUN if [ "$INSTALL_PG_CLIENT" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends postgresql-client \
      && rm -rf /var/lib/apt/lists/*; \
    fi \
    && useradd --create-home --uid 10001 ledgerguard

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=ledgerguard:ledgerguard . .

RUN mkdir -p /var/lib/ledgerguard/media /var/lib/ledgerguard/backups /var/lib/ledgerguard/ingest /app/staticfiles \
    && chown -R ledgerguard:ledgerguard /var/lib/ledgerguard /app/staticfiles

USER ledgerguard
# collectstatic needs settings to import; a throwaway key is used only at build time.
RUN DJANGO_SECRET_KEY=build-only DATABASE_URL=sqlite:////tmp/build.db python manage.py collectstatic --noinput -v0

ENV LEDGERGUARD_MEDIA_ROOT=/var/lib/ledgerguard/media \
    LEDGERGUARD_BACKUP_DIR=/var/lib/ledgerguard/backups \
    LEDGERGUARD_INGEST_ROOT=/var/lib/ledgerguard/ingest

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/')"
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["sh", "-c", "gunicorn config.wsgi:application --config deploy/gunicorn.conf.py"]
