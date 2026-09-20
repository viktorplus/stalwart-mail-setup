#!/bin/sh
# Copy the Let's Encrypt certificate Caddy holds into Stalwart's database.
#
# Why this is needed: Stalwart keeps the TLS certificate as inline PEM in its
# database, because the container (uid 2000) cannot read Caddy's certificate
# directory (mode 0700, owned by root), so a file-path certificate is not
# usable. Inline PEM does NOT follow Caddy's ACME renewal — without this step
# the certificate served on SMTP and IMAP silently goes stale and expires.
# Caddy renews the files on disk; this copies the renewed pair into Stalwart.
#
# Run it as root, the only user who can read Caddy's certificate directory.
#
# Idempotent and cheap: it fingerprints the certificate and does nothing
# unless it changed, so it is safe to run daily from cron and safe to run by
# hand.
#
#   bin/reimport-cert.sh          act only if the certificate changed
#   bin/reimport-cert.sh --force  re-import and restart regardless
#   bin/reimport-cert.sh --seed   record the current certificate, change nothing
#
# Install the renewal hook (as root, once Caddy has issued the certificate):
#
#   bin/reimport-cert.sh --seed
#   ( crontab -l 2>/dev/null
#     echo "30 3 * * * /path/to/bin/reimport-cert.sh >> /var/log/reimport-cert.log 2>&1"
#   ) | crontab -

set -eu

ROOT=$(unset CDPATH; cd -- "$(dirname -- "$0")/.." && pwd)
CONF="$ROOT/mailserver.conf"
CLI="$ROOT/bin/sw-cli.sh"
CERTS="$ROOT/compose/caddy_data/caddy/certificates"
MARK="$ROOT/.cert-fingerprint"
CONTAINER=stalwart

MODE=${1:-run}

[ -r "$CONF" ] || { echo "reimport-cert: cannot read $CONF" >&2; exit 1; }
DOMAIN=$(sed -n 's/^[[:space:]]*hostname[[:space:]]*=[[:space:]]*//p' "$CONF" \
	| head -1 | sed 's/[[:space:]]*$//; s/^"\(.*\)"$/\1/')
[ -n "$DOMAIN" ] || { echo "reimport-cert: hostname not set in $CONF" >&2; exit 1; }

CRT=$(find "$CERTS" -type f -path "*/$DOMAIN/$DOMAIN.crt" 2>/dev/null | head -1)
if [ -z "$CRT" ] || [ ! -r "$CRT" ]; then
	echo "reimport-cert: no certificate for $DOMAIN yet" >&2
	exit 1
fi
KEY=$(dirname "$CRT")/"$DOMAIN".key
[ -r "$KEY" ] || { echo "reimport-cert: key file not found ($KEY)" >&2; exit 1; }

FP=$(openssl x509 -in "$CRT" -noout -fingerprint -sha256 | sed 's/.*=//')

if [ "$MODE" = "--seed" ]; then
	umask 077
	printf '%s' "$FP" > "$MARK"
	echo "reimport-cert: recorded $FP; the next run is a no-op until Caddy renews"
	exit 0
fi

if [ "$MODE" != "--force" ] && [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$FP" ]; then
	echo "reimport-cert: certificate unchanged ($FP), nothing to do"
	exit 0
fi

echo "reimport-cert: importing certificate $FP for $DOMAIN"
# Build the upsert plan and stream it straight into the CLI over stdin, so the
# private key never lands on disk. matchOn subjectAlternativeNames updates the
# existing row instead of creating a duplicate.
python3 - "$CRT" "$KEY" "$DOMAIN" <<'PY' | "$CLI" apply --stdin
import json, sys
crt = open(sys.argv[1]).read()
key = open(sys.argv[2]).read()
domain = sys.argv[3]
print(json.dumps({
    "@type": "upsert",
    "object": "Certificate",
    "matchOn": ["subjectAlternativeNames"],
    "value": {
        "certificate-acme": {
            "certificate": {"@type": "Text", "value": crt},
            "privateKey": {"@type": "Text", "secret": key},
            "subjectAlternativeNames": {domain: True},
        }
    },
}))
PY

# Stalwart caches the TLS configuration; a restart makes it serve the new
# certificate.
docker restart "$CONTAINER" >/dev/null
umask 077
printf '%s' "$FP" > "$MARK"
echo "reimport-cert: done — imported and restarted $CONTAINER"
