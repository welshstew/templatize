FROM python:3.12.7-slim-bookworm

ARG APP_VERSION=2.4.1
ENV APP_HOME=/srv/app \
    PORT=8000 \
    LOG_LEVEL=info

WORKDIR /srv/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
USER 10001
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "app:create_app()"]
