FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 ledgerguard

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=ledgerguard:ledgerguard . .

RUN mkdir -p /var/lib/ledgerguard/media /app/staticfiles \
    && chown -R ledgerguard:ledgerguard /var/lib/ledgerguard /app/staticfiles

USER ledgerguard
# collectstatic needs settings to import; a throwaway key is used only at build time.
RUN DJANGO_SECRET_KEY=build-only DATABASE_URL=sqlite:////tmp/build.db python manage.py collectstatic --noinput -v0

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/')"
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-3} --timeout 120 --access-logfile -"]
