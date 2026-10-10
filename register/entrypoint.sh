#!/bin/sh
set -eu

secret_file=/data/.register-secrets
if [ ! -s "$secret_file" ]; then
  umask 077
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
  FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  printf 'SECRET_KEY=%s\nFERNET_KEY=%s\n' "$SECRET_KEY" "$FERNET_KEY" > "$secret_file"
fi

set -a
. "$secret_file"
set +a

exec gunicorn --bind 0.0.0.0:8080 --workers 2 --threads 4 --timeout 45 app:app

