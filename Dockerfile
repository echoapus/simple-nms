FROM python:3.11-slim-bookworm

LABEL maintainer="Simple NMS"
LABEL description="Lightweight Network Management System"

# Create non-root user
RUN groupadd -r simplenms && useradd -r -g simplenms -d /app simplenms

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY database.py web_app.py main.py cleanup.py metrics.py ./
COPY collectors/ ./collectors/
COPY static/ ./static/
COPY config.json ./config.json

# Create data directory
RUN mkdir -p /app/data && chown -R simplenms:simplenms /app

USER simplenms

# Expose ports: HTTP(80), Syslog(514/udp), SNMP Trap(162/udp)
EXPOSE 80/tcp
EXPOSE 514/udp
EXPOSE 162/udp

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:80/health', timeout=3)" || exit 1

VOLUME ["/app/data"]

ENTRYPOINT ["python3", "main.py"]
CMD ["config.json"]
