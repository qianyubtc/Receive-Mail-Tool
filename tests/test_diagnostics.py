import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import mailtool as m


class DiagnosticTests(unittest.TestCase):
    def test_strict_log_allowlist_discards_private_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnostics.jsonl"
            log = m.DiagnosticLog(path)
            log.emit("fetch_failed", account_id=1, protocol="imap", stage="read_messages",
                     reason="message_parse_error", error_type="ValueError",
                     email="private@example.com", password="private-password",
                     refresh_token="private-refresh", code="654321", body="private-mail-content")
            public = json.dumps(log.read())
            log.close()
            for private in ("private@example.com", "private-password", "private-refresh", "654321", "private-mail-content"):
                self.assertNotIn(private, public)
                self.assertNotIn(private, path.read_text())
            self.assertIn("message_parse_error", public)

    def test_fetch_failure_logs_phase_and_reason_without_raw_error(self):
        with tempfile.TemporaryDirectory() as directory:
            store = m.Store(Path(directory) / "test.sqlite3")
            store.import_accounts(m.parse_accounts("private@example.com----x----c----private-refresh"))
            log = m.DiagnosticLog()
            service = m.MailService(store, log)
            with patch.object(m, "refresh_access", side_effect=m.MailError("invalid_grant private-refresh")):
                with self.assertRaises(m.MailError):
                    service.fetch(1, "imap")
            failure = log.read()["errors"][0]
            self.assertEqual(failure["stage"], "token_refresh")
            self.assertEqual(failure["reason"], "authorization_rejected")
            self.assertNotIn("private-refresh", json.dumps(log.read()))

    def test_only_loopback_and_private_ipv4_clients_are_allowed(self):
        for address in ("127.0.0.1", "192.168.100.25", "10.1.2.3", "172.16.1.2"):
            self.assertTrue(m.private_client(address))
        for address in ("8.8.8.8", "203.0.113.1", "0.0.0.0", "172.32.1.2", "invalid"):
            self.assertFalse(m.private_client(address))

    def test_success_records_code_lengths_and_partial_folder_failure_without_content(self):
        with tempfile.TemporaryDirectory() as directory:
            store = m.Store(Path(directory) / "test.sqlite3")
            store.import_accounts(m.parse_accounts("private@example.com----x----c----private-refresh"))
            log = m.DiagnosticLog()
            service = m.MailService(store, log)
            message = {"id": "one", "code": "654321", "body": "private-body", "received_at": m.utc_now()}
            with patch.object(m, "refresh_access", return_value={"access_token": "private-access"}), patch.object(m, "graph_messages", return_value=([message], ["垃圾箱：当前应用未获得此协议的读取权限"])):
                service.fetch(1, "graph")
            data = log.read()
            complete = next(item for item in data["entries"] if item["event"] == "fetch_completed")
            self.assertEqual((complete["code_count"], complete["code_length_min"], complete["warning_count"]), (1, 6, 1))
            self.assertEqual(data["errors"][0]["folder"], "junk")
            self.assertEqual(data["errors"][0]["reason"], "permission_denied")
            self.assertNotIn("private-", json.dumps(data))

    def test_imap_authentication_stage_is_identified_without_raw_response(self):
        client = unittest.mock.Mock()
        client.authenticate.side_effect = m.imaplib.IMAP4.error("private-auth-response")
        with patch.object(m.imaplib, "IMAP4_SSL", return_value=client):
            with self.assertRaises(m.MailError) as caught:
                m.imap_messages("private@example.com", "private-access")
        self.assertEqual(caught.exception.imap_stage, "authenticate")
        self.assertNotIn("private", str(caught.exception))


class DiagnosticHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = m.Store(Path(self.temp.name) / "test.sqlite3")
        self.app = m.make_server(0, self.store)
        self.remote = m.make_diagnostic_server(0, self.app, "127.0.0.1")
        self.worker = threading.Thread(target=self.remote.serve_forever, daemon=True)
        self.worker.start()
        self.url = "http://127.0.0.1:%s" % self.remote.server_port

    def tearDown(self):
        self.remote.shutdown()
        self.remote.server_close()
        self.worker.join()
        self.app.server_close()
        self.app.diagnostics.close()
        self.temp.cleanup()

    def request(self, path, key=None, data=None):
        headers = {"Authorization": "Bearer " + key} if key is not None else {}
        return urllib.request.urlopen(urllib.request.Request(self.url + path, headers=headers, data=data))

    def test_health_has_version_without_exposing_access_key(self):
        with self.request("/health") as response:
            result = response.read().decode()
        self.assertIn(m.APP_VERSION, result)
        self.assertNotIn(self.remote.access_key, result)

    def test_logs_require_key_and_only_return_sanitized_read_only_data(self):
        for key in (None, "wrong-key", "café"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("/api/logs", key)
            self.assertEqual(caught.exception.code, 401)
        self.store.import_accounts(m.parse_accounts("private@example.com----password----client----private-token"))
        self.app.diagnostics.emit("fetch_completed", account_id=1, protocol="imap", message_count=1, code_count=1)
        with self.request("/api/logs", self.remote.access_key) as response:
            data = response.read().decode()
        self.assertIn("fetch_completed", data)
        for secret in ("private@example.com", "private-token", self.remote.access_key):
            self.assertNotIn(secret, data)
        for path, data, expected in (("/api/accounts", None, 404), ("/api/logs", b"{}", 405)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path, self.remote.access_key, data)
            self.assertEqual(caught.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
