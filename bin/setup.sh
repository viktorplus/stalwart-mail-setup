#!/bin/sh
# Bring the mail server up from mailserver.conf, then apply the configuration.
#
#   cp mailserver.conf.example mailserver.conf
#   edit mailserver.conf
#   bin/setup.sh
#
# Safe to re-run after editing mailserver.conf: it applies the difference and
# keeps the password of every mailbox that already exists.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONF="$ROOT/mailserver.conf"
COMPOSE="$ROOT/compose/docker-compose.yml"

[ -r "$CONF" ] || {
	echo "setup: $CONF not found." >&2
	echo "       cp mailserver.conf.example mailserver.conf, then edit it." >&2
	exit 1
}
command -v docker >/dev/null || { echo "setup: docker is not installed" >&2; exit 1; }
command -v python3 >/dev/null || { echo "setup: python3 is not installed" >&2; exit 1; }

chmod 600 "$CONF" 2>/dev/null || true

echo "== 1/5  rendering compose/.env and compose/Caddyfile"
python3 "$ROOT/bin/apply-config.py" render

# Start the webmail container only when a webmail host is configured.
if grep -qE '^[[:space:]]*webmail_host[[:space:]]*=[[:space:]]*[^[:space:]#]' "$CONF"; then
	set -- --profile webmail
else
	set --
fi

echo
echo "== 2/5  starting the stack"
docker compose -f "$COMPOSE" "$@" up -d

echo
echo "== 3/5  waiting for Stalwart to become healthy"
waited=0
while [ "$waited" -lt 120 ]; do
	state=$(docker inspect -f '{{.State.Health.Status}}' stalwart 2>/dev/null || echo starting)
	[ "$state" = healthy ] && break
	sleep 3
	waited=$((waited + 3))
done
[ "${state:-}" = healthy ] || {
	echo "setup: Stalwart did not become healthy in ${waited}s." >&2
	echo "       docker compose -f $COMPOSE logs stalwart" >&2
	exit 1
}
echo "     healthy after ${waited}s"

echo
echo "== 4/5  applying domains and mailboxes"
python3 "$ROOT/bin/apply-config.py" apply

echo
echo "== 5/5  importing the TLS certificate, if Caddy has issued one yet"
if "$ROOT/bin/reimport-cert.sh"; then
	:
else
	echo "     no certificate yet. Caddy needs the hostname's A record to"
	echo "     resolve to this server on ports 80 and 443. Re-run"
	echo "     bin/reimport-cert.sh once DNS has propagated."
fi

echo
echo "Done. Publish the DNS records next:"
echo "  bin/show-dns.sh"
echo "Mailbox passwords are in mailbox-passwords.txt (mode 600)."
