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

ROOT=$(unset CDPATH; cd -- "$(dirname -- "$0")/.." && pwd)
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

echo "== 1/6  rendering compose/.env and compose/Caddyfile"
python3 "$ROOT/bin/apply-config.py" render

# Webmail (and its brute-force protection) only when a webmail host is set.
if grep -qE '^[[:space:]]*webmail_host[[:space:]]*=[[:space:]]*[^[:space:]#]' "$CONF"; then
	WEBMAIL=1
	set -- --profile webmail
else
	WEBMAIL=0
	set --
fi

# mail_net must carry the address plan the webmail protection relies on. An
# older install has the network with a Docker-chosen subnet; it cannot be
# changed in place, so stop before touching anything.
if subnet=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' mail_net 2>/dev/null); then
	case " $subnet" in
	*" 172.16.89.0/24 "*) ;;
	*)
		echo "setup: the mail_net network uses the subnet ${subnet% }, not 172.16.89.0/24." >&2
		echo "       It has to be recreated once. This stops the stack briefly; the data" >&2
		echo "       in the stalwart_data volume and in compose/ is kept. Run:" >&2
		echo "sudo docker compose -f '$COMPOSE' --profile webmail down && sudo '$ROOT/bin/setup.sh'" >&2
		exit 1
		;;
	esac
fi

echo
echo "== 2/6  starting the stack"
docker compose -f "$COMPOSE" "$@" up -d

echo
echo "== 3/6  waiting for Stalwart to become healthy"
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
echo "== 4/6  applying domains and mailboxes"
python3 "$ROOT/bin/apply-config.py" apply

echo
echo "== 5/6  webmail: SnappyMail settings, fail2ban, Stalwart exemption"
if [ "$WEBMAIL" = 1 ]; then
	python3 "$ROOT/bin/apply-config.py" webmail
	if ! command -v fail2ban-client >/dev/null; then
		command -v apt-get >/dev/null || {
			echo "setup: fail2ban is not installed and apt-get is not available." >&2
			echo "       Install fail2ban with your package manager, then re-run bin/setup.sh." >&2
			exit 1
		}
		apt-get update
		apt-get install -y fail2ban
	fi
	install -m 644 "$ROOT/compose/fail2ban/filter.d/snappymail.conf" /etc/fail2ban/filter.d/snappymail.conf
	install -m 644 "$ROOT/compose/fail2ban/jail.d/snappymail.local" /etc/fail2ban/jail.d/snappymail.local
	systemctl enable --now fail2ban
	fail2ban-client reload
	fail2ban-client status snappymail >/dev/null || {
		echo "setup: the fail2ban jail 'snappymail' is not active." >&2
		echo "       Not exempting the webmail from Stalwart's ban without it." >&2
		echo "       Check: fail2ban-client status; journalctl -u fail2ban" >&2
		exit 1
	}
	echo "     fail2ban jail snappymail active"
	python3 "$ROOT/bin/apply-config.py" allow-webmail
else
	echo "     skipped (no webmail_host)"
fi

echo
echo "== 6/6  importing the TLS certificate, if Caddy has issued one yet"
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
if [ "$WEBMAIL" = 1 ]; then
	echo "The webmail admin panel key is in snappymail-admin-key.txt (mode 600)."
fi
