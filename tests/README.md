# Tests

Two of them, with different jobs.

| | What it proves | Needs | Time |
|---|---|---|---|
| `test_config.py` | `mailserver.conf` is parsed and validated correctly, and the Caddy templates render with nothing left unsubstituted | python3 | under a second |
| `integration.sh` | the whole setup works against a real Stalwart: domains, mailboxes, aliases, DNS records, IMAP, SMTP, delivery | Linux, Docker, python3 | 2–4 minutes |

## Unit tests

```sh
python3 tests/test_config.py
```

Standard library only — no Docker, no network, no packages to install. Runs
anywhere python3 does, including Windows.

They cover the failure that is otherwise invisible: a `mailserver.conf` that
is accepted despite being wrong. An unknown key, a mailbox on a domain that
was never declared, an alias with a typo, a missing `hostname` — each has to
be refused with a message that says which line and why. They also render the
repository's real Caddy templates and check that no `{{PLACEHOLDER}}`
survives, because one that does takes Caddy down at the next restart.

Run one test or one class while working on the parser:

```sh
python3 -m unittest tests.test_config.TestReadConf -v
python3 -m unittest tests.test_config.TestReadConf.test_alias_on_undeclared_domain_is_rejected
```

## Integration test

```sh
tests/integration.sh
```

This is the one that matters for keeping the repository honest.

The scripts talk to Stalwart through field shapes that are not documented
anywhere — `mailExchangers`, the `credentials` list, the alias map, the
`Account/User` variant. They were established by inspecting a running server.
When an upstream release changes one, nothing fails loudly: the setup still
runs, and the mailboxes quietly come out wrong. Running the real scripts
against the real image is the only way to see it.

Eight steps, each asserted:

1. `render` writes `.env` and `Caddyfile` with the configured hosts.
2. Caddy itself validates the generated `Caddyfile`.
3. `docker compose config` validates the stack.
4. A throwaway Stalwart starts and reports healthy.
5. `apply` creates two domains and three mailboxes with their aliases, and
   records the generated passwords.
6. A second `apply` changes nothing and rotates no password — the password
   file must come out byte-identical.
7. `show-dns.sh` produces MX, SPF, DKIM, DMARC and SRV records carrying the
   configured hostname, not the container's.
8. `verify.sh` signs every mailbox in over IMAP, sends a message and confirms
   delivery; `alias-probe.py` then sends to an alias address and confirms it
   reaches the mailbox behind it.

### Safety

It is built to be harmless on a machine that already runs a mail server:

- It refuses to start if a container named `stalwart`, a `mail_net` network or
  a `stalwart_data` volume already exists. It never adopts an existing
  deployment.
- It publishes nothing on a host port. The IMAP and SMTP checks run inside a
  container that shares the test server's network namespace.
- It works on a copy of `bin/` and `compose/` in a temporary directory, so a
  `mailserver.conf` in the checkout is never read, written or overwritten.
- The container, volume, network and temporary directory are removed on exit,
  including on failure and on Ctrl-C.

### Testing against a specific version

```sh
STALWART_IMAGE=stalwartlabs/stalwart:v0.16.0 tests/integration.sh
```

Useful for confirming which release changed a field shape when the scheduled
run goes red.

## Continuous integration

`.github/workflows/ci.yml` runs the unit tests, `sh -n` and `shellcheck` over
every shell script, an executable-bit check, and the full integration run.

It fires on push and pull request, and — the important one — **every Monday at
05:00 UTC**. The code here will rarely change; `stalwartlabs/stalwart:latest`
and `caddy:2` will. The scheduled run is what turns an upstream breaking
change into a red build instead of a broken mail server on someone's host.

Run it manually from the Actions tab (`workflow_dispatch`) after any upstream
release you care about.

## When the integration test fails

Read which of the eight steps failed; each prints `ok:` lines for what passed.

- **Step 4, not healthy** — `docker logs stalwart` is printed. Usually the
  data volume or a changed entry point.
- **Step 5, an `apply` error** — the message from `stalwart-cli` is passed
  through verbatim. A field shape has changed; confirm with
  `bin/sw-cli.sh describe <Object>` against a running instance and fix
  `bin/apply-config.py`.
- **Step 6, a password was rotated** — the idempotency guard broke. This is
  the most damaging failure mode in production: a re-run would lock everyone
  out of their mailboxes.
- **Step 7, the MX record carries a container id** — `defaultHostname` or
  `mailExchangers` no longer does what it did.
- **Step 8, delivery failed** — the alias map encoding, or spam filtering on
  the submission path.
