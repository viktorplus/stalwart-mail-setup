#!/usr/bin/env python3
"""Confirm that mail addressed to an alias reaches the mailbox behind it.

Part of tests/integration.sh, which runs it inside a container that shares the
mail server's network namespace. It is not meant to be run on its own.

The alias map is the field shape most likely to break silently on a Stalwart
upgrade: a wrong encoding is accepted, the mailbox is created, and only the
alias stops delivering.
"""

import imaplib
import smtplib
import ssl
import sys
import time
import uuid

HOST = "127.0.0.1"
IMAP_PORT = 993
SMTP_PORT = 465
MAILBOX = "info@example.com"
ALIAS = "abuse@example.com"

context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE


def password_of(address):
    for line in open("mailbox-passwords.txt", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and len(line.split()) == 2:
            name, secret = line.split()
            if name == address:
                return secret
    sys.exit("alias-probe: no password for " + address)


secret = password_of(MAILBOX)
tag = "alias-" + uuid.uuid4().hex[:8]

server = smtplib.SMTP_SSL(HOST, SMTP_PORT, context=context, timeout=30)
server.login(MAILBOX, secret)
server.sendmail(MAILBOX, [ALIAS],
                "From: %s\r\nTo: %s\r\nSubject: %s\r\n\r\nalias probe\r\n"
                % (MAILBOX, ALIAS, tag))
server.quit()

for _ in range(10):
    time.sleep(3)
    session = imaplib.IMAP4_SSL(HOST, IMAP_PORT, ssl_context=context)
    session.login(MAILBOX, secret)
    session.select("INBOX")
    ids = session.search(None, "HEADER", "Subject", tag)[1][0].split()
    for message_id in ids:
        session.store(message_id, "+FLAGS", r"\Deleted")
    if ids:
        session.expunge()
    session.logout()
    if ids:
        print("alias delivery: OK (%s -> %s, probe removed)" % (ALIAS, MAILBOX))
        break
else:
    sys.exit("alias delivery: FAILED — %s did not reach %s within 30s"
             % (ALIAS, MAILBOX))
