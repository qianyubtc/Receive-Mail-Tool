#!/usr/bin/env python3
"""Single-mailbox verification code API and signed webhook delivery."""

import argparse
import hashlib
import hmac
import imaplib
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import mailtool as mail


IMAP_ERRORS = (imaplib.IMAP4.error, OSError, EOFError)
READ_ERRORS = (mail.MailError,) + IMAP_ERRORS
UID_PATTERN = re.compile(rb"\bUID (\d+)\b")
INTERNAL_DATE = re.compile(rb'\bINTERNALDATE "([^"]+)"')
LIVE_STATES = {"listening", "delivering"}
ADMIN_ASSETS = {
    "/admin": ("index.html", "text/html; charset=utf-8"),
    "/admin/": ("index.html", "text/html; charset=utf-8"),
    "/admin/admin.css": ("admin.css", "text/css; charset=utf-8"),
    "/admin/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
}


@dataclass
class Watch:
    id: str
    account_id: int
    since: int
    deadline: float
    cancel: threading.Event = field(default_factory=threading.Event)
    status: str = "listening"
    claimed: bool = False
    address: str = ""
    reader: object = None
    webhook: object = None
    code: str = field(default="", repr=False)
    code_deadline: float = 0

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def listening(self):
        return not self.cancel.is_set() and self.remaining() > 0

    def wait(self, delay):
        self.cancel.wait(min(delay, self.remaining()))


