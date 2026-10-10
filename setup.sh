#!/bin/sh
set -eu

if [ ! -f .env ]; then
  cp .env.example .env
fi

if grep -q 'POSTGRES_PASSWORD=CHANGE_ME' .env; then
  if ! command -v openssl >/dev/null 2>&1; then
    echo "錯誤：找不到 openssl，請先在 .env 手動替換 CHANGE_ME。" >&2
    exit 1
  fi
  db_password="$(openssl rand -hex 24)"
  sed -i.bak "s/POSTGRES_PASSWORD=CHANGE_ME/POSTGRES_PASSWORD=$db_password/; s#postgresql+psycopg://ots:CHANGE_ME@database/ots#postgresql+psycopg://ots:$db_password@database/ots#" .env
  rm -f .env.bak
fi

mkdir -p data/ots data/register data/rabbitmq data/postgres
echo "完成：請編輯 .env 的 OTS_FQDN、PUBLIC_HOST、PUBLIC_BASE_URL，再於 DSM 匯入 docker-compose.yml。"
