FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt && apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*
COPY app ./app
COPY static ./static
COPY entrypoint.sh /entrypoint.sh
# Filled in by update.sh, shown under Instellingen → Over (last, so a new version doesn't redo the steps above).
ARG VERSION=""
ENV APP_VERSION=$VERSION

RUN useradd --uid 1000 --create-home dressing && mkdir -p /data && chmod +x /entrypoint.sh
VOLUME /data
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health')"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
