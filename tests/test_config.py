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
import tempfile
import unittest

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
        shutil.copytree(os.path.join(ROOT, "compose"), self.compose)
        self._saved_compose = apply_config.COMPOSE
        apply_config.COMPOSE = self.compose
        self.addCleanup(setattr, apply_config, "COMPOSE", self._saved_compose)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
