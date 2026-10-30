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
# It also checks the webmail protection: SnappyMail's settings are rewritten
# in a live container, Stalwart's ban exemption is created, and the fail2ban
# filter is run against sample log lines inside a container.
#
# Requirements: Linux, Docker, python3. Nothing is published on a host port
# and nothing outside Docker is touched (fail2ban is not installed or
# configured on the host here).
#
# It refuses to run if a `stalwart` or `snappymail` container, a `mail_net`
# network or a `stalwart_data` volume already exists, so it can never disturb
# a real deployment on the same host.

set -eu

ROOT=$(unset CDPATH; cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE=${STALWART_IMAGE:-stalwartlabs/stalwart:latest}
SNAPPYMAIL_IMAGE=${SNAPPYMAIL_IMAGE:-djmaze/snappymail:latest}
FAIL2BAN_IMAGE=${FAIL2BAN_IMAGE:-crazymax/fail2ban:1.1.0}
# Must match MAIL_SUBNET, MAIL_DYNAMIC_RANGE and SNAPPYMAIL_IP in bin/apply-config.py.
SUBNET=172.16.89.0/24
DYNAMIC_RANGE=172.16.89.128/25
SNAPPYMAIL_IP=172.16.89.10
ADMIN_PASSWORD='integration-Kettle-9-rasp-Violin'
HOSTNAME_UNDER_TEST=mx.example.com

command -v docker >/dev/null || { echo "integration: docker is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "integration: python3 is required" >&2; exit 1; }

if docker ps -a --format '{{.Names}}' | grep -qx stalwart; then
	echo "integration: a container named 'stalwart' already exists." >&2
	echo "             Refusing to run so a live deployment is not disturbed." >&2
	exit 1
fi
if docker ps -a --format '{{.Names}}' | grep -qx snappymail; then
	echo "integration: a container named 'snappymail' already exists." >&2
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
	docker rm -f stalwart snappymail >/dev/null 2>&1 || true
	docker volume rm stalwart_data >/dev/null 2>&1 || true
	docker network rm mail_net >/dev/null 2>&1 || true
	# SnappyMail's data is created by root inside the container.
	rm -rf "$WORK" 2>/dev/null || docker run --rm -v "$WORK:/work" python:3-alpine \
		sh -c 'rm -rf /work/* /work/.[!.]*' >/dev/null 2>&1 || true
	rm -rf "$WORK" 2>/dev/null || true
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

echo "== 1/11 render"
python3 "$WORK/bin/apply-config.py" render > "$WORK/render.out"
[ -f "$WORK/compose/.env" ] || fail ".env was not written"
[ -f "$WORK/compose/Caddyfile" ] || fail "Caddyfile was not written"
expect "STALWART_RECOVERY_ADMIN=admin:$ADMIN_PASSWORD" "$WORK/compose/.env" \
	"admin credential in .env"
expect "$HOSTNAME_UNDER_TEST {" "$WORK/compose/Caddyfile" "mail host block"
expect "webmail.example.com {" "$WORK/compose/Caddyfile" "webmail block"
expect "header_up -Client-IP" "$WORK/compose/Caddyfile" "Client-IP header stripped"
expect "wrote compose/fail2ban/jail.d/snappymail.local" "$WORK/render.out" \
	"jail rendered"
expect "logpath = .*snappymail_data.*auth.log" \
	"$WORK/compose/fail2ban/jail.d/snappymail.local" "jail points at the auth log"

echo
echo "== 2/11 the generated Caddyfile is valid"
docker run --rm -v "$WORK/compose/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 \
	caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
	> "$WORK/caddy.out" 2>&1 || { cat "$WORK/caddy.out"; fail "caddy validate"; }
expect "Valid configuration" "$WORK/caddy.out" "caddy validate"

echo
echo "== 3/11 the compose file is valid"
docker compose -f "$WORK/compose/docker-compose.yml" --env-file "$WORK/compose/.env" \
	config --quiet || fail "docker compose config"
echo "   ok: docker compose config"

echo
echo "== 4/11 starting a throwaway Stalwart ($IMAGE)"
docker network create --subnet "$SUBNET" --ip-range "$DYNAMIC_RANGE" mail_net >/dev/null
docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{.IPRange}}{{end}}' mail_net \
	> "$WORK/net.out"
expect "$SUBNET $DYNAMIC_RANGE" "$WORK/net.out" "mail_net subnet and dynamic range"
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
echo "== 5/11 applying the configuration"
python3 "$WORK/bin/apply-config.py" apply | tee "$WORK/apply1.out"
expect "example.com  *created" "$WORK/apply1.out" "domain created"
expect "info@example.com  *created" "$WORK/apply1.out" "mailbox created"
expect "2 alias(es) applied" "$WORK/apply1.out" "aliases applied"
expect "3 new password(s)" "$WORK/apply1.out" "passwords recorded"
[ -f "$WORK/mailbox-passwords.txt" ] || fail "password file not written"

echo
echo "== 6/11 re-applying is idempotent and keeps passwords"
cp "$WORK/mailbox-passwords.txt" "$WORK/passwords.before"
python3 "$WORK/bin/apply-config.py" apply | tee "$WORK/apply2.out"
expect "info@example.com  *exists" "$WORK/apply2.out" "mailbox left alone"
expect "password kept" "$WORK/apply2.out" "password not rotated"
grep -q "new password" "$WORK/apply2.out" && fail "a password was rotated on re-run"
cmp -s "$WORK/passwords.before" "$WORK/mailbox-passwords.txt" \
	|| fail "the password file changed on re-run"
echo "   ok: password file unchanged"

echo
echo "== 7/11 DNS records carry the configured hostname"
sh "$WORK/bin/show-dns.sh" example.com > "$WORK/dns.out"
expect "example.com. IN MX 10 $HOSTNAME_UNDER_TEST." "$WORK/dns.out" "MX record"
expect "example.com. IN TXT \"v=spf1 mx -all\"" "$WORK/dns.out" "SPF record"
expect "_domainkey.example.com. IN TXT" "$WORK/dns.out" "DKIM record"
expect "_dmarc.example.com. IN TXT" "$WORK/dns.out" "DMARC record"
expect "_submissions._tcp.example.com. IN SRV 0 1 465 $HOSTNAME_UNDER_TEST." \
	"$WORK/dns.out" "submission SRV record"

echo
echo "== 8/11 IMAP, SMTP, and delivery to an alias"
# Sharing the server's network namespace makes 127.0.0.1 the mail server, so
# nothing has to be published on a host port.
docker run --rm --network container:stalwart -v "$WORK:/work" -w /work \
	python:3-alpine sh -c 'sh bin/verify.sh && python3 tests/alias-probe.py' \
	> "$WORK/verify.out" 2>&1 || { cat "$WORK/verify.out"; fail "verify.sh"; }
cat "$WORK/verify.out"
expect "all checks passed" "$WORK/verify.out" "IMAP sign-in, submission, delivery"
expect "alias delivery: OK" "$WORK/verify.out" "delivery to an alias address"

echo
echo "== 9/11 SnappyMail settings are rewritten in a live container"
mkdir -p "$WORK/compose/snappymail_data"
docker run -d --name snappymail --network mail_net --ip "$SNAPPYMAIL_IP" \
	-v "$WORK/compose/snappymail_data:/var/lib/snappymail" \
	"$SNAPPYMAIL_IMAGE" >/dev/null
INI="$WORK/compose/snappymail_data/_data_/_default_/configs/application.ini"
waited=0
while [ "$waited" -lt 90 ]; do
	[ -f "$INI" ] && break
	sleep 2
	waited=$((waited + 2))
done
[ -f "$INI" ] || { docker logs snappymail | tail -20; fail "application.ini not created in ${waited}s"; }
echo "   ok: application.ini exists after ${waited}s"
# The container owns the file, so the command runs as root, as setup.sh does.
webmail() {
	docker run --rm -v "$WORK:/work" -w /work python:3-alpine \
		python3 bin/apply-config.py webmail
}
webmail | tee "$WORK/webmail1.out"
expect "admin panel key generated" "$WORK/webmail1.out" "admin key generated"
expect "client IP taken from the proxy" "$WORK/webmail1.out" "proxy client IP enabled"
[ -s "$WORK/snappymail-admin-key.txt" ] || fail "admin key file not written"
KEY=$(cat "$WORK/snappymail-admin-key.txt")
grep -q -- "$KEY" "$WORK/webmail1.out" && fail "the admin key was printed"
docker run --rm -v "$WORK:/work" python:3-alpine cat \
	/work/compose/snappymail_data/_data_/_default_/configs/application.ini \
	> "$WORK/application.ini.1"
expect "key = \"$KEY\"" "$WORK/application.ini.1" "admin panel key in application.ini"
expect "http_client_ip_check_proxy = On" "$WORK/application.ini.1" \
	"proxy client IP in application.ini"
# fail2ban refuses the jail while its log file is missing.
docker run --rm -v "$WORK:/work" -w /work/compose/snappymail_data/_data_/_default_ \
	python:3-alpine stat -c '%u:%g' configs/application.ini logs/auth.log \
	> "$WORK/owners.out" 2>&1 || { cat "$WORK/owners.out"; fail "auth.log not created"; }
[ "$(sort -u "$WORK/owners.out" | wc -l)" -eq 1 ] \
	|| { cat "$WORK/owners.out"; fail "auth.log not owned like application.ini"; }
docker exec -u www-data snappymail test -w \
	/var/lib/snappymail/_data_/_default_/logs/auth.log \
	|| fail "auth.log not writable by SnappyMail"
echo "   ok: auth.log exists, owned like application.ini, writable by SnappyMail"
webmail | tee "$WORK/webmail2.out"
expect "admin panel key kept" "$WORK/webmail2.out" "admin key kept on re-run"
docker run --rm -v "$WORK:/work" python:3-alpine cat \
	/work/compose/snappymail_data/_data_/_default_/configs/application.ini \
	> "$WORK/application.ini.2"
cmp -s "$WORK/application.ini.1" "$WORK/application.ini.2" \
	|| fail "application.ini changed on re-run"
echo "   ok: application.ini unchanged on re-run"
# The rewritten file must still be readable by the web server.
docker exec snappymail wget -q -O /dev/null http://127.0.0.1:8888/ \
	|| fail "SnappyMail does not answer after its settings were rewritten"
echo "   ok: SnappyMail answers HTTP after the rewrite"

echo
echo "== 10/11 Stalwart exempts the webmail container from its ban"
python3 "$WORK/bin/apply-config.py" allow-webmail | tee "$WORK/allow1.out"
expect "allowip  $SNAPPYMAIL_IP  *created" "$WORK/allow1.out" "exemption created"
python3 "$WORK/bin/apply-config.py" allow-webmail | tee "$WORK/allow2.out"
expect "allowip  $SNAPPYMAIL_IP  *exists" "$WORK/allow2.out" "no duplicate on re-run"
sh "$WORK/bin/sw-cli.sh" query AllowedIp > "$WORK/allowed.out"
echo "   query AllowedIp output:"
sed 's/^/     | /' "$WORK/allowed.out"
[ "$(grep -c -- "$SNAPPYMAIL_IP" "$WORK/allowed.out")" -eq 1 ] \
	|| fail "expected exactly one AllowedIp row for $SNAPPYMAIL_IP"
echo "   ok: one AllowedIp row"

echo
echo "== 11/11 the fail2ban filter matches SnappyMail's log lines"
cat > "$WORK/auth.log" <<'LOG'
[2026-10-30 12:00:00] Auth failed: ip=203.0.113.7 user=info@example.com host=stalwart port=993
[2026-10-30 12:00:05] Admin Auth failed: ip=203.0.113.8 user=admin
[2026-10-30 12:00:09] Auth success: ip=203.0.113.9 user=info@example.com
LOG
docker run --rm --entrypoint fail2ban-regex \
	-v "$WORK/auth.log:/test/auth.log:ro" \
	-v "$WORK/compose/fail2ban/filter.d/snappymail.conf:/test/snappymail.conf:ro" \
	"$FAIL2BAN_IMAGE" /test/auth.log /test/snappymail.conf > "$WORK/f2b.out" 2>&1 \
	|| { cat "$WORK/f2b.out"; fail "fail2ban-regex"; }
expect "Lines: 3 lines, 0 ignored, 2 matched, 1 missed" "$WORK/f2b.out" \
	"two failures match, the success line does not"
