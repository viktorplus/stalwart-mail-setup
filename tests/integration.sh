#!/bin/sh
# End-to-end test against a real, throwaway Stalwart instance.
#
#   tests/integration.sh
#
# What it is for: the scripts depend on field shapes of the Stalwart API that
# are not documented anywhere — `mailExchangers`, the `credentials` list, the
# alias map, the `Account/User` variant. When upstream changes them, nothing
# here fails loudly; mailboxes just quietly come out wrong. Running this
# against `stalwart:latest` is the only way to notice.
#
# Requirements: Linux, Docker, python3. Nothing is published on a host port
# and nothing outside Docker is touched.
#
# It refuses to run if a `stalwart` container, a `mail_net` network or a
# `stalwart_data` volume already exists, so it can never disturb a real
# deployment on the same host.

set -eu

ROOT=$(unset CDPATH; cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE=${STALWART_IMAGE:-stalwartlabs/stalwart:latest}
ADMIN_PASSWORD='integration-Kettle-9-rasp-Violin'
HOSTNAME_UNDER_TEST=mx.example.com

command -v docker >/dev/null || { echo "integration: docker is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "integration: python3 is required" >&2; exit 1; }

if docker ps -a --format '{{.Names}}' | grep -qx stalwart; then
	echo "integration: a container named 'stalwart' already exists." >&2
	echo "             Refusing to run so a live deployment is not disturbed." >&2
	exit 1
fi
if docker network ls --format '{{.Name}}' | grep -qx mail_net; then
	echo "integration: the 'mail_net' network already exists. Refusing to run." >&2
	exit 1
fi
if docker volume ls --format '{{.Name}}' | grep -qx stalwart_data; then
	echo "integration: the 'stalwart_data' volume already exists. Refusing to run." >&2
	exit 1
fi

WORK=$(mktemp -d)
cleanup() {
	status=$?
	echo
	echo "-- cleaning up"
	docker rm -f stalwart >/dev/null 2>&1 || true
	docker volume rm stalwart_data >/dev/null 2>&1 || true
	docker network rm mail_net >/dev/null 2>&1 || true
	rm -rf "$WORK"
	[ "$status" -eq 0 ] && echo "integration: PASS" || echo "integration: FAIL"
	exit "$status"
}
trap cleanup EXIT INT TERM

# Work on a copy, so a mailserver.conf in the checkout is never touched.
cp -R "$ROOT/bin" "$ROOT/compose" "$ROOT/tests" "$WORK/"
cat > "$WORK/mailserver.conf" <<CONF
hostname       = $HOSTNAME_UNDER_TEST
acme_email     = admin@example.com
admin_password = $ADMIN_PASSWORD
webmail_host   = webmail.example.com

domain  = example.com
domain  = example.net

mailbox = info@example.com   postmaster@example.com, abuse@example.com
mailbox = sales@example.com
mailbox = info@example.net   postmaster@example.net
CONF
chmod 600 "$WORK/mailserver.conf"

fail() { echo "   FAIL: $*" >&2; exit 1; }
expect() { # expect <needle> <haystack-file> <description>
	grep -q -- "$1" "$2" || fail "$3"
	echo "   ok: $3"
}

echo "== 1/8  render"
python3 "$WORK/bin/apply-config.py" render > "$WORK/render.out"
[ -f "$WORK/compose/.env" ] || fail ".env was not written"
[ -f "$WORK/compose/Caddyfile" ] || fail "Caddyfile was not written"
expect "STALWART_RECOVERY_ADMIN=admin:$ADMIN_PASSWORD" "$WORK/compose/.env" \
	"admin credential in .env"
expect "$HOSTNAME_UNDER_TEST {" "$WORK/compose/Caddyfile" "mail host block"
expect "webmail.example.com {" "$WORK/compose/Caddyfile" "webmail block"

echo
echo "== 2/8  the generated Caddyfile is valid"
docker run --rm -v "$WORK/compose/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 \
	caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
	> "$WORK/caddy.out" 2>&1 || { cat "$WORK/caddy.out"; fail "caddy validate"; }
expect "Valid configuration" "$WORK/caddy.out" "caddy validate"

echo
echo "== 3/8  the compose file is valid"
docker compose -f "$WORK/compose/docker-compose.yml" --env-file "$WORK/compose/.env" \
	config --quiet || fail "docker compose config"
echo "   ok: docker compose config"

echo
echo "== 4/8  starting a throwaway Stalwart ($IMAGE)"
docker network create mail_net >/dev/null
docker run -d --name stalwart --network mail_net \
	-e "STALWART_RECOVERY_ADMIN=admin:$ADMIN_PASSWORD" \
	-v "$WORK/compose/stalwart:/etc/stalwart:ro" \
	-v stalwart_data:/var/lib/stalwart \
	"$IMAGE" --config /etc/stalwart/config.json >/dev/null
waited=0
while [ "$waited" -lt 120 ]; do
	state=$(docker inspect -f '{{.State.Health.Status}}' stalwart 2>/dev/null || echo starting)
	[ "$state" = healthy ] && break
	sleep 3
	waited=$((waited + 3))
done
[ "${state:-}" = healthy ] || { docker logs stalwart | tail -20; fail "not healthy in ${waited}s"; }
echo "   ok: healthy after ${waited}s"

echo
echo "== 5/8  applying the configuration"
python3 "$WORK/bin/apply-config.py" apply | tee "$WORK/apply1.out"
expect "example.com  *created" "$WORK/apply1.out" "domain created"
expect "info@example.com  *created" "$WORK/apply1.out" "mailbox created"
expect "2 alias(es) applied" "$WORK/apply1.out" "aliases applied"
expect "3 new password(s)" "$WORK/apply1.out" "passwords recorded"
[ -f "$WORK/mailbox-passwords.txt" ] || fail "password file not written"

echo
echo "== 6/8  re-applying is idempotent and keeps passwords"
cp "$WORK/mailbox-passwords.txt" "$WORK/passwords.before"
python3 "$WORK/bin/apply-config.py" apply | tee "$WORK/apply2.out"
expect "info@example.com  *exists" "$WORK/apply2.out" "mailbox left alone"
expect "password kept" "$WORK/apply2.out" "password not rotated"
grep -q "new password" "$WORK/apply2.out" && fail "a password was rotated on re-run"
cmp -s "$WORK/passwords.before" "$WORK/mailbox-passwords.txt" \
	|| fail "the password file changed on re-run"
echo "   ok: password file unchanged"

echo
echo "== 7/8  DNS records carry the configured hostname"
sh "$WORK/bin/show-dns.sh" example.com > "$WORK/dns.out"
expect "example.com. IN MX 10 $HOSTNAME_UNDER_TEST." "$WORK/dns.out" "MX record"
expect "example.com. IN TXT \"v=spf1 mx -all\"" "$WORK/dns.out" "SPF record"
expect "_domainkey.example.com. IN TXT" "$WORK/dns.out" "DKIM record"
expect "_dmarc.example.com. IN TXT" "$WORK/dns.out" "DMARC record"
expect "_submissions._tcp.example.com. IN SRV 0 1 465 $HOSTNAME_UNDER_TEST." \
	"$WORK/dns.out" "submission SRV record"

echo
echo "== 8/8  IMAP, SMTP, and delivery to an alias"
# Sharing the server's network namespace makes 127.0.0.1 the mail server, so
# nothing has to be published on a host port.
docker run --rm --network container:stalwart -v "$WORK:/work" -w /work \
	python:3-alpine sh -c 'sh bin/verify.sh && python3 tests/alias-probe.py' \
	> "$WORK/verify.out" 2>&1 || { cat "$WORK/verify.out"; fail "verify.sh"; }
cat "$WORK/verify.out"
expect "all checks passed" "$WORK/verify.out" "IMAP sign-in, submission, delivery"
expect "alias delivery: OK" "$WORK/verify.out" "delivery to an alias address"
