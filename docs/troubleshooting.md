# Troubleshooting

## The admin panel returns 502 after a while

Stalwart bans IP addresses that fail authentication repeatedly. Behind a
reverse proxy every request arrives from the proxy's address, so a few failed
sign-ins get the proxy itself banned — and then the whole panel is gone, not
just the offender.

Tell Stalwart to read the forwarded address, and clear the ban:

```sh
bin/sw-cli.sh update Http singleton --field useXForwarded=true
bin/sw-cli.sh create Action/ReloadSettings
bin/sw-cli.sh query BlockedIp
bin/sw-cli.sh delete BlockedIp <id>
bin/sw-cli.sh create Action/ReloadBlockedIps
```

`ReloadBlockedIps` matters: removing the row is not enough, the running server
keeps its in-memory ban list until it is reloaded.

## A setting was saved but nothing changed

Writes land in the database while the running server keeps the previous
settings. After changing anything outside `bin/apply-config.py`:

```sh
bin/sw-cli.sh create Action/ReloadSettings
```

The symptom is indistinguishable from a wrong value, which is what makes it
expensive.

## MX records point at something like `4f92af2771a6`

That is the container's own host name. It means the server hostname was never
applied. Check that `hostname` is set in `mailserver.conf` and re-run:

```sh
bin/setup.sh
bin/show-dns.sh
```

## Creating a mailbox fails with "Password is too weak"

Stalwart rejects passwords that a dictionary attack would find, including for
the `admin_password` in `mailserver.conf`. Use a long passphrase of several
uncommon words. Generated mailbox passwords are random and always pass.

## Mail is not reaching other servers

In order of likelihood:

1. **Port 25 is blocked outbound.** Most providers block it by default.
   `nc -zv gmail-smtp-in.l.google.com 25` from the server shows whether it is
   open; the provider's support unblocks it on request.
2. **No PTR record**, or one that does not match the hostname. Check with
   `dig +short -x <server ip>`.
3. **SPF still points at the old server.** A domain may have only one SPF
   record; the new one has to replace the old, not sit beside it.
4. **DKIM records not published.** `bin/show-dns.sh` prints them; the RSA one
   must be pasted as a single joined string.

Send to [mail-tester.com](https://www.mail-tester.com/) — it names the failing
check directly.

## Mail arrives but lands in spam

A brand-new IP has no sending reputation. Send low volumes at first and let it
build. Confirm SPF, DKIM and DMARC all pass, that the PTR matches, and that
the `From` address belongs to a domain this server is authoritative for.

## A test message sent from an outside address disappears

The spam filter runs on unauthenticated sessions. Probes sent from domains
that publish `v=spf1 -all` — `example.com` among them — are dropped silently.
Test through authenticated submission on port 465 instead, which is what
`bin/verify.sh` does.

## An IMAP search finds nothing in `Junk` or `Sent`

The folders are named `Junk Mail`, `Sent Items` and `Deleted Items`. Quote the
name when it contains a space.

## Port 587 does not respond from outside

Some providers filter it at the network edge, before it reaches the host. Use
465, which this setup publishes for exactly this reason. Confirm from outside
with `nc -zv mx.example.com 587` and `nc -zv mx.example.com 465`.

## The stack will not start

```sh
docker compose -f compose/docker-compose.yml ps
docker compose -f compose/docker-compose.yml logs stalwart | tail -40
docker compose -f compose/docker-compose.yml logs caddy | tail -40
```

A Stalwart container that restarts in a loop with `Permission denied` has lost
ownership of its data directory; the image runs as uid 2000 and the volume
must be writable by it.

If port 80, 443, 25, 465, 587 or 993 is already taken, `docker compose up`
says which. Find the holder with `ss -lptn 'sport = :25'` before changing
anything — another mail server on the host will fight this one for port 25.

## Going beyond what the scripts cover

Everything in Stalwart is reachable through the CLI:

```sh
bin/sw-cli.sh describe                 # every object type
bin/sw-cli.sh describe Account         # one object's fields
bin/sw-cli.sh query Account
bin/sw-cli.sh get Domain <id> --json
```

Two encoding rules are not obvious from the field descriptions: a set is
`{"name": true}`, not an array, and a list is `{"0": {...}, "1": {...}}`.
Objects with variants carry `@type`.
