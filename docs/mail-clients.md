# Connecting a mail client

## Server settings

The same for every mailbox — only the username changes.

| Setting | Value |
|---|---|
| Incoming server (IMAP) | `mx.example.com`, port **993**, SSL/TLS (implicit) |
| Outgoing server (SMTP) | `mx.example.com`, port **465**, SSL/TLS (implicit) |
| Authentication | normal password (`AUTH PLAIN` over TLS) |
| Username | the **full email address**, not just the part before `@` |
| Password | from `mailbox-passwords.txt` on the server |
| Webmail | `https://webmail.example.com/`, if `webmail_host` was set |
| Admin panel | `https://mx.example.com/` — server settings only, not for reading mail |

Replace `mx.example.com` with the `hostname` from your `mailserver.conf`.

Port 587 with STARTTLS is also open and works. Prefer 465: some hosting
providers and public networks block 587 outbound, and implicit TLS has no
plaintext phase to downgrade.

POP3 is not published by this setup. IMAP keeps the mailbox in sync across
devices; POP3 does not.

The server's folder names are `INBOX`, `Sent Items`, `Drafts`, `Junk Mail`
and `Deleted Items`. Point the client's special folders at these rather than
letting it create its own `Sent` or `Trash` alongside them.

## Automatic configuration

If you published the `autoconfig` and `autodiscover` records from
[dns.md](dns.md), Thunderbird and Outlook find all of the above from the email
address alone. Enter the address and the password; accept what the client
proposes and check that it chose ports 993 and 465.

## Thunderbird

1. **Account Settings → Account Actions → Add Mail Account**.
2. Name, full email address, password → **Continue**.
3. If autoconfiguration does not pick it up, choose **Configure manually** and
   enter the table above.
4. **Done**.

## Outlook

1. **File → Add Account**, enter the address, choose **Advanced options → Let
   me set up my account manually**.
2. Account type **IMAP**.
3. Incoming `mx.example.com`, port 993, encryption SSL/TLS.
   Outgoing `mx.example.com`, port 465, encryption SSL/TLS.
4. Enter the password. Under **More Settings → Outgoing Server**, confirm that
   the outgoing server requires authentication and reuses the incoming
   credentials.

## iOS

**Settings → Apps → Mail → Mail Accounts → Add Account → Other → Add Mail
Account**. Enter name, address and password, choose **IMAP**, and set both
host names to `mx.example.com` with the same address and password for the
incoming and the outgoing server. iOS picks ports 993 and 465 itself.

## Android

The stock Gmail app and K-9 Mail both work. Add the account as **Other** /
**Personal (IMAP)**, then set the incoming server to `mx.example.com` port 993
with SSL/TLS and the outgoing server to `mx.example.com` port 465 with
SSL/TLS, with authentication enabled. The username is the full address in both
places.

## Sending from application code

Use a mailbox of its own for automated mail rather than a person's, so that
rotating its password or revoking it does not lock anyone out of their mail.
Add it to `mailserver.conf` like any other mailbox:

```
mailbox = noreply@example.com
```

Connection parameters:

| Parameter | Value |
|---|---|
| Host | `mx.example.com` |
| Port | `465` |
| Encryption | implicit TLS (`SMTPS` / `SMTP_SSL`) |
| Username | `noreply@example.com` |
| Password | from `mailbox-passwords.txt` |
| From | must be an address this mailbox owns, or DKIM and SPF will not match |

Python:

```python
import smtplib
import ssl
from email.message import EmailMessage

message = EmailMessage()
message["From"] = "noreply@example.com"
message["To"] = "someone@example.org"
message["Subject"] = "Subject line"
message.set_content("Body text.")

with smtplib.SMTP_SSL("mx.example.com", 465,
                      context=ssl.create_default_context()) as server:
    server.login("noreply@example.com", PASSWORD)
    server.send_message(message)
```

PHP (Laravel `config/mail.php` or `.env`):

```
MAIL_MAILER=smtp
MAIL_HOST=mx.example.com
MAIL_PORT=465
MAIL_ENCRYPTION=tls
MAIL_USERNAME=noreply@example.com
MAIL_PASSWORD=...
MAIL_FROM_ADDRESS=noreply@example.com
```

Keep the password in an environment variable, never in the source tree.

## Checking from the command line

```sh
# IMAP sign-in
curl -v --url "imaps://mx.example.com:993/INBOX" --user "info@example.com:PASSWORD"

# SMTP submission
swaks --to someone@example.org --from info@example.com \
      --server mx.example.com:465 --tls-on-connect \
      --auth PLAIN --auth-user info@example.com
```

`bin/verify.sh` on the server does both for every mailbox at once.
