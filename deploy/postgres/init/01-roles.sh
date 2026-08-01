#!/bin/bash
set -euo pipefail

# The passwords reach psql as data, never as SQL text.
#
# `-v name=value` hands psql a variable and `:'name'` expands it as a properly
# quoted SQL literal, doubling any apostrophe inside it. Interpolating
# "PASSWORD '${APP_DB_PASSWORD}'" directly — which is what this did — ends the
# literal on the first apostrophe, so a password containing one turns the
# bootstrap into a syntax error while the operator is looking at a runbook that
# told them to generate a random value. The heredoc delimiter is quoted for the
# same reason: nothing below is expanded by the shell either, so a `$` in a
# password cannot become a variable reference.
#
# Verified against a password containing ' @ / + and ".
#
# This does not make every random password safe end to end: `DATABASE_URL` in
# docker-compose.yml interpolates the same value into a URL, where '@' and '/'
# are structural whatever this script does. deploy/README.md therefore asks for
# hex, which is safe in both places by construction.
psql -v ON_ERROR_STOP=1 \
     -v app_password="$APP_DB_PASSWORD" \
     -v worker_password="$WORKER_DB_PASSWORD" \
     -v db="$POSTGRES_DB" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'EOSQL'
  CREATE ROLE lexloom_app    LOGIN PASSWORD :'app_password';
  CREATE ROLE lexloom_worker LOGIN PASSWORD :'worker_password';
  GRANT CONNECT ON DATABASE :"db" TO lexloom_app, lexloom_worker;
  GRANT USAGE ON SCHEMA public TO lexloom_app, lexloom_worker;
EOSQL
