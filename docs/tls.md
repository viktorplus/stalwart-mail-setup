# TLS certificates

## How the certificate gets here

Caddy owns the certificate. It answers the ACME HTTP challenge on port 80,
stores the issued pair under `compose/caddy_data/caddy/certificates/`, and
renews it roughly every 60 days.

Stalwart cannot read that directory. Caddy creates it mode 0700 owned by root,
and the Stalwart container runs as uid 2000, so pointing Stalwart at the file
path does not work. Instead, Stalwart keeps its own copy of the certificate as
inline PEM in its database, and `bin/reimport-cert.sh` puts it there.

That copy does not follow Caddy's renewal. This is the part that bites: mail
keeps flowing for two months, then the certificate served on port 993 and port
465 expires while the one Caddy serves on 443 is perfectly current. Clients
start refusing to connect and nothing in the web panel looks wrong.

## Installing the renewal hook

Once, after the first certificate has been issued:

```sh
sudo bin/reimport-cert.sh --seed          # record the current certificate
sudo crontab -e
```

Add:

```
30 3 * * * /path/to/stalwart-mail-setup/bin/reimport-cert.sh >> /var/log/reimport-cert.log 2>&1
```

The script fingerprints the certificate on disk and exits without doing
anything when it has not changed, so a daily run costs nothing. When Caddy has
renewed, it imports the new pair and restarts Stalwart — a few seconds of
dropped connections, which mail clients retry.

It must run as root; no other user can read Caddy's certificate directory.

## Checking what is actually being served

```sh
# what SMTP submission serves
openssl s_client -connect mx.example.com:465 -servername mx.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -dates -subject

# what IMAP serves
openssl s_client -connect mx.example.com:993 -servername mx.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -dates -subject

# what Caddy serves
openssl s_client -connect mx.example.com:443 -servername mx.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -dates -subject
```

All three should show the same `notAfter`. If 443 is newer than 465 and 993,
the hook is not running — re-run it by hand with `--force` and check the cron
entry.

## Before the first certificate exists

Until the hostname's A record resolves to the server and ports 80 and 443 are
reachable, Caddy cannot complete the ACME challenge, and Stalwart serves a
self-signed placeholder. Everything works, but clients warn about the
certificate.

`bin/setup.sh` says so and continues. Once DNS is in place:

```sh
sudo bin/reimport-cert.sh
```

If Caddy still has not issued anything, its log says why:

```sh
docker compose -f compose/docker-compose.yml logs caddy | tail -40
```
