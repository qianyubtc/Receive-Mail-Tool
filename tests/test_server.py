import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import mailtool as mail
import mailtool_server as api


def message(watch, key, code, *, age=0, confidence="high"):
    return {
        "id": key,
        "code": code,
        "code_confidence": confidence,
        "received_at": datetime.fromtimestamp(watch.since - age, timezone.utc).isoformat(),
    }


def wait_until(check, seconds=4):
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError("The expected state was not reached")
        time.sleep(0.01)


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ServerTests(unittest.TestCase):
    def test_web_import_settings_and_manual_receive_use_the_shared_monitor(self):
        defaults = {"protocol": "graph", "interval": 2, "timeout": 120, "webhook_url": "", "webhook_secret": ""}
        service = Mock()
        service.authorize.return_value = ("demo@example.com", "fixture-access")
        received_at = mail.utc_now()
        incoming = {"id": "new", "code": "042681", "code_confidence": "high", "received_at": received_at}
        with tempfile.TemporaryDirectory() as directory:
            store = mail.Store(Path(directory) / "server.sqlite3")
            settings = api.ServerSettings(store, service, defaults)
            monitor = api.SingleMailboxMonitor(*settings.runtime())
            self.addCleanup(monitor.close)
            server = api.make_api_server(0, store, monitor, "fixture-api-key",
                                         admin_token="fixture-admin-key", settings=settings)
            with running(server) as base, patch.object(mail, "graph_folder_messages") as fetch:
                def request(path, data=None, key="fixture-admin-key"):
                    body = json.dumps(data).encode() if data is not None else None
                    with urlopen(Request(base + path, body, {"Authorization": "Bearer " + key}), timeout=2) as response:
                        return response.status, json.load(response)

                with urlopen(base + "/admin", timeout=2) as response:
                    self.assertIn(b'/admin/admin.js', response.read())
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                with self.assertRaises(HTTPError) as denied:
                    request("/admin/api/accounts", key="fixture-api-key")
                self.assertEqual(denied.exception.code, 401)
                denied.exception.close()
                _, imported = request("/admin/api/import", {
                    "text": "demo@example.com----fixture-password----fixture-app----fixture-refresh"
                })
                self.assertEqual(imported["added"], 1)
                self.assertEqual(request("/admin/api/accounts")[1], {"accounts": [{"id": 1, "email": "demo@example.com"}]})

                def receive(*args):
                    return [{**incoming, "received_at": mail.utc_now()}]

                fetch.side_effect = receive
                _, started = request("/admin/api/watches", {"email": "demo@example.com"})
                watch_id = started["watch_id"]
                wait_until(lambda: request("/admin/api/watch")[1]["watch"]["status"] == "received")
                visible = request("/admin/api/watch")[1]["watch"]
                self.assertEqual(visible["watch_id"], watch_id)
                self.assertEqual(visible["code"], "042681")
                self.assertEqual(request("/v1/watches/" + watch_id, key="fixture-api-key")[1],
                                 {"watch_id": watch_id, "status": "received"})

                with self.assertRaises(HTTPError) as missing_webhook:
                    request("/v1/watches", {"email": "demo@example.com"}, key="fixture-api-key")
                self.assertEqual(missing_webhook.exception.code, 409)
                missing_webhook.exception.close()
                self.assertEqual(request("/admin/api/watch")[1]["watch"]["watch_id"], watch_id)
                secret = "fixture-webhook-signature-secret" * 2
                _, saved = request("/admin/api/settings", {
                    "interval": 1.5, "timeout": 40, "webhook_url": "https://hooks.example.com/code",
                    "webhook_secret": secret,
                })
                self.assertTrue(saved["webhook_ready"])
                self.assertNotIn(secret, json.dumps(saved))
                request("/admin/api/settings", {"webhook_secret": ""})
                restored = api.ServerSettings(store, service, defaults)
                self.assertEqual(restored.public()["interval"], 1.5)
                self.assertEqual(restored.runtime()[1].secret, secret.encode())

    def test_api_switches_mailboxes_and_retries_a_signed_code_only_webhook(self):
        old_started = threading.Event()
        old_finished = threading.Event()
        release_old = threading.Event()
        callbacks = []
        secret = "fixture-secret" * 3

        class Reader:
            def messages(self, watch):
                if watch.account_id == 1:
                    old_started.set()
                    try:
                        release_old.wait(3)
                        yield message(watch, "late-a", "111111")
                    finally:
                        old_finished.set()
                    return
                yield message(watch, "history", "222222", age=5)
                yield message(watch, "order-number", "333333", confidence="possible")
                yield message(watch, "new-b", "042681")
                yield message(watch, "more", "444444")

        class Callback(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                callbacks.append((dict(self.headers), body))
                self.send_response(503 if len(callbacks) == 1 else 204)
                self.end_headers()

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as directory:
            store = mail.Store(Path(directory) / "server.sqlite3")
            store.import_accounts(mail.parse_accounts(
                "first@example.com----unused----fixture-app----fixture-refresh\n"
                "second@example.com----unused----fixture-app----fixture-refresh"
            ))
            with running(ThreadingHTTPServer(("127.0.0.1", 0), Callback)) as receiver:
                monitor = api.SingleMailboxMonitor(Reader(), api.Webhook(receiver + "/callback", secret))
                self.addCleanup(monitor.close)
                self.addCleanup(release_old.set)
                with running(api.make_api_server(0, store, monitor, "fixture-api-key")) as base:
                    def request(path, data=None, authenticated=True):
                        headers = {"Authorization": "Bearer fixture-api-key"} if authenticated else {}
                        body = json.dumps(data).encode() if data is not None else None
                        with urlopen(Request(base + path, body, headers), timeout=2) as response:
                            return response.status, json.load(response)

                    with self.assertRaises(HTTPError) as unauthorized:
                        request("/v1/watches", {"email": "first@example.com"}, authenticated=False)
                    self.assertEqual(unauthorized.exception.code, 401)
                    unauthorized.exception.close()
                    status, first = request("/v1/watches", {"email": "first@example.com"})
                    self.assertEqual(status, 202)
                    self.assertTrue(old_started.wait(1))
                    with self.assertRaises(HTTPError) as missing:
                        request("/v1/watches", {"email": "unknown@example.com"})
                    self.assertEqual(missing.exception.code, 404)
                    missing.exception.close()
                    first_path = "/v1/watches/" + first["watch_id"]
                    self.assertEqual(request(first_path)[1]["status"], "listening")
                    status, second = request("/v1/watches", {"email": "SECOND@example.com"})
                    self.assertEqual(status, 202)
                    self.assertNotEqual(second["watch_id"], first["watch_id"])
                    self.assertEqual(request(first_path)[1]["status"], "replaced")
                    wait_until(lambda: len(callbacks) == 1)
                    self.assertFalse(old_finished.is_set())
                    release_old.set()
                    self.assertTrue(old_finished.wait(1))
                    second_path = "/v1/watches/" + second["watch_id"]
                    wait_until(lambda: request(second_path)[1]["status"] == "delivered")
                    self.assertEqual(request(second_path)[1], {"watch_id": second["watch_id"], "status": "delivered"})
                    self.assertEqual(len(callbacks), 2)
                    for headers, body in callbacks:
                        headers = {k.lower(): v for k, v in headers.items()}
                        self.assertEqual(headers["x-watch-id"], second["watch_id"])
                        self.assertEqual(json.loads(body), {"code": "042681"})
                        signed = (headers["x-webhook-timestamp"] + "." + second["watch_id"] + ".").encode() + body
                        digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
                        self.assertEqual(headers["x-webhook-signature"], "sha256=" + digest)

    def test_replacement_stops_webhook_retry_and_expiry_does_not_push(self):
        class Reader:
            def messages(self, watch):
                if watch.account_id == 1:
                    yield message(watch, "code-a", "123456")
                else:
                    watch.wait(5)

        webhook = Mock()
        first_attempt = threading.Event()

        def send(*_):
            first_attempt.set()
            return False

        webhook.send.side_effect = send
        monitor = api.SingleMailboxMonitor(Reader(), webhook, timeout=0.2)
        self.addCleanup(monitor.close)
        first = monitor.start(1)
        self.assertTrue(first_attempt.wait(1))
        second = monitor.start(2)
        wait_until(lambda: monitor.status(second) == "timeout")
        self.assertEqual(monitor.status(first), "replaced")
        monitor.close()
        self.assertEqual(webhook.send.call_count, 1)

    def test_imap_idle_wakes_for_new_uid_and_does_not_fetch_old_body(self):
        watch = api.Watch("fixture", 1, int(time.time()), time.monotonic() + 5)
        new_date = datetime.fromtimestamp(watch.since, timezone.utc).strftime("%d-%b-%Y %H:%M:%S %z")
        old_date = "01-Jan-2020 12:00:00 +0000"
        mime = EmailMessage()
        mime["Subject"] = "Verification code"
        mime["Message-ID"] = "<fixture@example.com>"
        mime.set_content("Your verification code is 042681.")
        client = Mock(capabilities=("IMAP4REV1", "IDLE"))
        client.response.return_value = ("UIDVALIDITY", [b"1"])
        client.list.return_value = ("OK", [])
        arrived = False

        def select(name, *, readonly):
            self.assertTrue(readonly)
            if name != "INBOX":
                return "NO", []
            return "OK", [b"2" if arrived else b"1"]

        def uid(command, *args):
            if command == "search":
                return "OK", [b"1 2" if arrived else b"1"]
            if args[-1] == "(UID INTERNALDATE)":
                key, date = (2, new_date) if arrived else (1, old_date)
                return "OK", [f'1 (UID {key} INTERNALDATE "{date}")'.encode()]
            self.assertEqual(args, (b"2", "(BODY.PEEK[])"))
            return "OK", [(b"2", mime.as_bytes())]

        @contextmanager
        def idle(*, duration):
            nonlocal arrived
            arrived = True
            yield iter([("EXISTS", [b"2"])])

        client.select.side_effect = select
        client.uid.side_effect = uid
        client.idle.side_effect = idle
        service = Mock()
        service.authorize.return_value = ("first@example.com", "fixture-access")
        with patch.object(mail, "imap_connect", return_value=client):
            with api.closing(api.MailReader(service, "imap", 2).messages(watch)) as messages:
                self.assertEqual(next(messages)["code"], "042681")
        client.idle.assert_called_once()
        client.shutdown.assert_called_once()

    def test_auto_uses_graph_if_imap_cannot_authenticate(self):
        watch = api.Watch("fixture", 1, int(time.time()), time.monotonic() + 5)
        service = Mock()
        service.authorize.side_effect = [mail.MailError("IMAP 未获得读取权限"), ("first@example.com", "graph-access")]
        with patch.object(mail, "graph_folder_messages", return_value=[message(watch, "new", "042681")]) as fetch:
            with api.closing(api.MailReader(service, "auto", 2).messages(watch)) as messages:
                self.assertEqual(next(messages)["code"], "042681")
        self.assertEqual([call.args[1] for call in service.authorize.call_args_list], ["imap", "graph"])
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[1], "inbox")


if __name__ == "__main__":
    unittest.main()
