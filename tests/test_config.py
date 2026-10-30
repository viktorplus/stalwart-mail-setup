#!/usr/bin/env python3
"""Unit tests for the configuration parser and the template renderer.

Pure Python, standard library only, no Docker and no network. These cover the
part of the setup that fails silently: a typo in mailserver.conf that is
accepted and then breaks half way through, or a template placeholder that is
never substituted and takes Caddy down on the next restart.

    python3 tests/test_config.py
"""

import contextlib
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The module under test has a dash in its name, so it cannot be imported by
# name. Load it from its path instead.
_spec = importlib.util.spec_from_file_location(
    "apply_config", os.path.join(ROOT, "bin", "apply-config.py"))
apply_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(apply_config)


VALID = """
hostname       = mx.example.com
acme_email     = admin@example.com
admin_password = a-long-unique-passphrase
webmail_host   = webmail.example.com

domain = example.com
domain = example.net

mailbox = info@example.com   postmaster@example.com, abuse@example.com
mailbox = sales@example.com
"""


class ConfigCase(unittest.TestCase):
    """Base class that points the module at a throwaway mailserver.conf."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conf_path = os.path.join(self.tmp, "mailserver.conf")
        self._saved_conf = apply_config.CONF
        apply_config.CONF = self.conf_path
        self.addCleanup(setattr, apply_config, "CONF", self._saved_conf)

    def write(self, text):
        with open(self.conf_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def read(self, text):
        self.write(text)
        return apply_config.read_conf()

    def assertRejected(self, text, fragment):
        """The file is refused, and the message says why."""
        self.write(text)
        stderr = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(stderr):
            apply_config.read_conf()
        self.assertIn(fragment, stderr.getvalue())


class TestReadConf(ConfigCase):

    def test_parses_a_valid_file(self):
        conf = self.read(VALID)
        self.assertEqual(conf["hostname"], "mx.example.com")
        self.assertEqual(conf["acme_email"], "admin@example.com")
        self.assertEqual(conf["admin_password"], "a-long-unique-passphrase")
        self.assertEqual(conf["webmail_host"], "webmail.example.com")
        self.assertEqual(conf["domains"], ["example.com", "example.net"])
        self.assertEqual(len(conf["mailboxes"]), 2)

    def test_mailbox_aliases(self):
        conf = self.read(VALID)
        first, second = conf["mailboxes"]
        self.assertEqual(first["address"], "info@example.com")
        self.assertEqual(first["aliases"],
                         ["postmaster@example.com", "abuse@example.com"])
        self.assertEqual(second["address"], "sales@example.com")
        self.assertEqual(second["aliases"], [])

    def test_addresses_are_lowercased(self):
        conf = self.read(VALID.replace("info@example.com   postmaster",
                                       "INFO@Example.COM   POSTMASTER"))
        first = conf["mailboxes"][0]
        self.assertEqual(first["address"], "info@example.com")
        self.assertEqual(first["aliases"][0], "postmaster@example.com")

    def test_comments_and_blank_lines_are_ignored(self):
        conf = self.read(VALID + "\n# mailbox = ghost@example.com\n\n")
        self.assertEqual(len(conf["mailboxes"]), 2)

    def test_trailing_comment_is_stripped(self):
        conf = self.read(VALID.replace(
            "hostname       = mx.example.com",
            "hostname       = mx.example.com   # the public name"))
        self.assertEqual(conf["hostname"], "mx.example.com")

    def test_quoted_value_is_unwrapped(self):
        conf = self.read(VALID.replace(
            "admin_password = a-long-unique-passphrase",
            'admin_password = "a-long-unique-passphrase"'))
        self.assertEqual(conf["admin_password"], "a-long-unique-passphrase")

    def test_password_may_contain_spaces(self):
        conf = self.read(VALID.replace(
            "admin_password = a-long-unique-passphrase",
            'admin_password = "correct horse battery staple"'))
        self.assertEqual(conf["admin_password"], "correct horse battery staple")

    def test_webmail_host_is_optional(self):
        conf = self.read(VALID.replace(
            "webmail_host   = webmail.example.com", "webmail_host   ="))
        self.assertEqual(conf["webmail_host"], "")

    def test_missing_file_is_rejected(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(stderr):
            apply_config.read_conf()
        self.assertIn("not found", stderr.getvalue())

    def test_unknown_key_is_rejected(self):
        self.assertRejected(VALID + "\nmailboxes = info@example.com\n",
                            "unknown key")

    def test_line_without_equals_is_rejected(self):
        self.assertRejected(VALID + "\nhostname mx.example.com\n",
                            "key = value")

    def test_missing_hostname_is_rejected(self):
        self.assertRejected(VALID.replace("hostname       = mx.example.com", ""),
                            "must be set")

    def test_missing_admin_password_is_rejected(self):
        self.assertRejected(
            VALID.replace("admin_password = a-long-unique-passphrase", ""),
            "must be set")

    def test_no_domain_lines_is_rejected(self):
        text = "\n".join(line for line in VALID.splitlines()
                         if not line.startswith("domain "))
        self.assertRejected(text, "at least one")

    def test_mailbox_on_undeclared_domain_is_rejected(self):
        self.assertRejected(VALID + "\nmailbox = info@elsewhere.org\n",
                            "has no `domain` line")

    def test_alias_on_undeclared_domain_is_rejected(self):
        self.assertRejected(
            VALID.replace("abuse@example.com", "abuse@elsewhere.org"),
            "has no `domain` line")

    def test_malformed_address_is_rejected(self):
        self.assertRejected(VALID + "\nmailbox = not-an-address\n",
                            "not an email address")

    def test_malformed_alias_is_rejected(self):
        self.assertRejected(VALID.replace("abuse@example.com", "abuse"),
                            "not an email address")


class TestSubstitute(ConfigCase):

    def setUp(self):
        super().setUp()
        self.compose = os.path.join(self.tmp, "compose")
        os.makedirs(self.compose)
        self._saved_compose = apply_config.COMPOSE
        apply_config.COMPOSE = self.compose
        self.addCleanup(setattr, apply_config, "COMPOSE", self._saved_compose)

    def template(self, name, text):
        with open(os.path.join(self.compose, name), "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write(text)

    def test_replaces_every_placeholder(self):
        self.template("t", "host {{MAIL_HOST}} mail {{ACME_EMAIL}}\n")
        out = apply_config.substitute("t", {"MAIL_HOST": "mx.example.com",
                                            "ACME_EMAIL": "a@example.com"})
        self.assertEqual(out, "host mx.example.com mail a@example.com\n")

    def test_unsubstituted_placeholder_is_an_error(self):
        self.template("t", "{{MAIL_HOST}} and {{FORGOTTEN}}\n")
        with self.assertRaises(SystemExit):
            apply_config.substitute("t", {"MAIL_HOST": "mx.example.com"})


class TestRender(ConfigCase):
    """Renders through the repository's real templates."""

    def setUp(self):
        super().setUp()
        self.compose = os.path.join(self.tmp, "compose")
        # Generated files from a local run must not leak into the copy.
        shutil.copytree(os.path.join(ROOT, "compose"), self.compose,
                        ignore=shutil.ignore_patterns(
                            "snappymail.local", "*_data", "*_config"))
        for name, value in (
                ("COMPOSE", self.compose),
                ("FAIL2BAN_DIR", os.path.join(self.compose, "fail2ban"))):
            self.addCleanup(setattr, apply_config, name,
                            getattr(apply_config, name, None))
            setattr(apply_config, name, value)

    def render(self, text):
        conf = self.read(text)
        with contextlib.redirect_stdout(io.StringIO()):
            apply_config.render(conf)
        with open(os.path.join(self.compose, "Caddyfile"),
                  encoding="utf-8") as handle:
            caddyfile = handle.read()
        with open(os.path.join(self.compose, ".env"),
                  encoding="utf-8") as handle:
            env = handle.read()
        return caddyfile, env

    def test_writes_the_admin_credential(self):
        _, env = self.render(VALID)
        self.assertIn("STALWART_RECOVERY_ADMIN=admin:a-long-unique-passphrase",
                      env)

    def test_env_file_is_not_world_readable(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions only")
        self.render(VALID)
        mode = os.stat(os.path.join(self.compose, ".env")).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_caddyfile_carries_the_hosts(self):
        caddyfile, _ = self.render(VALID)
        self.assertIn("mx.example.com {", caddyfile)
        self.assertIn("email admin@example.com", caddyfile)
        self.assertIn("reverse_proxy stalwart:8080", caddyfile)

    def test_webmail_block_present_when_configured(self):
        caddyfile, _ = self.render(VALID)
        self.assertIn("webmail.example.com {", caddyfile)
        self.assertIn("reverse_proxy snappymail:8888", caddyfile)

    def test_webmail_block_absent_when_not_configured(self):
        caddyfile, _ = self.render(
            VALID.replace("webmail_host   = webmail.example.com",
                          "webmail_host   ="))
        self.assertNotIn("snappymail", caddyfile)
        self.assertIn("mx.example.com {", caddyfile)

    def test_no_placeholder_survives(self):
        caddyfile, _ = self.render(VALID)
        self.assertNotIn("{{", caddyfile)

    def test_webmail_proxy_drops_client_ip_header(self):
        caddyfile, _ = self.render(VALID)
        self.assertIn("reverse_proxy snappymail:8888 {", caddyfile)
        self.assertIn("header_up -Client-IP", caddyfile)

    def jail(self):
        return os.path.join(self.compose, "fail2ban", "jail.d",
                            "snappymail.local")

    def test_jail_written_when_webmail_configured(self):
        conf = self.read(VALID)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            apply_config.render(conf)
        self.assertIn("wrote compose/fail2ban/jail.d/snappymail.local",
                      out.getvalue())
        with open(self.jail(), encoding="utf-8") as handle:
            lines = [line.strip() for line in handle]
        expected = [
            "filter = snappymail",
            "logpath = %s" % apply_config.SNAPPYMAIL_AUTH_LOG,
            "logtimezone = UTC",
            "maxretry = 5",
            "findtime = 10m",
            "bantime = 1h",
            "bantime.increment = true",
            "bantime.maxtime = 1w",
            "action = iptables-allports[name=snappymail, chain=DOCKER-USER, "
            "protocol=all]",
        ]
        for line in expected:
            self.assertIn(line, lines)
        self.assertTrue(os.path.isabs(apply_config.SNAPPYMAIL_AUTH_LOG))
        self.assertTrue(apply_config.SNAPPYMAIL_AUTH_LOG.endswith(
            os.path.join("_default_", "logs", "auth.log")))

    def test_jail_not_written_without_webmail(self):
        self.render(VALID.replace("webmail_host   = webmail.example.com",
                                  "webmail_host   ="))
        self.assertFalse(os.path.exists(self.jail()))


class TestRepositoryFiles(unittest.TestCase):
    """Committed files that must agree with the constants in the code."""

    def text(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_constants(self):
        self.assertEqual(apply_config.MAIL_SUBNET, "172.16.89.0/24")
        self.assertEqual(apply_config.MAIL_DYNAMIC_RANGE, "172.16.89.128/25")
        self.assertEqual(apply_config.SNAPPYMAIL_IP, "172.16.89.10")

    def test_compose_pins_the_addresses(self):
        compose = self.text("compose", "docker-compose.yml")
        self.assertIn("subnet: %s" % apply_config.MAIL_SUBNET, compose)
        self.assertIn("ip_range: %s" % apply_config.MAIL_DYNAMIC_RANGE, compose)
        self.assertIn("ipv4_address: %s" % apply_config.SNAPPYMAIL_IP, compose)

    def test_fail2ban_filter(self):
        lines = [line.strip() for line in
                 self.text("compose", "fail2ban", "filter.d",
                           "snappymail.conf").splitlines()]
        self.assertIn(r"failregex = ^\s*(?:Admin )?Auth failed: ip=<HOST> user=",
                      lines)
        self.assertIn("[Init]", lines)
        self.assertIn(r"datepattern = ^\[%%Y-%%m-%%d %%H:%%M:%%S\]", lines)

    def test_documented_actions_carry_a_body(self):
        names = ["README.md"] + [os.path.join("docs", name) for name
                                 in sorted(os.listdir(os.path.join(ROOT, "docs")))]
        actions = [line for name in names
                   for line in self.text(name).splitlines()
                   if "create Action/" in line]
        self.assertTrue(actions)
        for line in actions:
            self.assertTrue(line.rstrip().endswith("--json '{}'"), line)

    def test_generated_files_are_ignored_by_git(self):
        ignored = self.text(".gitignore").splitlines()
        self.assertIn("compose/fail2ban/jail.d/snappymail.local", ignored)
        self.assertIn("snappymail-admin-key.txt", ignored)


SHIPPED_INI = """\
[webmail]
title = "SnappyMail Webmail"

[admin_panel]
host = ""
key = "admin"
allow_update = Off

[labs]
cache_system_data = On
http_client_ip_check_proxy = Off
use_local_proxy_for_external_images = On
"""


class TestPatchIni(unittest.TestCase):

    def test_sets_both_settings(self):
        out = apply_config.patch_snappymail_ini(SHIPPED_INI, "0123abcd")
        expected = (SHIPPED_INI
                    .replace('key = "admin"', 'key = "0123abcd"')
                    .replace("http_client_ip_check_proxy = Off",
                             "http_client_ip_check_proxy = On"))
        self.assertEqual(out, expected)

    def test_is_idempotent(self):
        once = apply_config.patch_snappymail_ini(SHIPPED_INI, "0123abcd")
        self.assertEqual(apply_config.patch_snappymail_ini(once, "0123abcd"),
                         once)

    def test_key_outside_admin_panel_is_not_touched(self):
        text = SHIPPED_INI.replace("[webmail]\n", "[webmail]\nkey = \"x\"\n")
        out = apply_config.patch_snappymail_ini(text, "0123abcd")
        self.assertIn('[webmail]\nkey = "x"\n', out)

    def assertDies(self, text):
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            apply_config.patch_snappymail_ini(text, "0123abcd")

    def test_missing_admin_key_line_is_an_error(self):
        self.assertDies(SHIPPED_INI.replace('key = "admin"\n', ""))

    def test_missing_proxy_line_is_an_error(self):
        self.assertDies(SHIPPED_INI.replace(
            "http_client_ip_check_proxy = Off\n", ""))

    def test_setting_in_the_wrong_section_is_an_error(self):
        self.assertDies(SHIPPED_INI.replace("[labs]\n", "[other]\n"))


class WebmailCase(ConfigCase):
    """Points the key file and the SnappyMail data directory at a temp dir."""

    def setUp(self):
        super().setUp()
        for name, value in (
                ("ADMIN_KEY_FILE", os.path.join(self.tmp, "admin-key.txt")),
                ("SNAPPYMAIL_INI", os.path.join(self.tmp, "application.ini")),
                ("SNAPPYMAIL_AUTH_LOG",
                 os.path.join(self.tmp, "logs", "auth.log"))):
            self.addCleanup(setattr, apply_config, name,
                            getattr(apply_config, name))
            setattr(apply_config, name, value)


class TestAdminKey(WebmailCase):

    def test_created_once_and_kept(self):
        key = apply_config.admin_key()
        self.assertRegex(key, r"^[0-9a-f]{24}$")
        with open(apply_config.ADMIN_KEY_FILE, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), key)
        self.assertEqual(apply_config.admin_key(), key)

    def test_key_file_is_not_world_readable(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions only")
        apply_config.admin_key()
        mode = os.stat(apply_config.ADMIN_KEY_FILE).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)


class TestWebmailCommand(WebmailCase):

    def run_webmail(self, text=VALID):
        conf = self.read(text)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                apply_config.webmail(conf)
            except SystemExit:
                return None, out.getvalue(), err.getvalue()
        return True, out.getvalue(), err.getvalue()

    def write_ini(self, text):
        with open(apply_config.SNAPPYMAIL_INI, "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write(text)

    def read_ini(self):
        with open(apply_config.SNAPPYMAIL_INI, encoding="utf-8") as handle:
            return handle.read()

    def test_requires_webmail_host(self):
        ok, _, err = self.run_webmail(
            VALID.replace("webmail_host   = webmail.example.com",
                          "webmail_host   ="))
        self.assertIsNone(ok)
        self.assertIn("webmail_host", err)

    def test_patches_the_ini_and_never_prints_the_key(self):
        self.write_ini(SHIPPED_INI)
        ok, out, err = self.run_webmail()
        self.assertTrue(ok)
        key = apply_config.admin_key()
        ini = self.read_ini()
        self.assertIn('key = "%s"' % key, ini)
        self.assertIn("http_client_ip_check_proxy = On", ini)
        self.assertNotIn(key, out + err)
        self.assertIn("webmail  admin panel key generated "
                      "(snappymail-admin-key.txt)", out)
        self.assertIn("webmail  client IP taken from the proxy", out)

        ok, out, _ = self.run_webmail()
        self.assertIn("webmail  admin panel key kept", out)
        self.assertEqual(self.read_ini(), ini)

    def test_blank_key_file_is_refused(self):
        self.write_ini(SHIPPED_INI)
        with open(apply_config.ADMIN_KEY_FILE, "w", encoding="utf-8") as handle:
            handle.write("  \n")
        ok, _, err = self.run_webmail()
        self.assertIsNone(ok)
        self.assertIn("snappymail-admin-key.txt", err)
        self.assertIn("is empty", err)
        self.assertEqual(self.read_ini(), SHIPPED_INI)

    def test_creates_the_auth_log_fail2ban_reads(self):
        # fail2ban refuses a jail whose log file does not exist, and
        # SnappyMail writes it only on the first failed sign-in.
        self.write_ini(SHIPPED_INI)
        ok, _, _ = self.run_webmail()
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(apply_config.SNAPPYMAIL_AUTH_LOG))

        with open(apply_config.SNAPPYMAIL_AUTH_LOG, "a",
                  encoding="utf-8") as handle:
            handle.write("[2026-10-30 12:00:00] Auth failed: ip=203.0.113.7\n")
        self.run_webmail()
        with open(apply_config.SNAPPYMAIL_AUTH_LOG, encoding="utf-8") as handle:
            self.assertIn("ip=203.0.113.7", handle.read())

    def test_unexpected_ini_is_left_alone(self):
        broken = SHIPPED_INI.replace('key = "admin"\n', "")
        self.write_ini(broken)
        ok, _, _ = self.run_webmail()
        self.assertIsNone(ok)
        self.assertEqual(self.read_ini(), broken)

    def test_missing_ini_triggers_snappymail_then_gives_up(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        sleeps = []
        with mock.patch.object(apply_config.subprocess, "run", fake_run), \
                mock.patch.object(apply_config.time, "sleep", sleeps.append):
            ok, _, err = self.run_webmail()
        self.assertIsNone(ok)
        self.assertIn("application.ini", err)
        self.assertTrue(any(args[:3] == ["docker", "exec", "snappymail"]
                            for args in calls))
        self.assertLessEqual(sum(sleeps), 60)
        self.assertGreater(sum(sleeps), 0)


# The current stalwart-cli refuses `create Action/...` without a body.
RELOAD = ("create", "Action/ReloadSettings", "--json", "{}")


class TestApplyReload(ConfigCase):

    def test_apply_ends_with_a_reload_that_carries_a_body(self):
        calls = []
        replies = {"Domain": "Id  Name\n1  example.com\n2  example.net\n",
                   "Account": "Id  Name\n5  info@example.com\n"
                              "6  sales@example.com\n"}

        def fake_cli(*args):
            calls.append(args)
            return replies[args[1]] if args[0] == "query" else ""

        with mock.patch.object(apply_config, "cli", fake_cli),                 contextlib.redirect_stdout(io.StringIO()):
            apply_config.apply(self.read(VALID))
        self.assertEqual(calls[-1], RELOAD)


class TestAllowWebmail(ConfigCase):

    HEADER = "Id  IP Address(es)  Reason\n"

    def run_allow(self, query_output):
        calls = []

        def fake_cli(*args):
            calls.append(args)
            return query_output if args[:1] == ("query",) else ""

        out = io.StringIO()
        with mock.patch.object(apply_config, "cli", fake_cli), \
                contextlib.redirect_stdout(out):
            apply_config.allow_webmail(self.read(VALID))
        return calls, out.getvalue()

    def test_creates_the_exception_once(self):
        calls, out = self.run_allow(self.HEADER + "7  10.0.0.1  other\n")
        create = [c for c in calls if c[:2] == ("create", "AllowedIp")]
        self.assertEqual(len(create), 1)
        self.assertIn("address=172.16.89.10", create[0])
        self.assertIn("reason=SnappyMail webmail; brute force is banned by "
                      "fail2ban on the host", create[0])
        self.assertEqual(calls[-1], RELOAD)
        self.assertRegex(out, r"allowip  172\.16\.89\.10 +created")

    def test_existing_exception_is_not_duplicated(self):
        calls, out = self.run_allow(
            self.HEADER + "9  172.16.89.10  SnappyMail webmail\n")
        self.assertFalse([c for c in calls if c[:2] == ("create", "AllowedIp")])
        self.assertEqual(calls[-1], RELOAD)
        self.assertRegex(out, r"allowip  172\.16\.89\.10 +exists")


class TestUsage(unittest.TestCase):

    def test_usage_lists_every_subcommand(self):
        err = io.StringIO()
        with mock.patch.object(apply_config.sys, "argv", ["apply-config.py"]), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            apply_config.main()
        self.assertIn("apply-config.py render|apply|webmail|allow-webmail",
                      err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
