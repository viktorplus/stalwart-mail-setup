#!/bin/sh
# Print the DNS records every configured domain needs.
#
#   bin/show-dns.sh              all domains
#   bin/show-dns.sh example.com  one domain
#
# The record strings come from Stalwart itself, which generates them from the
# live configuration and the DKIM keys it holds. Re-run this after adding a
# domain or rotating DKIM keys.
#
# Apply them in two stages, not all at once — see docs/dns.md. The records
# that move mail (MX, SPF, DMARC) belong to the second stage, one domain at a
# time.

set -eu

ROOT=$(unset CDPATH; cd -- "$(dirname -- "$0")/.." && pwd)
CLI="$ROOT/bin/sw-cli.sh"
WANTED=${1:-}

"$CLI" query Domain | awk 'NR > 1 { print $1, $2 }' | while read -r id name; do
	[ -z "$WANTED" ] || [ "$WANTED" = "$name" ] || continue
	echo "########################################"
	echo "# $name"
	echo "########################################"
	"$CLI" get Domain "$id" --json | python3 -c \
		'import json, sys; print(json.load(sys.stdin)["dnsZoneFile"])'
	echo
done
