#!/usr/bin/env bash
# (Re)build the Meridian Supply source database and its read-only user on a local MySQL.
#
#   eval/meridian/load.sh
#
#   MERIDIAN_INJECT=1 eval/meridian/load.sh   # the prompt-injection variant: meridian_inject,
#                                             # user meridian_inject_ro, same password
#
# Uses the local MySQL root account (Homebrew default: socket auth, no password). Override
# with MYSQL_ADMIN_ARGS, e.g. MYSQL_ADMIN_ARGS="-uadmin -p".
#
# The read-only user's password is taken from MERIDIAN_RO_PASSWORD, or from
# KLEANDBM_DEV_MERIDIAN_PASSWORD in backend/.env, or generated and appended there.
# It is never printed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ENV_FILE="$ROOT/backend/.env"
if [[ -n "${MERIDIAN_INJECT:-}" ]]; then
  DB="${MERIDIAN_DB:-meridian_inject}"; RO_USER="${MERIDIAN_RO_USER:-meridian_inject_ro}"; GEN_ARGS="--inject"
else
  DB="${MERIDIAN_DB:-meridian_ops}"; RO_USER="${MERIDIAN_RO_USER:-meridian_ro}"; GEN_ARGS=""
fi
ADMIN_ARGS="${MYSQL_ADMIN_ARGS:--uroot}"
PYTHON="${PYTHON:-conda run -n kleandbm-agentic python}"

if [[ -z "${MERIDIAN_RO_PASSWORD:-}" && -f "$ENV_FILE" ]]; then
  MERIDIAN_RO_PASSWORD="$(grep -E '^KLEANDBM_DEV_MERIDIAN_PASSWORD=' "$ENV_FILE" | cut -d= -f2- || true)"
fi
if [[ -z "${MERIDIAN_RO_PASSWORD:-}" ]]; then
  MERIDIAN_RO_PASSWORD="$(openssl rand -hex 18)"
  printf '\n# Local read-only password for the Meridian sample source (eval/meridian/load.sh)\nKLEANDBM_DEV_MERIDIAN_PASSWORD=%s\n' \
    "$MERIDIAN_RO_PASSWORD" >> "$ENV_FILE"
  echo "Generated a read-only password and saved it in backend/.env (KLEANDBM_DEV_MERIDIAN_PASSWORD)."
fi

SQL="$(mktemp -t meridian.XXXXXX.sql)"
trap 'rm -f "$SQL"' EXIT
$PYTHON "$HERE/generate.py" $GEN_ARGS > "$SQL"

mysql $ADMIN_ARGS <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS '$RO_USER'@'localhost' IDENTIFIED BY '$MERIDIAN_RO_PASSWORD';
CREATE USER IF NOT EXISTS '$RO_USER'@'127.0.0.1' IDENTIFIED BY '$MERIDIAN_RO_PASSWORD';
ALTER USER '$RO_USER'@'localhost' IDENTIFIED BY '$MERIDIAN_RO_PASSWORD';
ALTER USER '$RO_USER'@'127.0.0.1' IDENTIFIED BY '$MERIDIAN_RO_PASSWORD';
-- Read-only by grant: the MCP server's own checks are a second line of defense, not the only one.
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '$RO_USER'@'localhost', '$RO_USER'@'127.0.0.1';
GRANT SELECT, SHOW VIEW ON \`$DB\`.* TO '$RO_USER'@'localhost', '$RO_USER'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

mysql $ADMIN_ARGS "$DB" < "$SQL"
echo "Loaded $DB; read-only user $RO_USER has SELECT and SHOW VIEW only."
