#!/bin/bash
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE ROLE lexloom_app    LOGIN PASSWORD '${APP_DB_PASSWORD}';
  CREATE ROLE lexloom_worker LOGIN PASSWORD '${WORKER_DB_PASSWORD}';
  GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO lexloom_app, lexloom_worker;
  GRANT USAGE ON SCHEMA public TO lexloom_app, lexloom_worker;
EOSQL
