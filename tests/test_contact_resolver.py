#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("contact_resolver", ROOT / "phase2" / "contact_resolver.py")
assert SPEC and SPEC.loader
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)


class ContactResolverTests(unittest.TestCase):
    def make_snapshot(self, conversations, users, relations=()):
        root = Path(tempfile.mkdtemp(prefix="contact-resolver-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        with sqlite3.connect(root / "session.db") as connection:
            connection.executescript(
                """
                CREATE TABLE conversation_table(id TEXT, name TEXT, last_message_time INTEGER, last_message_id INTEGER);
                CREATE TABLE conversation_user_table(conversation_id TEXT, user_id INTEGER, nick_name TEXT);
                """
            )
            connection.executemany(
                "INSERT INTO conversation_table VALUES (?, ?, ?, ?)",
                [(cid, name, time, index) for index, (cid, name, time) in enumerate(conversations, 1)],
            )
        with sqlite3.connect(root / "user.db") as connection:
            connection.executescript(
                """
                CREATE TABLE user_table(id INTEGER, name TEXT, real_name TEXT, account TEXT, external_corp_name TEXT);
                CREATE TABLE external_user_relation_v3(user_id INTEGER, remarks TEXT, real_remarks TEXT, corp_remark TEXT);
                """
            )
            connection.executemany("INSERT INTO user_table VALUES (?, ?, ?, ?, ?)", users)
            connection.executemany("INSERT INTO external_user_relation_v3 VALUES (?, ?, ?, ?)", relations)
        return root

    def test_confident_self_inference_and_contact_resolution(self):
        snapshot = self.make_snapshot(
            [
                ("S:1_2", "S:1_2", 3),
                ("S:1_3", "S:1_3", 2),
                ("S:1_4", "S:1_4", 1),
            ],
            [
                (1, "本人", "本人", "self", ""),
                (2, "联系人甲", "", "a", ""),
                (3, "联系人乙", "", "b", ""),
                (4, "联系人丙", "", "c", ""),
            ],
        )

        result = resolver.resolve_sessions(snapshot, 10)
        names = {item["display_name"] for item in result["sessions"]}

        self.assertEqual(result["count"], 3)
        self.assertEqual(names, {"联系人甲", "联系人乙", "联系人丙"})
        self.assertTrue(all(not item["display_name"].startswith("S:") for item in result["sessions"]))

    def test_external_remark_has_priority(self):
        snapshot = self.make_snapshot(
            [("S:1_2", "S:1_2", 1), ("S:1_3", "S:1_3", 2)],
            [
                (1, "本人", "本人", "self", ""),
                (2, "原姓名", "", "a", ""),
                (3, "另一个人", "", "b", ""),
            ],
            [(2, "普通备注", "真实备注", "企业备注")],
        )

        result = resolver.resolve_sessions(snapshot, 10)
        names = {item["display_name"] for item in result["sessions"]}

        self.assertIn("真实备注", names)

    def test_unknown_alias_is_stable_and_does_not_expose_ids(self):
        snapshot = self.make_snapshot([("S:99_100", "S:99_100", 1)], [])

        first = resolver.resolve_sessions(snapshot, 10)
        second = resolver.resolve_sessions(snapshot, 10)
        first_name = first["sessions"][0]["display_name"]

        self.assertEqual(first_name, second["sessions"][0]["display_name"])
        self.assertTrue(first_name.startswith("未识别单聊 #"))
        self.assertNotIn("99", first_name)
        self.assertNotIn("100", first_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
