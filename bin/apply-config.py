#!/usr/bin/env python3
"""Turn mailserver.conf into a running configuration.

All steps are idempotent:

    apply-config.py render         write compose/.env and compose/Caddyfile
                                   (and the fail2ban jail when webmail is on)
    apply-config.py apply          create the domains and mailboxes in Stalwart
    apply-config.py webmail        set the SnappyMail admin key and client IP
    apply-config.py allow-webmail  exempt SnappyMail from Stalwart's IP ban

`render` runs before the stack starts, the others after it is up.
bin/setup.sh calls them in the order render, apply, webmail, allow-webmail;
run render or apply on its own after editing mailserver.conf.

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
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(ROOT, "mailserver.conf")
COMPOSE = os.path.join(ROOT, "compose")
PASSWORDS = os.path.join(ROOT, "mailbox-passwords.txt")
CLI = os.path.join(ROOT, "bin", "sw-cli.sh")

ADMIN_KEY_FILE = os.path.join(ROOT, "snappymail-admin-key.txt")
SNAPPYMAIL_DATA = os.path.join(COMPOSE, "snappymail_data", "_data_", "_default_")
SNAPPYMAIL_INI = os.path.join(SNAPPYMAIL_DATA, "configs", "application.ini")
SNAPPYMAIL_AUTH_LOG = os.path.join(SNAPPYMAIL_DATA, "logs", "auth.log")
FAIL2BAN_DIR = os.path.join(COMPOSE, "fail2ban")

# mail_net in compose/docker-compose.yml. Docker hands out dynamic addresses
# only inside MAIL_DYNAMIC_RANGE, so SNAPPYMAIL_IP never goes to another
# container.
MAIL_SUBNET = "172.16.89.0/24"
MAIL_DYNAMIC_RANGE = "172.16.89.128/25"
SNAPPYMAIL_IP = "172.16.89.10"
SNAPPYMAIL_REASON = ("SnappyMail webmail; brute force is banned by fail2ban "
                     "on the host")

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

    if conf["webmail_host"]:
        write_private(
            os.path.join(FAIL2BAN_DIR, "jail.d", "snappymail.local"),
            substitute(os.path.join("fail2ban", "jail.d",
                                    "snappymail.local.template"),
                       {"AUTH_LOG": SNAPPYMAIL_AUTH_LOG}))
        print("wrote compose/fail2ban/jail.d/snappymail.local")


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
    cli("create", "Action/ReloadSettings", "--json", "{}")
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
# webmail
# --------------------------------------------------------------------------

def patch_snappymail_ini(text, key):
    """Set the admin panel key and trust the client IP from the proxy."""
    wanted = {
        ("admin_panel", "key"): 'key = "%s"' % key,
        ("labs", "http_client_ip_check_proxy"): "http_client_ip_check_proxy = On",
    }
    found = set()
    section = None
    lines = text.splitlines(True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        name = stripped.split("=", 1)[0].strip()
        if (section, name) in wanted and "=" in stripped:
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = wanted[(section, name)] + ending
            found.add((section, name))
    for section, name in wanted:
        if (section, name) not in found:
            die("%s: no `%s` line under [%s]; unexpected SnappyMail version"
                % (SNAPPYMAIL_INI, name, section))
    return "".join(lines)


def admin_key():
    """The SnappyMail admin panel key, generated once and kept on file."""
    if not os.path.exists(ADMIN_KEY_FILE):
        write_private(ADMIN_KEY_FILE, secrets.token_hex(12) + "\n")
    with open(ADMIN_KEY_FILE, encoding="utf-8") as handle:
        key = handle.read().strip()
    if not key:
        die("snappymail-admin-key.txt is empty. Put the key from [admin_panel] "
            "in %s into it, or delete it to generate a new key."
            % SNAPPYMAIL_INI)
    return key


def ensure_auth_log():
    """Create SnappyMail's auth log, owned like application.ini.

    fail2ban refuses a jail whose log file does not exist, and SnappyMail
    writes the file only on the first failed sign-in.
    """
    if os.path.exists(SNAPPYMAIL_AUTH_LOG):
        return
    owner = os.stat(SNAPPYMAIL_INI)
    logs = os.path.dirname(SNAPPYMAIL_AUTH_LOG)
    os.makedirs(logs, mode=0o700, exist_ok=True)
    write_private(SNAPPYMAIL_AUTH_LOG, "")
    if hasattr(os, "chown"):
        for path in (logs, SNAPPYMAIL_AUTH_LOG):
            os.chown(path, owner.st_uid, owner.st_gid)


def webmail(conf):
    if not conf["webmail_host"]:
        die("%s: `webmail_host` is not set" % CONF)

    # SnappyMail writes application.ini on its first run. Ask it for a page
    # in case it has not run yet, then wait for the file.
    if not os.path.exists(SNAPPYMAIL_INI):
        subprocess.run(["docker", "exec", "snappymail", "wget", "-q",
                        "-O", "/dev/null", "http://127.0.0.1:8888/"],
                       stdin=subprocess.DEVNULL, capture_output=True)
        for _ in range(60):
            if os.path.exists(SNAPPYMAIL_INI):
                break
            time.sleep(1)
        else:
            die("%s did not appear within 60 s. Check that the snappymail "
                "container is running: docker logs snappymail" % SNAPPYMAIL_INI)

    generated = not os.path.exists(ADMIN_KEY_FILE)
    key = admin_key()
    with open(SNAPPYMAIL_INI, encoding="utf-8") as handle:
        text = patch_snappymail_ini(handle.read(), key)
    write_private(SNAPPYMAIL_INI, text)
    ensure_auth_log()
    print("webmail  admin panel key %s (snappymail-admin-key.txt)"
          % ("generated" if generated else "kept"))
    print("webmail  client IP taken from the proxy")


def allow_webmail(conf):
    """Exempt the webmail container from Stalwart's automatic IP ban."""
    if any(SNAPPYMAIL_IP in row for row in table(cli("query", "AllowedIp"))):
        print("allowip  %-32s exists" % SNAPPYMAIL_IP)
    else:
        cli("create", "AllowedIp",
            "--field", "address=%s" % SNAPPYMAIL_IP,
            "--field", "reason=%s" % SNAPPYMAIL_REASON)
        print("allowip  %-32s created" % SNAPPYMAIL_IP)
    cli("create", "Action/ReloadSettings", "--json", "{}")
    print("settings reloaded")


# --------------------------------------------------------------------------

COMMANDS = {"render": render, "apply": apply, "webmail": webmail,
            "allow-webmail": allow_webmail}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.stderr.write("usage: apply-config.py "
                         "render|apply|webmail|allow-webmail\n")
        raise SystemExit(2)
    COMMANDS[sys.argv[1]](read_conf())


if __name__ == "__main__":
    main()
