#!/usr/bin/env bash
docker run -d --name grafana \
  -p 3000:3000 \
  -e GF_SECURITY_ADMIN_PASSWORD=Gr4f4n4!2026 \
  -e GF_SERVER_ROOT_URL=https://grafana.acme.example.com \
  -v /srv/grafana:/var/lib/grafana \
  --restart unless-stopped \
  grafana/grafana:11.3.1