class MailReader:
    def __init__(self, service, protocol, interval):
        self.service = service
        self.protocol = protocol
        self.interval = interval

    def messages(self, watch):
        protocols = ("imap", "graph") if self.protocol == "auto" else (self.protocol,)
        for protocol in protocols:
            failures = 0
            while watch.listening():
                try:
                    address, token = self.service.authorize(watch.account_id, protocol)
                    if not watch.listening():
                        return
                    if protocol == "imap":
                        yield from self._imap(watch, address, token)
                        return
                    yield from self._graph(watch, token)
                    failures = 0
                    watch.wait(self.interval)
                except READ_ERRORS as exc:
                    failures += 1
                    reason = mail.diagnostic_reason(exc)
                    retry_after = getattr(exc, "retry_after", 0)
                    # Respect rate limits. Otherwise try Graph if IMAP cannot be used.
                    if not retry_after and protocol == "imap" and self.protocol == "auto":
                        break
                    if reason in {"authorization_rejected", "interactive_signin_required", "permission_denied",
                                  "imap_authentication_or_protocol"}:
                        raise
                    if "HTTP 401" in str(exc):
                        self.service.forget([watch.account_id])
                    watch.wait(max(retry_after, self.interval, 2 ** min(failures, 4)))

    def _graph(self, watch, token):
        since = datetime.fromtimestamp(watch.since, timezone.utc).isoformat().replace("+00:00", "Z")
        errors = []
        for folder, label in mail.MAIL_FOLDERS:
            if not watch.listening():
                return
            try:
                yield from mail.graph_folder_messages(token, folder, label, since)
            except mail.MailError as exc:
                if exc.retry_after:
                    raise
                errors.append(exc)
        if len(errors) == len(mail.MAIL_FOLDERS):
            raise errors[0]

    def _imap(self, watch, address, token):
        client = mail.imap_connect()
        try:
            if not watch.listening():
                return
            try:
                mail.imap_authenticate(client, address, token)
            except imaplib.IMAP4.error:
                self.service.forget([watch.account_id])
                raise mail.MailError("IMAP 认证失败，请检查邮箱授权") from None
            can_idle = hasattr(client, "idle") and "IDLE" in client.capabilities
            since_day = (datetime.fromtimestamp(watch.since, timezone.utc) - timedelta(days=1)).date()
            month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[since_day.month - 1]
            since = f"{since_day.day:02d}-{month}-{since_day.year}"
            cursors = {}
            junk_names = None
            while watch.listening():
                status, inbox_size = client.select("INBOX", readonly=True)
                if status != "OK":
                    raise mail.MailError("无法打开 IMAP 收件箱")
                yield from self._imap_new(client, watch, "收件箱", since, cursors)
                if not watch.listening():
                    return
                if junk_names is None:
                    junk_names = mail.imap_junk_folders(client)
                for name in junk_names:
                    if not watch.listening():
                        return
                    if client.select(name, readonly=True)[0] == "OK":
                        junk_names = [name]
                        yield from self._imap_new(client, watch, "垃圾箱", since, cursors)
                        break
                else:
                    junk_names = []
                if not watch.listening():
                    return
                if can_idle:
                    status, current_size = client.select("INBOX", readonly=True)
                    if status != "OK":
                        raise mail.MailError("无法打开 IMAP 收件箱")
                    if current_size != inbox_size:
                        continue
                    # Periodically check junk and recover updates missed between commands.
                    with client.idle(duration=min(self.interval, watch.remaining())) as idle:
                        for kind, data in idle:
                            if (kind == "EXISTS" and data != current_size) or not watch.listening():
                                break
                else:
                    watch.wait(self.interval)
        finally:
            try:
                client.shutdown()
            except OSError:
                pass

    def _imap_new(self, client, watch, folder, since, cursors):
        _, values = client.response("UIDVALIDITY")
        validity = (values or [None])[0]
        previous_validity, high = cursors.get(folder, (None, 0))
        validity = validity or previous_validity
        if validity != previous_validity:
            high = 0
        criteria = ("UID", f"{high + 1}:*") if high else ("SINCE", since)
        status, data = client.uid("search", None, *criteria)
        if status != "OK":
            raise mail.MailError("无法查询 IMAP 新邮件")
        uids = sorted({int(uid) for uid in ((data or [None])[0] or b"").split() if uid.isdigit() and int(uid) > high},
                      reverse=True)[:20]
        if not uids:
            return
        status, rows = client.uid("fetch", ",".join(map(str, uids)), "(UID INTERNALDATE)")
        if status != "OK":
            raise mail.MailError("无法读取 IMAP 邮件时间")
        for row in rows or []:
            if not watch.listening():
                return
            if not isinstance(row, bytes):
                continue
            uid, date = UID_PATTERN.search(row), INTERNAL_DATE.search(row)
            if not uid or not date:
                continue
            date = date[1].decode("ascii")
            try:
                received = parsedate_to_datetime(date)
            except ValueError:
                continue
            if received.tzinfo is None or received.timestamp() < watch.since:
                continue
            status, parts = client.uid("fetch", uid[1], "(BODY.PEEK[])")
            if status != "OK":
                raise mail.MailError("无法读取 IMAP 邮件正文")
            for part in parts:
                if isinstance(part, tuple) and isinstance(part[1], bytes):
                    key = f"{validity!r}:{uid[1].decode('ascii')}"
                    yield mail.parse_mime(part[1], key, folder, date)
                    break
        cursors[folder] = (validity, uids[0])


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_webhook(url):
    parsed = urlsplit(url)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in url)):
        raise ValueError("请配置完整的 Webhook 地址，地址中不带用户名、密码或片段")
    if parsed.port == 0:
        raise ValueError("Webhook 端口无效")
    if parsed.scheme == "https":
        return
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "http" or not loopback:
        raise ValueError("Webhook 使用 HTTPS，同机联调可以使用本机 HTTP")


