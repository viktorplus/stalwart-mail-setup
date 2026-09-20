# DNS records

`bin/show-dns.sh` prints the records for every configured domain. The strings
come from Stalwart itself — it generates them from the live configuration and
the DKIM keys it holds — so they are never hand-typed and never drift from
what the server actually does.

Re-run it after adding a domain, changing the hostname, or rotating DKIM keys.

## Before anything else

Two records are not in that output because they belong to the host, not to a
mail domain. Both must already be in place:

- `mx.example.com. IN A <server ip>` — DNS only, not behind a CDN or proxy.
  Mail ports do not survive proxying, and the certificate must be issued to
  the real address.
- A PTR record for `<server ip>` resolving to `mx.example.com`. Set it in the
  server provider's control panel, not in the DNS zone. Large receivers reject
  mail from a host whose reverse DNS does not match its hostname.

## Do not apply everything at once

The output mixes records that are safe right now with records that switch live
mail routing. Apply them in two stages.

### Stage 1 — now, all at once

These change nothing about where mail goes. Publishing DKIM early removes the
signing gap at cutover.

- `*._domainkey.<domain>` TXT — the DKIM public keys, one Ed25519 and one RSA
  per domain.
- `_imaps`, `_submissions`, `_jmap`, `_caldavs`, `_carddavs`, `_pop3s` SRV —
  client autoconfiguration hints, routing-neutral.
- `autoconfig.<domain>` and `autodiscover.<domain>` CNAME — client
  autoconfiguration.
- `_smtp._tls.<domain>` TXT — TLS reporting.

The RSA DKIM record is split across two quoted strings inside `( ... )`. Most
control panels want it pasted as one joined string with the quotes and
parentheses removed.

### Stage 2 — the cutover, one domain at a time

These move mail. Change them together, as one atomic edit per domain:

- `<domain>. IN MX 10 mx.example.com.`
- `<domain>. IN TXT "v=spf1 mx -all"`
- `_dmarc.<domain>` TXT

Publish `mta-sts.<domain>` CNAME and `_mta-sts.<domain>` TXT only once Caddy
actually serves the policy at
`https://mta-sts.<domain>/.well-known/mta-sts.txt`; otherwise hold them back.

Do one domain, watch delivery for a day, then do the next.

## Replacing an existing setup

If the domain already has mail somewhere else:

- Remove the old MX records in the same edit that adds the new one. Two MX
  records pointing at two servers split incoming mail between them.
- Check the current SPF record before publishing the new one. A domain may
  only have one. The generated record uses `-all`, which is stricter than the
  common `~all` and will reject anything not sent through this server — so it
  has to take effect together with the MX change, never before it.
- Leave the old server running until the change has propagated everywhere and
  its mailboxes have stopped receiving. TTL is a lower bound, not a promise.

## Checking

```sh
dig +short MX example.com
dig +short TXT example.com
dig +short TXT v1-ed25519-20260920._domainkey.example.com
dig +short -x <server ip>
```

Then send a message from a configured mailbox to
[mail-tester.com](https://www.mail-tester.com/) and confirm SPF, DKIM and
DMARC all pass.
