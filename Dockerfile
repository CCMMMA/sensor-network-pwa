FROM python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 portal \
    && useradd --uid 10001 --gid portal --create-home --home-dir /home/portal portal

COPY requirements-container.txt /app/requirements-container.txt
RUN pip install --no-cache-dir --upgrade "setuptools==84.2.1" \
    && pip install --no-cache-dir -r /app/requirements-container.txt

COPY --chown=portal:portal main.py webapp_wsgi.py /app/
COPY --chown=portal:portal sensor_network_pwa /app/sensor_network_pwa
COPY --chown=portal:portal docs /app/docs

USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()"]

CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:8080", "--worker-tmp-dir", "/tmp", "webapp_wsgi:app"]
