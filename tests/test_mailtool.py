import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import mailtool as m


class ParsingTests(unittest.TestCase):
    def test_supplier_format_bom_and_deduplication(self):
        accounts = m.parse_accounts("\ufeffa@example.com----password-one----client----old\n\nA@example.com----password-two----client----new")
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].refresh_token, "new")
        self.assertNotIn("new", repr(accounts[0]))
        self.assertNotIn("password", repr(accounts[0]))

    def test_invalid_line_does_not_disclose_credentials(self):
        with self.assertRaises(m.MailError) as context:
            m.parse_accounts("a@example.com----super-secret")
        self.assertIn("第 1 行", str(context.exception))
        self.assertNotIn("super-secret", str(context.exception))

    def test_multilingual_codes_and_sentence_punctuation(self):
        for text, expected in [
            ("您的验证码是123456，请勿泄露。", "123456"),
            ("Your verification code is 654321.", "654321"),
            ("Security code: 123 456", "123456"),
            ("Security code: 1234 5678", "12345678"),
            ("Your verification code is G-987654", "987654"),
            ("登录验证码：A2B3C4", "A2B3C4"),
            ("验证码：2024", "2024"),
        ]:
            with self.subTest(text=text):
                result = m.extract_code("", text)
                self.assertEqual(result["code"], expected)
                self.assertEqual(result["code_confidence"], "high")

    def test_avoid_order_ids_phone_numbers_and_dates(self):
        result = m.extract_code("", "订单 123456。您的验证码：765432，有效期 10 分钟。")
        self.assertEqual(result["code"], "765432")
        self.assertEqual(m.extract_code("账单", "电话 13812345678，日期 2026-11-06，金额 123456.78")["code"], "")
        self.assertEqual(m.extract_code("通知", "参考编号 123456")["code_confidence"], "possible")

    def test_mime_prefers_plain_and_decodes_chinese_and_internal_date(self):
        message = EmailMessage()
        message["Subject"] = "登录验证码"
        message["From"] = "登录中心 <security@example.com>"
        message.set_content("您的验证码是 456789", charset="gb2312")
        message.add_alternative("<style>111111</style><p>验证码：999999</p>", subtype="html")
        message.add_attachment(b"verification code 777777", maintype="application", subtype="octet-stream", filename="code.txt")
        result = m.parse_mime(message.as_bytes(), "10", "垃圾箱", "06-Nov-2026 09:00:00 +0800")
        self.assertEqual(result["code"], "456789")
        self.assertEqual(result["subject"], "登录验证码")
        self.assertEqual(result["received_at"], "2026-11-06T01:00:00+00:00")

    def test_html_does_not_load_remote_content_or_keep_script(self):
        text = m.html_to_text('<head><style>123456</style></head><p>验证码：654321</p><script>111111</script><img src="https://tracking.invalid">')
        self.assertEqual(text, "验证码：654321")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = m.Store(Path(self.temp.name) / "accounts.sqlite3")
        self.store.import_accounts(m.parse_accounts("a@example.com----not-saved-password----client----old-refresh-secret"))
        self.account_id = self.store.accounts()[0]["id"]
        self.service = m.MailService(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_private_storage_and_public_api_never_expose_credentials(self):
        if os.name == "posix":
            self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        self.assertNotIn("secret", json.dumps(self.store.accounts()))
        self.assertNotIn(b"not-saved-password", self.store.path.read_bytes())

    def test_database_connection_closes_after_transaction(self):
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

    def test_token_rotation_saved_even_if_mail_fetch_fails(self):
        with patch.object(m, "refresh_access", return_value={"access_token": "access-secret", "refresh_token": "rotated-secret"}), patch.object(m, "graph_messages", side_effect=m.MailError("暂时不可用")):
            with self.assertRaises(m.MailError):
                self.service.fetch(self.account_id, "graph")
        self.assertEqual(self.store.account(self.account_id).refresh_token, "rotated-secret")
        self.assertNotIn("secret", json.dumps(self.store.cached(self.account_id)))

    def test_poll_reuses_access_token_and_reimport_invalidates_it(self):
        with patch.object(m, "refresh_access", return_value={"access_token": "access-secret", "refresh_token": "new-refresh", "expires_in": 3600}) as refresh, patch.object(m, "graph_messages", return_value=([], [])):
            self.service.fetch(self.account_id, "graph")
            self.service.fetch(self.account_id, "graph")
            self.assertEqual(refresh.call_count, 1)
            self.store.import_accounts(m.parse_accounts("a@example.com----password----client----reimported"))
            self.service.fetch(self.account_id, "graph")
            self.assertEqual(refresh.call_count, 2)

    def test_one_mailbox_network_operation_at_a_time(self):
        lock = self.service.account_locks.setdefault(self.account_id, threading.Lock())
        lock.acquire()
        try:
            with self.assertRaisesRegex(m.MailError, "正在收信"):
                self.service.fetch(self.account_id, "graph")
        finally:
            lock.release()

    def test_mail_from_both_folders_sorted_by_actual_received_time(self):
        messages = [{"id": "older", "received_at": "2026-11-06T01:00:00Z"},
                    {"id": "newer", "received_at": "2026-11-06T01:00:00.500Z"}]
        with patch.object(m, "refresh_access", return_value={"access_token": "access"}), patch.object(m, "graph_messages", return_value=(messages, [])):
            result = self.service.fetch(self.account_id, "graph")
        self.assertEqual([message["id"] for message in result["messages"]], ["newer", "older"])

    def test_imap_numeric_uid_order_readonly_and_peek(self):
        message = EmailMessage()
        message["Subject"] = "Verification code"
        message.set_content("Your code: 123456")
        class FakeImap:
            def __init__(self):
                self.selected = []
                self.fetched = []
                self.logged_out = False
            def authenticate(self, mechanism, callback):
                self.auth = callback(b"")
            def list(self):
                return "OK", [b'(\\Junk) "/" "Junk Email"']
            def select(self, name, readonly=False):
                self.selected.append((name, readonly))
                return "OK", [b"3"]
            def uid(self, command, *args):
                if command == "search":
                    return "OK", [b"9 10 100"]
                self.fetched.append(args)
                return "OK", [(b'1 (INTERNALDATE "06-Nov-2026 01:00:00 +0000" BODY[] {100}', message.as_bytes()), b")"]
            def logout(self):
                self.logged_out = True
        fake = FakeImap()
        with patch.object(m.imaplib, "IMAP4_SSL", return_value=fake):
            messages, warnings = m.imap_messages("a@example.com", "access-secret")
        self.assertEqual([args[0] for args in fake.fetched[:3]], [b"100", b"10", b"9"])
        self.assertTrue(all(readonly for _, readonly in fake.selected))
        self.assertTrue(all("BODY.PEEK[]" in args[1] for args in fake.fetched))
        self.assertEqual(fake.selected[1][0], '"Junk Email"')
        self.assertTrue(fake.logged_out)
        self.assertEqual(len(messages), 6)
        self.assertFalse(warnings)

    def test_graph_checks_inbox_and_junk_without_changing_read_status(self):
        response = {"value": [{"id": "one", "subject": "验证码", "body": {"contentType": "html", "content": "<p>验证码 987654</p>"}, "receivedDateTime": "2026-11-06T01:00:00Z"}]}
        with patch.object(m, "read_json", return_value=response) as request:
            messages, warnings = m.graph_messages("access-secret")
        urls = [call.args[0].full_url for call in request.call_args_list]
        self.assertIn("/inbox/messages?", urls[0])
        self.assertIn("/junkemail/messages?", urls[1])
        self.assertTrue(all(call.args[0].get_method() == "GET" for call in request.call_args_list))
        self.assertEqual(messages[0]["code"], "987654")
        self.assertFalse(warnings)

    def test_oauth_error_is_sanitized(self):
        body = io.BytesIO(json.dumps({"error": "invalid_grant", "error_description": "refresh-secret private-user"}).encode())
        error = urllib.error.HTTPError(m.TOKEN_URL, 400, "Bad Request", {}, body)
        with patch.object(m.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(m.MailError) as result:
                m.refresh_access(self.store.account(self.account_id), "graph")
        self.assertIn("授权已失效", str(result.exception))
        self.assertNotIn("private-user", str(result.exception))
        self.assertNotIn("secret", str(result.exception))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = m.Store(Path(self.temp.name) / "test.sqlite3")
        self.server = m.make_server(0, self.store)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = "http://127.0.0.1:%s" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.temp.cleanup()

    def test_import_does_not_fetch_and_secrets_are_not_public(self):
        bootstrap = json.load(urllib.request.urlopen(self.url + "/api/accounts"))
        request = urllib.request.Request(self.url + "/api/import", data=json.dumps({"text": "a@example.com----private-password----client----private-token"}).encode(),
                                         headers={"X-Mailtool-Token": bootstrap["token"], "Content-Type": "application/json"})
        with patch.object(self.server.service, "fetch") as fetch:
            result = json.load(urllib.request.urlopen(request))
        self.assertEqual(result["added"], 1)
        fetch.assert_not_called()
        public = urllib.request.urlopen(self.url + "/api/accounts").read().decode()
        self.assertNotIn("private-", public)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.url + "/.mailtool/accounts.sqlite3")
        self.assertEqual(error.exception.code, 404)

    def test_cross_site_and_rebinding_requests_are_rejected(self):
        requests = [
            urllib.request.Request(self.url + "/api/import", data=b"{}", headers={"Origin": "https://evil.invalid"}),
            urllib.request.Request(self.url + "/api/accounts", headers={"Host": "evil.invalid"}),
        ]
        for request in requests:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)

    def test_batch_api_lifecycle_and_delete_restore(self):
        bootstrap = json.load(urllib.request.urlopen(self.url + "/api/accounts"))
        def post(path, data):
            request = urllib.request.Request(self.url + path, data=json.dumps(data).encode(),
                headers={"X-Mailtool-Token": bootstrap["token"], "Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(request))
        post("/api/import", {"text": "first@example.com----x----c----t\nsecond@example.com----x----c----t"})
        with patch.object(self.server.service, "fetch", return_value={"messages": [], "checked_at": m.utc_now(), "warnings": []}):
            result = post("/api/monitor/start", {"ids": [1, 2], "interval": 30, "protocol": "graph"})
            self.assertTrue(result["running"])
            post("/api/accounts/action", {"ids": [1], "action": "delete"})
            status = json.load(urllib.request.urlopen(self.url + "/api/monitor"))
            self.assertEqual(status["ids"], [2])
            trash = json.load(urllib.request.urlopen(self.url + "/api/accounts?trash=1"))
            self.assertEqual(trash["accounts"][0]["number"], 1)
            post("/api/accounts/action", {"ids": [1], "action": "restore"})
            self.assertEqual([a["number"] for a in json.load(urllib.request.urlopen(self.url + "/api/accounts"))["accounts"]], [1, 2])
            self.assertFalse(post("/api/monitor/stop", {})["running"])
            self.server.monitor.executor.shutdown(wait=True)

    def test_permanent_delete_api_requires_confirmation_and_trash_membership(self):
        bootstrap = json.load(urllib.request.urlopen(self.url + "/api/accounts"))
        def post(data):
            request = urllib.request.Request(self.url + "/api/accounts/action", data=json.dumps(data).encode(),
                headers={"X-Mailtool-Token": bootstrap["token"], "Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(request))
        self.store.import_accounts(m.parse_accounts("purge@example.com----x----c----private-token"))
        with self.assertRaises(urllib.error.HTTPError) as error:
            post({"ids": [1], "action": "purge", "confirmed": True})
        self.assertEqual(error.exception.code, 400)
        post({"ids": [1], "action": "delete"})
        for confirmed in (False, "true"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                post({"ids": [1], "action": "purge", "confirmed": confirmed})
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(len(self.store.accounts(trash=True)), 1)
        self.server.service.token_cache[(1, "graph")] = ("private-access", 1, "private-refresh")
        self.assertEqual(post({"ids": [1], "action": "purge", "confirmed": True})["changed"], 1)
        self.assertEqual(self.store.accounts(trash=True), [])
        self.assertFalse(self.server.service.token_cache)


if __name__ == "__main__":
    unittest.main()