class Webhook:
    def __init__(self, url, secret):
        validate_webhook(url)
        self.url = url
        self.secret = secret.encode("utf-8")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def send(self, watch_id, code):
        body = json.dumps({"code": code}, separators=(",", ":")).encode("utf-8")
        sent_at = str(int(time.time()))
        signed = f"{sent_at}.{watch_id}.".encode("ascii") + body
        signature = hmac.new(self.secret, signed, hashlib.sha256).hexdigest()
        request = Request(self.url, body, headers={
            "Content-Type": "application/json",
            "X-Watch-ID": watch_id,
            "X-Webhook-Timestamp": sent_at,
            "X-Webhook-Signature": "sha256=" + signature,
        }, method="POST")
        try:
            with self.opener.open(request, timeout=3) as response:
                return 200 <= response.status < 300
        except HTTPError as exc:
            exc.close()
            return False
        except (URLError, OSError):
            return False


class ServerSettings:
    def __init__(self, store, service, defaults):
        self.store = store
        self.service = service
        self.lock = threading.Lock()
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS server_config (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
            row = db.execute("SELECT data FROM server_config WHERE id=1").fetchone()
        self.values = {**defaults, **(json.loads(row[0]) if row else {})}
        self.validate(self.values)

    @staticmethod
    def validate(values):
        if values["protocol"] not in ("auto", "imap", "graph"):
            raise ValueError("请选择自动、IMAP 或 Graph")
        interval, timeout = values["interval"], values["timeout"]
        if type(interval) not in (int, float) or not 1 <= interval <= 30:
            raise ValueError("补查间隔须为 1–30 秒")
        if type(timeout) is not int or not 10 <= timeout <= 300:
            raise ValueError("监听时长须为 10–300 秒")
        url, secret = values["webhook_url"], values["webhook_secret"]
        if not isinstance(url, str) or len(url) > 2048 or not isinstance(secret, str):
            raise ValueError("Webhook 设置无效")
        if url:
            validate_webhook(url)
        if secret and not 32 <= len(secret.encode("utf-8")) <= 1024:
            raise ValueError("Webhook 签名密钥至少需要 32 字节，最多 1024 字节")

    def _runtime(self, values):
        reader = MailReader(self.service, values["protocol"], values["interval"])
        url, secret = values["webhook_url"], values["webhook_secret"]
        webhook = Webhook(url, secret) if url and secret else None
        return reader, webhook, values["timeout"]

    def runtime(self):
        return self._runtime(self.values)

    def public(self):
        with self.lock:
            values = self.values
            return {**{key: values[key] for key in ("protocol", "interval", "timeout", "webhook_url")},
                    "webhook_ready": bool(values["webhook_url"] and values["webhook_secret"]),
                    "webhook_secret_set": bool(values["webhook_secret"])}

    def update(self, payload, monitor):
        with self.lock:
            values = {key: payload.get(key, value) for key, value in self.values.items()}
            if values["webhook_secret"] == "":
                values["webhook_secret"] = self.values["webhook_secret"]
            self.validate(values)
            if values["webhook_url"] and not values["webhook_secret"]:
                raise ValueError("请输入 Webhook 签名密钥")
            runtime = self._runtime(values)
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO server_config(id,data) VALUES(1,?)", (json.dumps(values),))
            monitor.configure(*runtime)
            self.values = values


class SingleMailboxMonitor:
    def __init__(self, reader, webhook, timeout=120):
        self.reader = reader
        self.webhook = webhook
        self.timeout = timeout
        self.condition = threading.Condition()
        self.current = None
        self.watches = OrderedDict()
        self.cancelled_ids = deque(maxlen=128)
        self.sent = OrderedDict()
        self.closed = False
        # One spare worker lets the new mailbox start while old network I/O drains.
        self.workers = [threading.Thread(target=self._work, daemon=True) for _ in range(2)]
        for worker in self.workers:
            worker.start()

    def configure(self, reader, webhook, timeout):
        with self.condition:
            self.reader, self.webhook, self.timeout = reader, webhook, timeout

    def start(self, account_id, *, address="", notify=True, watch_id=None):
        with self.condition:
            if notify and self.webhook is None:
                raise mail.MailError("请先配置 Webhook 地址和签名密钥")
            if watch_id is not None and (watch_id in self.watches or watch_id in self.cancelled_ids):
                raise ValueError("watch_id_already_used")
            if self.current:
                self._end_current("replaced")
            watch = Watch(
                watch_id or secrets.token_hex(16), account_id, math.floor(time.time()),
                time.monotonic() + self.timeout, address=address, reader=self.reader,
                webhook=self.webhook if notify else None,
            )
            self.current = watch
            self.watches[watch.id] = watch
            if len(self.watches) > 128:
                self.watches.popitem(last=False)
            self.condition.notify()
            return watch.id

    def _end_current(self, status):
        watch = self.current
        watch.code = ""
        if watch.status in LIVE_STATES:
            watch.status = status
            watch.cancel.set()

    def stop(self, watch_id, *, api_only=False):
        with self.condition:
            if not self.current or self.current.id != watch_id:
                if api_only and watch_id not in self.watches and watch_id not in self.cancelled_ids:
                    self.cancelled_ids.append(watch_id)
                return False
            if api_only and self.current.webhook is None:
                return False
            self._end_current("cancelled")
            return True

    def snapshot(self):
        with self.condition:
            watch = self.current
            if not watch:
                return None
            if time.monotonic() >= watch.code_deadline:
                watch.code = ""
            return {"watch_id": watch.id, "email": watch.address, "status": self.status(watch.id),
                    "code": watch.code}

    def import_accounts(self, store, accounts):
        addresses = {account.email.lower() for account in accounts}
        with self.condition:
            result = store.import_accounts(accounts)
            if self.current and self.current.address.lower() in addresses:
                self._end_current("cancelled")
            return result

    def status(self, watch_id):
        with self.condition:
            watch = self.watches.get(watch_id)
            if watch and watch.status == "listening" and not watch.remaining():
                watch.status = "timeout"
                watch.cancel.set()
            return watch.status if watch else None

    def close(self):
        with self.condition:
            self.closed = True
            if self.current:
                self.current.cancel.set()
            self.condition.notify_all()
        for worker in self.workers:
            worker.join(timeout=1)

    def _work(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or (self.current and not self.current.claimed))
                if self.closed:
                    return
                watch = self.current
                watch.claimed = True
            try:
                self._listen(watch)
            except Exception:
                self._finish(watch, "failed")

    def _finish(self, watch, status):
        with self.condition:
            if self.current is watch and watch.status in LIVE_STATES:
                watch.status = status

    def _listen(self, watch):
        code = None
        with closing(watch.reader.messages(watch)) as messages:
            for message in messages:
                if not watch.listening():
                    break
                code = self._claim(watch, message)
                if code:
                    break
        if code is None:
            self._finish(watch, "timeout")
        elif watch.webhook is None:
            self._finish(watch, "received")
        else:
            self._deliver(watch, code)

    def _claim(self, watch, message):
        code = message.get("code")
        key = message.get("internet_message_id") or message.get("id")
        if not code or not key or message.get("code_confidence") != "high":
            return None
        if mail.timestamp(message.get("received_at")) < watch.since:
            return None
        with self.condition:
            key = (watch.account_id, key)
            if self.current is not watch or not watch.listening() or key in self.sent:
                return None
            self.sent[key] = None
            if len(self.sent) > 512:
                self.sent.popitem(last=False)
            watch.status = "delivering"
            watch.code = code
            watch.code_deadline = time.monotonic() + 300
            return code

    def _deliver(self, watch, code):
        for delay in (0, 1, 2, 4):
            if watch.cancel.wait(delay):
                return
            if watch.webhook.send(watch.id, code):
                self._finish(watch, "delivered")
                return
        self._finish(watch, "delivery_failed")


class APIHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_):
        pass

    def reply(self, status, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def authorized(self, *, admin=False):
        expected = self.server.admin_authorization if admin else self.server.authorization
        if expected is None:
            self.reply(404, {"error": "not_found"})
            return False
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        if hmac.compare_digest(supplied, expected):
            return True
        self.reply(401, {"error": "unauthorized"})
        return False

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok"})
            return
        if self.path in ADMIN_ASSETS:
            if self.server.admin_authorization:
                name, kind = ADMIN_ASSETS[self.path]
                self.reply(200, (mail.ROOT / "server_web" / name).read_bytes(), kind)
            else:
                self.reply(404, {"error": "not_found"})
            return
        if self.path.startswith("/admin/api/"):
            if self.authorized(admin=True):
                self._admin_get()
            return
        if not self.authorized():
            return
        match = re.fullmatch(r"/v1/watches/([0-9a-f]{32})", self.path)
        status = self.server.monitor.status(match[1]) if match else None
        if status is None:
            self.reply(404, {"error": "not_found"})
        else:
            self.reply(200, {"watch_id": match[1], "status": status})

    def do_DELETE(self):
        if not self.authorized():
            return
        match = re.fullmatch(r"/v1/watches/([0-9a-f]{32})", self.path)
        if match and self.server.monitor.stop(match[1], api_only=True):
            self.reply(200, {"status": "ok"})
        else:
            self.reply(404, {"error": "not_found"})

    def _admin_get(self):
        try:
            if self.path == "/admin/api/accounts":
                accounts = self.server.store.accounts()
                self.reply(200, {"accounts": [{"id": item["id"], "email": item["email"]} for item in accounts]})
            elif self.path == "/admin/api/settings":
                self.reply(200, self.server.settings.public())
            elif self.path == "/admin/api/watch":
                self.reply(200, {"watch": self.server.monitor.snapshot()})
            else:
                self.reply(404, {"error": "not_found"})
        except sqlite3.Error:
            self.reply(503, {"error": "mailbox_store_unavailable"})

    def _payload(self, limit):
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or "Transfer-Encoding" in self.headers or not 0 < int(lengths[0]) <= limit:
            raise ValueError("invalid length")
        payload = json.loads(self.rfile.read(int(lengths[0])))
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        return payload

    def do_POST(self):
        admin = self.path.startswith("/admin/api/")
        if not self.authorized(admin=admin):
            return
        try:
            limit = 16 * 1024 * 1024 if self.path == "/admin/api/import" else 8192
            payload = self._payload(limit)
        except (ValueError, OSError, RecursionError):
            self.reply(400, {"error": "invalid_request"})
            return
        try:
            if admin:
                self._admin_post(payload)
            elif self.path == "/v1/watches":
                self._start_watch(payload, notify=True)
            else:
                self.reply(404, {"error": "not_found"})
        except (sqlite3.Error, UnicodeError):
            self.reply(503, {"error": "mailbox_store_unavailable"})
        except (ValueError, mail.MailError) as exc:
            self.reply(400, {"error": str(exc)})

    def _admin_post(self, payload):
        if self.path == "/admin/api/import":
            content = payload.get("text")
            if not isinstance(content, str):
                raise ValueError("请输入邮箱清单")
            result = self.server.monitor.import_accounts(self.server.store, mail.parse_accounts(content))
            self.reply(200, result)
        elif self.path == "/admin/api/watches":
            self._start_watch(payload, notify=False)
        elif self.path == "/admin/api/stop":
            if self.server.monitor.stop(payload.get("watch_id")):
                self.reply(200, {"status": "ok"})
            else:
                self.reply(409, {"error": "watch_changed"})
        elif self.path == "/admin/api/settings":
            self.server.settings.update(payload, self.server.monitor)
            self.reply(200, self.server.settings.public())
        else:
            self.reply(404, {"error": "not_found"})

    def _start_watch(self, payload, *, notify):
        address = payload.get("email")
        if not isinstance(address, str) or not 0 < len(address.strip()) <= 254:
            self.reply(400, {"error": "invalid_request"})
            return
        address = address.strip()
        account_id = self.server.store.find_account_id(address)
        if account_id is None:
            self.reply(404, {"error": "mailbox_not_imported"})
            return
        requested_id = payload.get("watch_id") if notify else None
        if requested_id is not None and (not isinstance(requested_id, str) or not re.fullmatch(r"[0-9a-f]{32}", requested_id)):
            self.reply(400, {"error": "invalid_request"})
            return
        try:
            watch_id = self.server.monitor.start(account_id, address=address, notify=notify, watch_id=requested_id)
        except ValueError:
            self.reply(409, {"error": "watch_id_already_used"})
            return
        except mail.MailError:
            self.reply(409, {"error": "webhook_not_configured"})
            return
        self.reply(202, {"watch_id": watch_id})


def make_api_server(port, store, monitor, api_token, *, admin_token=None, settings=None):
    if admin_token and (settings is None or admin_token == api_token):
        raise ValueError("Web 管理页需要独立的管理令牌和配置存储")
    server = ThreadingHTTPServer(("127.0.0.1", port), APIHandler)
    server.store = store
    server.monitor = monitor
    server.authorization = ("Bearer " + api_token).encode("utf-8")
    server.admin_authorization = ("Bearer " + admin_token).encode("utf-8") if admin_token else None
    server.settings = settings
    return server


def main():
    parser = argparse.ArgumentParser(description="收码台服务器版")
    parser.add_argument("--data", type=Path, default=mail.ROOT / ".mailtool" / "server.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="导入邮箱 TXT")
    importer.add_argument("file", type=Path)
    serve = commands.add_parser("serve", help="启动单邮箱监听 API")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--protocol", choices=("auto", "imap", "graph"), default="auto")
    serve.add_argument("--interval", type=float, default=2.0, help="补查间隔（秒）")
    serve.add_argument("--timeout", type=int, default=120, help="单次监听时长（秒）")
    args = parser.parse_args()
    if args.command == "import":
        try:
            accounts = mail.parse_accounts(args.file.read_text(encoding="utf-8-sig"))
            print(json.dumps(mail.Store(args.data).import_accounts(accounts), ensure_ascii=False))
        except (OSError, ValueError, mail.MailError) as exc:
            parser.exit(1, f"导入失败：{exc}\n")
        return
    api_token = os.environ.get("MAILTOOL_API_TOKEN", "")
    admin_token = os.environ.get("MAILTOOL_ADMIN_TOKEN", "")
    key_pattern = r"[A-Za-z0-9._~-]{32,256}"
    if not re.fullmatch(key_pattern, api_token):
        parser.error("请配置至少 32 字符的 MAILTOOL_API_TOKEN")
    if admin_token and (not re.fullmatch(key_pattern, admin_token) or admin_token == api_token):
        parser.error("MAILTOOL_ADMIN_TOKEN 至少 32 字符，且须与 API 令牌不同")
    store = mail.Store(args.data)
    defaults = {
        "protocol": args.protocol, "interval": args.interval, "timeout": args.timeout,
        "webhook_url": os.environ.get("MAILTOOL_WEBHOOK_URL", ""),
        "webhook_secret": os.environ.get("MAILTOOL_WEBHOOK_SECRET", ""),
    }
    try:
        settings = ServerSettings(store, mail.MailService(store), defaults)
        reader, webhook, timeout = settings.runtime()
    except (ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    if not admin_token and webhook is None:
        parser.error("请配置 Webhook 地址和密钥，或设置 MAILTOOL_ADMIN_TOKEN 后在网页配置")
    monitor = SingleMailboxMonitor(reader, webhook, timeout)
    try:
        with make_api_server(args.port, store, monitor, api_token, admin_token=admin_token, settings=settings) as server:
            print(f"收码台服务器版已启动 http://127.0.0.1:{server.server_port}", flush=True)
            if admin_token:
                print(f"Web 管理页 http://127.0.0.1:{server.server_port}/admin", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.close()


if __name__ == "__main__":
    main()
