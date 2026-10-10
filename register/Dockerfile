FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py admin_auth.py entrypoint.sh ./
COPY templates ./templates
COPY static ./static
RUN chmod 0755 /app/entrypoint.sh && mkdir -p /data/packages

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]

