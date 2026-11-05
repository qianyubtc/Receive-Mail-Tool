import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import mailtool as m


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for background work")


class StoreBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = m.Store(Path(self.temp.name) / "accounts.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, count=3):
        self.store.import_accounts(m.parse_accounts("\n".join(
            "mail%s@example.com----ignored----client----token%s" % (i, i) for i in range(count))))

    def test_1000_accounts_keep_import_order_and_numbers_reflow_after_delete(self):
        self.add(1000)
        accounts = self.store.accounts()
        self.assertEqual(len(accounts), 1000)
        self.assertEqual([a["number"] for a in accounts[:3]], [1, 2, 3])
        self.assertEqual([a["email"] for a in accounts[:3]], ["mail0@example.com", "mail1@example.com", "mail2@example.com"])
        self.store.update_accounts([1, 1000], "delete")
        self.store.import_accounts(m.parse_accounts("new@example.com----ignored----client----newtoken"))
        self.assertEqual(self.store.accounts()[-1]["id"], 1001)
        self.assertEqual([a["number"] for a in self.store.accounts()], list(range(1, 1000)))
        self.assertEqual(len(self.store.accounts(trash=True)), 2)
        self.store.update_accounts([1], "restore")
        self.assertEqual(self.store.accounts()[0]["number"], 1)
        self.assertEqual([a["number"] for a in self.store.accounts()], list(range(1, 1001)))

    def test_delete_is_recoverable_and_never_returns_deleted_credentials(self):
        self.add()
        account = self.store.account(1)
        self.store.update_accounts([1], "delete")
        with self.assertRaises(m.MailError):
            self.store.account(1)
        with self.assertRaises(m.MailError):
            self.store.cached(1)
        self.assertNotIn("refresh_token", json.dumps(self.store.accounts(trash=True)))
        self.store.update_accounts([1], "restore")
        self.assertEqual(self.store.account(1).refresh_token, account.refresh_token)

    def test_first_batch_used_leaves_next_20_in_order(self):
        self.add(40)
        self.store.update_accounts(list(range(1, 21)), "used")
        unused = [a for a in self.store.accounts() if not a["used"]]
        self.assertEqual([a["number"] for a in unused], list(range(21, 41)))

    def test_existing_database_migrates_without_losing_accounts(self):
        path = Path(self.temp.name) / "old.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,email TEXT UNIQUE COLLATE NOCASE NOT NULL,
                client_id TEXT NOT NULL,refresh_token TEXT NOT NULL,checked_at TEXT,
                error TEXT NOT NULL DEFAULT '',messages TEXT NOT NULL DEFAULT '[]',
                protocol TEXT NOT NULL DEFAULT '',warnings TEXT NOT NULL DEFAULT '[]')""")
            db.execute("INSERT INTO accounts(id,email,client_id,refresh_token) VALUES(7,'old@example.com','c','t')")
        migrated = m.Store(path)
        self.assertEqual(migrated.accounts()[0]["id"], 7)
        self.assertEqual(migrated.accounts()[0]["number"], 1)
        migrated.import_accounts(m.parse_accounts("new@example.com----x----c----t"))
        self.assertEqual(migrated.accounts()[-1]["id"], 8)
        self.assertEqual(migrated.accounts()[-1]["number"], 2)

    def test_display_numbers_and_codes_reflow_without_changing_mailbox_identity(self):
        self.add()
        started = m.utc_now()
        self.store.record_codes(2, [{"id": "code", "code": "123456", "received_at": m.utc_now()}], "graph", started)
        self.store.update_accounts([1], "delete")
        self.assertEqual([(a["id"], a["number"]) for a in self.store.accounts()], [(2, 1), (3, 2)])
        event = self.store.code_events([2], started)[0]
        self.assertEqual((event["account_id"], event["number"], event["email"]), (2, 1, "mail1@example.com"))
        self.store.update_accounts([1], "restore")
        self.assertEqual(self.store.code_events([2], started)[0]["number"], 2)

    def test_empty_list_restarts_at_one_and_internal_ids_are_never_reused(self):
        self.add()
        self.store.update_accounts([1, 2, 3], "delete")
        self.store.import_accounts(m.parse_accounts("new@example.com----x----c----newtoken"))
        self.assertEqual([(a["id"], a["number"]) for a in self.store.accounts()], [(4, 1)])
        self.store.purge_accounts([1, 2, 3])
        self.store.update_accounts([4], "delete")
        self.store.purge_accounts([4])
        self.store.import_accounts(m.parse_accounts("again@example.com----x----c----othertoken"))
        self.assertEqual([(a["id"], a["number"]) for a in self.store.accounts()], [(5, 1)])
        with self.assertRaises(m.MailError):
            self.store.save_messages(4, [], "graph", [])

    def test_permanent_delete_removes_credentials_cache_and_code_records(self):
        token = "purge-private-refresh-unique-382765"
        body = "purge-private-mail-body-unique-832765"
        self.store.import_accounts(m.parse_accounts("purge@example.com----x----c----" + token))
        message = {"id": "private", "code": "123456", "body": body, "received_at": m.utc_now()}
        self.store.save_messages(1, [message], "graph", [])
        self.store.record_codes(1, [message], "graph", m.utc_now())
        self.store.update_accounts([1], "delete")
        self.assertEqual(self.store.purge_accounts([1]), {"changed": 1})
        self.assertEqual(self.store.accounts(trash=True), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM code_events").fetchone()[0], 0)
        self.assertNotIn(token.encode(), self.store.path.read_bytes())
        self.assertNotIn(body.encode(), self.store.path.read_bytes())
        self.store.update_accounts([1], "restore")
        self.assertEqual(self.store.accounts(), [])

    def test_permanent_delete_rejects_active_or_missing_accounts_atomically(self):
        self.add()
        self.store.update_accounts([1], "delete")
        for ids in ([1, 2], [1, 999]):
            with self.assertRaises(m.MailError):
                self.store.purge_accounts(ids)
            self.assertEqual(len(self.store.accounts(trash=True)), 1)
            self.assertEqual(len(self.store.accounts()), 2)

    def test_inflight_result_cannot_restore_deleted_or_reimported_account(self):
        self.add()
        account = self.store.account(1)
        self.store.update_accounts([1], "delete")
        with self.assertRaises(m.MailError):
            self.store.save_messages(1, [], "graph", [], account.revision)
        self.store.import_accounts(m.parse_accounts("mail0@example.com----x----newclient----newtoken"))
        with self.assertRaises(m.MailError):
            self.store.rotate_token(account, "old-rotated-token")
        with self.assertRaises(m.MailError):
            self.store.save_messages(1, [], "graph", [], account.revision)
        self.assertEqual(self.store.account(1).refresh_token, "newtoken")

    def test_codes_deduplicated_and_recent_history_marked_separately(self):
        self.add()
        started = m.utc_now()
        recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        messages = [
            {"id": "new", "code": "123456", "received_at": m.utc_now(), "code_confidence": "high"},
            {"id": "recent", "code": "654321", "received_at": recent},
            {"id": "old", "code": "999999", "received_at": old},
        ]
        self.store.record_codes(1, messages, "graph", started)
        self.store.record_codes(1, messages, "graph", started)
        events = self.store.code_events([1], started)
        self.assertEqual(len(events), 2)
        self.assertEqual({event["code"] for event in events}, {"123456", "654321"})
        self.assertTrue(next(event for event in events if event["code"] == "123456")["is_new"])
        self.assertFalse(next(event for event in events if event["code"] == "654321")["is_new"])
        self.store.update_accounts([1], "delete")
        self.assertEqual(self.store.code_events([1], started), [])

    def test_busy_mailbox_cannot_push_other_mailboxes_out_of_feed(self):
        self.add()
        started = m.utc_now()
        self.store.record_codes(2, [{"id": "only", "code": "222222", "received_at": m.utc_now()}], "graph", started)
        self.store.record_codes(1, [{"id": str(i), "code": "111111", "received_at": m.utc_now()} for i in range(210)], "graph", started)
        events = self.store.code_events([1, 2], started)
        self.assertEqual({event["account_id"] for event in events}, {1, 2})
        self.assertEqual(len(events), 5)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = m.Store(Path(self.temp.name) / "accounts.sqlite3")
        self.store.import_accounts(m.parse_accounts("\n".join(
            "batch%s@example.com----x----c----t" % i for i in range(1000))))
        self.monitors = []

    def tearDown(self):
        for monitor in self.monitors:
            monitor.close()
            monitor.executor.shutdown(wait=True)
        self.temp.cleanup()

    def monitor(self, service, workers=8):
        monitor = m.BatchMonitor(self.store, service, workers=workers, stagger=0)
        self.monitors.append(monitor)
        return monitor

    def test_only_selected_20_polled_and_concurrency_bounded(self):
        calls, counters = [], {"active": 0, "peak": 0}
        lock = threading.Lock()
        store = self.store

        class FakeService:
            def fetch(self, account_id, protocol):
                with lock:
                    calls.append(account_id)
                    counters["active"] += 1
                    counters["peak"] = max(counters["peak"], counters["active"])
                try:
                    time.sleep(0.01)
                    return store.save_messages(account_id, [], protocol, [])
                finally:
                    with lock:
                        counters["active"] -= 1
        monitor = self.monitor(FakeService())
        monitor.start(list(range(21, 41)), interval=300)
        wait_for(lambda: monitor.status()["checked"] == 20)
        monitor.stop()
        self.assertEqual(set(calls), set(range(21, 41)))
        self.assertEqual(len(calls), 20)
        self.assertGreater(counters["peak"], 1)
        self.assertLessEqual(counters["peak"], 8)

    def test_rejects_21_and_invalid_ids_before_any_fetch(self):
        service = unittest.mock.Mock()
        monitor = self.monitor(service)
        for ids in (list(range(1, 22)), [1, 2000], [True], []):
            with self.assertRaises(m.MailError):
                monitor.start(ids)
        service.fetch.assert_not_called()

    def test_switch_batch_discards_late_old_batch_result(self):
        entered, release = threading.Event(), threading.Event()
        store = self.store
        class FakeService:
            def fetch(self, account_id, protocol):
                if account_id == 1:
                    entered.set()
                    release.wait(3)
                message = {"id": str(account_id), "code": "123456", "received_at": m.utc_now()}
                return store.save_messages(account_id, [message], protocol, [])
        monitor = self.monitor(FakeService())
        monitor.start([1], interval=300)
        self.assertTrue(entered.wait(2))
        monitor.start([2], interval=300)
        wait_for(lambda: monitor.status()["checked"] == 1)
        release.set()
        wait_for(lambda: not monitor.tasks)
        self.assertEqual([event["account_id"] for event in monitor.status()["events"]], [2])
        self.assertEqual(self.store.code_events([1], monitor.started_at), [])

    def test_rate_limited_account_backs_off_without_blocking_other_mailboxes(self):
        store = self.store
        class FakeService:
            def fetch(self, account_id, protocol):
                if account_id == 1:
                    raise m.MailError("限流", retry_after=123)
                return store.save_messages(account_id, [], protocol, [])
        monitor = self.monitor(FakeService())
        started = time.monotonic()
        monitor.start([1, 2], interval=5)
        wait_for(lambda: monitor.status()["checked"] == 2)
        status = monitor.status()
        self.assertEqual(status["results"][1]["retry_in"], 123)
        self.assertFalse(status["results"][2]["error"])
        self.assertGreaterEqual(monitor.due[1], started + 123)

    def test_deleted_account_removed_from_batch_while_other_continues(self):
        store = self.store
        class FakeService:
            def fetch(self, account_id, protocol):
                return store.save_messages(account_id, [], protocol, [])
        monitor = self.monitor(FakeService())
        monitor.start([1, 2], interval=300)
        wait_for(lambda: monitor.status()["checked"] == 2)
        self.store.update_accounts([1], "delete")
        monitor.remove([1])
        self.assertEqual(monitor.status()["ids"], [2])
        self.assertEqual(monitor.status()["checked"], 1)
        self.assertTrue(monitor.status()["running"])

    def test_retry_after_header_propagates_through_graph_folder_fetch(self):
        error = urllib.error.HTTPError("https://graph.microsoft.com", 429, "Too Many Requests",
                                       {"Retry-After": "120"}, io.BytesIO(b'{"error":{"code":"TooManyRequests"}}'))
        with patch.object(m.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(m.MailError) as caught:
                m.graph_messages("access-token")
        self.assertEqual(caught.exception.retry_after, 120)


if __name__ == "__main__":
    unittest.main()
