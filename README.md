# Stalwart mail server setup

Set up a working mail server from scratch on one Linux host, and connect mail
clients to it.

Everything is declared in a single file, `mailserver.conf` — hostname, domains,
mailboxes, aliases — and applied with one command. No interactive wizard, no
settings scattered across the stack.

What you get:

- **Stalwart** for SMTP and IMAP, with DKIM keys generated per domain.
- **Caddy** in front, obtaining and renewing the Let's Encrypt certificate.
- **SnappyMail** webmail, optional.
- Ready-to-publish DNS records (MX, SPF, DKIM, DMARC, MTA-STS, autoconfig),
  printed from the live configuration rather than typed by hand.
- Connection settings for Thunderbird, Outlook, iOS, Android and application
  code: [docs/mail-clients.md](docs/mail-clients.md).

---

## Requirements

- A Linux host with Docker and the Compose plugin, plus `python3` and
  `openssl` (both are present on a standard Debian or Ubuntu install).
- Root access. The mail ports are privileged, and only root can read the
  certificate directory Caddy creates.
- **Port 25 open in both directions.** Many providers block it by default and
  unblock it on request. Without it the server cannot exchange mail with
  anyone.
- **An A record** for the mail hostname, for example `mx.example.com`,
  pointing at the host. Not behind a CDN or proxy: mail ports do not survive
  proxying, and the certificate must be issued to the real address.
- **A PTR record** for the host's IP address, resolving back to that same
  hostname. Set it in your provider's control panel. Most large receivers
  reject mail from a host whose reverse DNS does not match.
- The domains you intend to serve, with access to their DNS zones.

## Install

```sh
git clone https://github.com/viktorplus/stalwart-mail-setup.git
cd stalwart-mail-setup
cp mailserver.conf.example mailserver.conf
chmod 600 mailserver.conf
```

Edit `mailserver.conf`. It is short and every field is commented:

```
hostname       = mx.example.com
acme_email     = admin@example.com
admin_password = a-long-unique-passphrase
webmail_host   = webmail.example.com

domain  = example.com
domain  = example.net

mailbox = info@example.com     postmaster@example.com, abuse@example.com
mailbox = sales@example.com
mailbox = info@example.net     postmaster@example.net, abuse@example.net
```

Then run the setup:

```sh
sudo bin/setup.sh
```

It renders the Caddy and Compose configuration, starts the stack, waits for
Stalwart to come up, creates the domains and mailboxes, and imports the TLS
certificate once Caddy has issued one.

A password is generated for every new mailbox and written to
`mailbox-passwords.txt` (mode 600). It is never printed to the terminal.

`bin/setup.sh` is safe to re-run. After editing `mailserver.conf` — a new
domain, another mailbox, an extra alias — run it again: it applies the
difference and keeps the password of every mailbox that already exists.

## Publish the DNS records

```sh
bin/show-dns.sh
```

This prints the exact records for every configured domain, generated from the
live configuration and the server's own DKIM keys.

Do not apply them all at once. The records split into a routing-neutral set
you can publish immediately and a cutover set that moves mail, one domain at a
time. [docs/dns.md](docs/dns.md) explains which is which and in what order.

## Check that it works

```sh
bin/verify.sh
```

Signs every mailbox in over IMAP, sends one message through SMTP submission,
confirms it is delivered, and removes the test message. It prints the address
and the result, never the password.

Once MX records point at the server, send a message to
[mail-tester.com](https://www.mail-tester.com/) from one of the mailboxes and
confirm SPF, DKIM and DMARC all pass.

## Connect a mail client

Server settings, per-client instructions and the parameters for sending from
application code are in [docs/mail-clients.md](docs/mail-clients.md).

The short version: the server is the hostname from `mailserver.conf`, IMAP is
port 993 with implicit TLS, SMTP is port 465 with implicit TLS, the username
is the full email address, and the password is the one in
`mailbox-passwords.txt`.

## Keep the certificate current

Caddy renews the certificate on disk about every 60 days, but Stalwart holds
its copy in the database and does not follow that renewal on its own. Install
the hook once:

```sh
sudo bin/reimport-cert.sh --seed
sudo crontab -e
# 30 3 * * * /path/to/stalwart-mail-setup/bin/reimport-cert.sh >> /var/log/reimport-cert.log 2>&1
```

Skipping this means the certificate served on SMTP and IMAP quietly expires.
[docs/tls.md](docs/tls.md) explains why the copy is needed at all.

---

## What is where

| Path | Purpose |
|---|---|
| `mailserver.conf` | the one file you edit; holds the admin password, so it is gitignored |
| `bin/setup.sh` | render, start, apply, import certificate |
| `bin/apply-config.py` | turns `mailserver.conf` into domains and mailboxes |
| `bin/show-dns.sh` | prints the DNS records for every domain |
| `bin/verify.sh` | IMAP sign-in and a live send/receive test |
| `bin/reimport-cert.sh` | copies the renewed certificate into Stalwart |
| `bin/sw-cli.sh` | direct `stalwart-cli` access for anything not covered above |
| `compose/` | the Docker stack and the Caddy templates |
| `docs/` | DNS, mail clients, TLS, troubleshooting |

Anything the scripts do not cover is reachable through the admin panel at
`https://<hostname>/` (sign in as `admin` with `admin_password`) or through
`bin/sw-cli.sh`:

```sh
bin/sw-cli.sh query Account
bin/sw-cli.sh describe Domain
```

## Licence

MIT — see [LICENSE](LICENSE).
