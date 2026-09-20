#!/bin/sh
# Prove the server actually works: log every mailbox in over IMAP, then send
# one message through SMTP submission and confirm it arrives.
#
#   bin/verify.sh
#
# Reads the generated passwords from mailbox-passwords.txt and prints only the
# address and the result — never the password itself.
#
# Run it on the mail server. It connects to 127.0.0.1, so it also works before
# DNS points at the host, and it does not care whether the certificate is the
# real one yet.

set -eu

ROOT=$(unset CDPATH; cd -- "$(dirname -- "$0")/.." && pwd)
PASSWORDS="$ROOT/mailbox-passwords.txt"

[ -r "$PASSWORDS" ] || {
	echo "verify: cannot read $PASSWORDS — run bin/setup.sh first" >&2
	exit 1
}
command -v python3 >/dev/null || { echo "verify: python3 is not installed" >&2; exit 1; }

exec python3 - "$PASSWORDS" <<'PY'
import imaplib
import smtplib
import ssl
import sys
import time
import uuid

context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE

HOST = "127.0.0.1"
IMAP_PORT = 993
SMTP_PORT = 465

boxes = []
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and len(line.split()) == 2:
        address, password = line.split()
        boxes.append((address, password))

if not boxes:
    sys.exit("verify: no mailboxes listed in the password file")

failed = 0
print("IMAP sign-in")
for address, password in boxes:
    try:
        session = imaplib.IMAP4_SSL(HOST, IMAP_PORT, ssl_context=context)
        session.login(address, password)
        session.select("INBOX")
        session.logout()
        print("  %-36s OK" % address)
    except Exception as error:
        print("  %-36s FAIL (%s)" % (address, error))
        failed += 1

address, password = boxes[0]
tag = "verify-" + uuid.uuid4().hex[:8]
print("\nSMTP submission as %s" % address)
try:
    server = smtplib.SMTP_SSL(HOST, SMTP_PORT, context=context, timeout=30)
    server.login(address, password)
    server.sendmail(address, [address],
                    "From: %s\r\nTo: %s\r\nSubject: %s\r\n\r\nself test\r\n"
                    % (address, address, tag))
    server.quit()
    print("  accepted for delivery")
except Exception as error:
    print("  FAIL (%s)" % error)
    failed += 1
    tag = None

if tag:
    print("\nDelivery back into the mailbox")
    found = False
    for _ in range(10):
        time.sleep(3)
        session = imaplib.IMAP4_SSL(HOST, IMAP_PORT, ssl_context=context)
        session.login(address, password)
        session.select("INBOX")
        ids = session.search(None, "HEADER", "Subject", tag)[1][0].split()
        for message_id in ids:
            session.store(message_id, "+FLAGS", r"\Deleted")
        if ids:
            session.expunge()
            found = True
        session.logout()
        if found:
            break
    print("  %s" % ("delivered (test message removed)" if found
                    else "NOT delivered within 30s"))
    failed += 0 if found else 1

print()
if failed:
    sys.exit("%d check(s) failed" % failed)
print("all checks passed")
PY
