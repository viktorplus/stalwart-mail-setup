#!/bin/sh
# Run stalwart-cli against the local Stalwart instance.
#
# The administrator password is read from mailserver.conf inside this script,
# so it never appears in a command line, in shell history, or in any log.
# The value is split without eval or sourcing, so a password containing shell
# metacharacters cannot be executed.
#
# The CLI talks to the container over the internal Docker network on plain
# HTTP, which avoids the TLS certificate entirely.
#
# Usage:  bin/sw-cli.sh query Domain
#         bin/sw-cli.sh describe Account
#         bin/sw-cli.sh apply --file plan.json      (plans/ is mounted as /work)

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONF="$ROOT/mailserver.conf"
PLANS="$ROOT/plans"

[ -r "$CONF" ] || { echo "sw-cli: cannot read $CONF" >&2; exit 1; }

line=$(grep -m1 '^[[:space:]]*admin_password[[:space:]]*=' "$CONF") || {
	echo "sw-cli: admin_password not set in $CONF" >&2
	exit 1
}
password=${line#*=}
# strip surrounding whitespace and optional quotes
password=$(printf '%s' "$password" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/^"\(.*\)"$/\1/; s/^'"'"'\(.*\)'"'"'$/\1/')
[ -n "$password" ] || { echo "sw-cli: admin_password is empty in $CONF" >&2; exit 1; }

mkdir -p "$PLANS"

STALWART_USER=admin
STALWART_PASSWORD=$password
export STALWART_USER STALWART_PASSWORD

exec docker run --rm -i --network mail_net \
	-e STALWART_URL=http://stalwart:8080 \
	-e STALWART_USER \
	-e STALWART_PASSWORD \
	-v "$PLANS:/work" -w /work \
	ghcr.io/stalwartlabs/cli:latest "$@"
