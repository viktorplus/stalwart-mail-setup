#!/usr/bin/env python3
"""Turn mailserver.conf into a running configuration.

Two steps, both idempotent:

    apply-config.py render   write compose/.env and compose/Caddyfile
    apply-config.py apply    create the domains and mailboxes in Stalwart

`render` runs before the stack starts, `apply` after it is up. bin/setup.sh
calls both in order; run either on its own after editing mailserver.conf.

`apply` never rotates the password of a mailbox that already exists. New
mailboxes get a generated password, written only to mailbox-passwords.txt
(mode 600) and never printed to the terminal.
"""

import json
import os
import re
import secrets
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(ROOT, "mailserver.conf")
COMPOSE = os.path.join(ROOT, "compose")
PASSWORDS = os.path.join(ROOT, "mailbox-passwords.txt")
CLI = os.path.join(ROOT, "bin", "sw-cli.sh")

SCALARS = ("hostname", "acme_email", "admin_password", "webmail_host")


# --------------------------------------------------------------------------
# configuration file
# --------------------------------------------------------------------------

def die(message):
    sys.stderr.write("error: %s\n" % message)
    raise SystemExit(1)


def read_conf():
    """Parse mailserver.conf into {scalars..., domains: [], mailboxes: []}."""
    if not os.path.exists(CONF):
        die("%s not found. Copy mailserver.conf.example to mailserver.conf "
            "and edit it." % CONF)

    conf = {key: "" for key in SCALARS}
    conf["domains"] = []
    conf["mailboxes"] = []

    with open(CONF, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" not in line:
                die("%s line %d: expected `key = value`" % (CONF, lineno))
            key, value = (part.strip() for part in line.split("=", 1))

            if key in SCALARS:
                conf[key] = value.strip("'\"")
            elif key == "domain":
                conf["domains"].append(value.lower())
            elif key == "mailbox":
                conf["mailboxes"].append(parse_mailbox(value, lineno))
            else:
                die("%s line %d: unknown key `%s`" % (CONF, lineno, key))

    for key in ("hostname", "acme_email", "admin_password"):
        if not conf[key]:
            die("%s: `%s` must be set" % (CONF, key))
    if not conf["domains"]:
        die("%s: at least one `domain` line is required" % CONF)

    known = set(conf["domains"])
    for box in conf["mailboxes"]:
        for address in [box["address"]] + box["aliases"]:
            domain = address.split("@", 1)[1]
            if domain not in known:
                die("%s: `%s` uses domain %s, which has no `domain` line"
                    % (CONF, address, domain))
    return conf


def parse_mailbox(value, lineno):
    """Parse `info@example.com  postmaster@example.com, abuse@example.com`."""
    parts = value.split(None, 1)
    address = parts[0].lower()
    aliases = []
    if len(parts) > 1:
        aliases = [a.strip().lower() for a in parts[1].split(",") if a.strip()]
    for candidate in [address] + aliases:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", candidate):
            die("%s line %d: `%s` is not an email address"
                % (CONF, lineno, candidate))
    return {"address": address, "aliases": aliases}


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def write_private(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def substitute(name, values):
    with open(os.path.join(COMPOSE, name), encoding="utf-8") as handle:
        text = handle.read()
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    leftover = re.search(r"\{\{(\w+)\}\}", text)
    if leftover:
        die("%s: placeholder {{%s}} was not substituted"
            % (name, leftover.group(1)))
    return text


def render(conf):
    write_private(
        os.path.join(COMPOSE, ".env"),
        "# Generated from mailserver.conf. Do not edit.\n"
        "STALWART_RECOVERY_ADMIN=admin:%s\n" % conf["admin_password"])
    print("wrote compose/.env")

    caddyfile = substitute("Caddyfile.template", {
        "ACME_EMAIL": conf["acme_email"],
        "MAIL_HOST": conf["hostname"],
    })
    if conf["webmail_host"]:
        caddyfile += substitute("Caddyfile.webmail.template", {
            "WEBMAIL_HOST": conf["webmail_host"],
        })
    with open(os.path.join(COMPOSE, "Caddyfile"), "w", encoding="utf-8",
              newline="\n") as handle:
        handle.write(caddyfile)
    print("wrote compose/Caddyfile (%s)"
          % ("with webmail" if conf["webmail_host"] else "no webmail"))


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def cli(*args):
    result = subprocess.run([CLI] + list(args), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True)
    if result.returncode != 0:
        die("stalwart-cli %s failed:\n%s"
            % (" ".join(args), (result.stderr or result.stdout).strip()))
    return result.stdout


def table(output):
    """Turn `query` output into rows of columns, dropping the header line."""
    lines = [line for line in output.splitlines() if line.strip()]
    return [line.split() for line in lines[1:]]


def index_by_address(object_name):
    return {row[1].lower(): row[0] for row in table(cli("query", object_name))}


def apply(conf):
    domain_ids = index_by_address("Domain")
    for name in conf["domains"]:
        if name in domain_ids:
            print("domain   %-32s exists (id %s)" % (name, domain_ids[name]))
            continue
        cli("create", "Domain", "--field", "name=%s" % name)
        domain_ids = index_by_address("Domain")
        print("domain   %-32s created (id %s)" % (name, domain_ids[name]))

    # The hostname is what goes into the published MX and SRV records. Without
    # it Stalwart falls back to the container's own host name.
    exchangers = {"0": {"hostname": conf["hostname"], "priority": 10}}
    cli("update", "SystemSettings", "singleton",
        "--field", "defaultHostname=%s" % conf["hostname"],
        "--field", "defaultDomainId=%s" % domain_ids[conf["domains"][0]],
        "--field", "mailExchangers=%s" % json.dumps(exchangers))
    print("hostname %-32s applied" % conf["hostname"])

    account_ids = index_by_address("Account")
    created = []

    for box in conf["mailboxes"]:
        address = box["address"]
        local, domain = address.split("@", 1)

        if address in account_ids:
            print("mailbox  %-32s exists (id %s, password kept)"
                  % (address, account_ids[address]))
        else:
            password = secrets.token_urlsafe(18)
            credentials = {"0": {"@type": "Password", "secret": password}}
            cli("create", "Account/User",
                "--field", "name=%s" % local,
                "--field", "domainId=%s" % domain_ids[domain],
                "--field", "credentials=%s" % json.dumps(credentials))
            account_ids = index_by_address("Account")
            created.append((address, password))
            print("mailbox  %-32s created (id %s)"
                  % (address, account_ids[address]))

        # `update` replaces the whole alias map, so every alias of a mailbox
        # goes in one call. An alias is {"name", "domainId"}.
        aliases = {}
        for position, alias in enumerate(box["aliases"]):
            alias_local, alias_domain = alias.split("@", 1)
            aliases[str(position)] = {"name": alias_local,
                                      "domainId": domain_ids[alias_domain]}
        cli("update", "Account", account_ids[address],
            "--field", "aliases=%s" % json.dumps(aliases))
        if box["aliases"]:
            print("         %-32s %d alias(es) applied"
                  % ("", len(box["aliases"])))

    if created:
        save_passwords(created)

    # Stored settings do not reach live sessions until they are reloaded.
    cli("create", "Action/ReloadSettings")
    print("settings reloaded")


def save_passwords(created):
    if not os.path.exists(PASSWORDS):
        write_private(PASSWORDS,
                      "# Mailbox passwords. Keep secret; this file is mode 600\n"
                      "# and is listed in .gitignore.\n\n")
    with open(PASSWORDS, "a", encoding="utf-8", newline="\n") as handle:
        for address, password in created:
            handle.write("%-32s %s\n" % (address, password))
    os.chmod(PASSWORDS, stat.S_IRUSR | stat.S_IWUSR)
    print("\n%d new password(s) written to mailbox-passwords.txt" % len(created))


# --------------------------------------------------------------------------

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("render", "apply"):
        sys.stderr.write("usage: apply-config.py render|apply\n")
        raise SystemExit(2)
    conf = read_conf()
    (render if sys.argv[1] == "render" else apply)(conf)


if __name__ == "__main__":
    main()
