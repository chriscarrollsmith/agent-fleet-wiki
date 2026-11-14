"""Tests for deploy/rotate_credentials.py. No live AWS, SSH, BookStack, or Infisical calls."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rotate_credentials as rotate


class PureTests(unittest.TestCase):
    def test_email_states_the_breach_as_a_precaution(self) -> None:
        text = rotate.email_text("Frank", "https://wiki.example", "frank@example.com", "secret-value", "Chris")
        self.assertIn("I don't know that these credentials were exposed.", text)
        self.assertIn("Cursor had a data breach.", text)
        self.assertIn("This is a precaution.", text)
        self.assertIn("This email was written by an agent. Agents say dumb things.", text)
        self.assertIn("Password: secret-value", text)
        self.assertIn("https://wiki.example/login", text)
        self.assertIn("Hi Frank,", text)
        self.assertTrue(text.endswith("Chris\n"))

    def test_signoff_uses_the_from_display_name(self) -> None:
        self.assertEqual(rotate.signoff("Christopher Smith <chris@example.com>"), "Christopher")
        self.assertEqual(rotate.signoff("chris@example.com"), "chris")

    def test_key_pair_must_belong_to_one_instance(self) -> None:
        rotate.assert_single_instance(["i-abc"], "i-abc")
        with self.assertRaises(rotate.RotationError):
            rotate.assert_single_instance(["i-abc", "i-def"], "i-abc")
        with self.assertRaises(rotate.RotationError):
            rotate.assert_single_instance([], "i-abc")

    def test_replace_mode_drops_every_key_except_the_new_one(self) -> None:
        lines = rotate.authorized_key_lines(
            ["ssh-ed25519 OLD old", "ssh-ed25519 ORPHAN orphan", "ssh-ed25519 NEW new"],
            "ssh-ed25519 OLD old",
            "ssh-ed25519 NEW new",
            "replace",
        )
        self.assertEqual(lines, ["ssh-ed25519 NEW new"])
        with self.assertRaises(rotate.RotationError):
            rotate.authorized_key_lines(["ssh-ed25519 OLD old"], "ssh-ed25519 OLD old", "ssh-ed25519 NEW new", "replace")

    def test_add_mode_keeps_the_old_key(self) -> None:
        lines = rotate.authorized_key_lines(["ssh-ed25519 OLD old"], "ssh-ed25519 OLD old", "ssh-ed25519 NEW new", "add")
        self.assertEqual(lines, ["ssh-ed25519 OLD old", "ssh-ed25519 NEW new"])

    def test_service_token_project_and_propagation(self) -> None:
        self.assertEqual(rotate.service_token_project({"projectId": "project-1"}), "project-1")
        self.assertEqual(rotate.service_token_project({"workspace": "project-2"}), "project-2")
        with self.assertRaises(rotate.RotationError):
            rotate.service_token_project({})
        self.assertTrue(rotate.access_key_still_propagating("InvalidAccessKeyId: the key does not exist"))
        self.assertFalse(rotate.access_key_still_propagating("AccessDenied"))

    def test_env_replacement_quotes_spaces_and_preserves_other_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "user.env"
            path.write_text("EMAIL=a@example.com\nNAME=Ada Lovelace\nPASSWORD=old\n", encoding="utf-8")
            rotate.replace_env_keys(path, {"PASSWORD": "new value"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("EMAIL=a@example.com", text)
            self.assertIn("NAME=Ada Lovelace", text)
            self.assertIn("PASSWORD=$'new value'", text)
            self.assertEqual(rotate.load_env_file(path)["PASSWORD"], "new value")

    def test_json_lines_ignore_status_text(self) -> None:
        rows = rotate.json_lines('rotated later\n{"ok": true, "email": "a@example.com"}\n')
        self.assertEqual(rows, [{"ok": True, "email": "a@example.com"}])

    def test_chunks_cover_the_tail(self) -> None:
        self.assertEqual(rotate.chunks([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])


if __name__ == "__main__":
    unittest.main()
