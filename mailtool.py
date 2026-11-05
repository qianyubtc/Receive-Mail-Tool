#!/usr/bin/env python3
"""Local Microsoft OAuth2 verification-code reader."""

import argparse
import email.policy
import imaplib
import ipaddress
import logging.handlers
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_URL = "https://graph.microsoft.com/v1.0/me/mailFolders"
TIMEOUT = 15
APP_VERSION = "2026.11.06-compact.1"
MAX_BATCH_SIZE = 20


class MailError(Exception):
    """A user-facing error that never includes a credential or raw server response."""

    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    try:
        date = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


@dataclass
class Account:
    email: str
    client_id: str
    refresh_token: str = field(repr=False)
    id: int = 0
    revision: int = 0


def parse_accounts(text):
    accounts = {}
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("----", 3)]
        if len(parts) != 4 or not all(parts[i] for i in (0, 2, 3)):
            raise MailError("第 %s 行格式错误，应为：邮箱----密码----client_id----refresh_token" % number)
        address, _password, client_id, token = parts
        if not re.fullmatch(r"[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+\.[^\s@\x00-\x1f]+", address):
            raise MailError("第 %s 行邮箱地址格式错误" % number)
        if any(ord(char) < 32 for char in client_id + token):
            raise MailError("第 %s 行凭证包含无效字符" % number)
        accounts[address.lower()] = Account(address, client_id, token)
    if not accounts:
        raise MailError("请先粘贴账号，或选择包含账号信息的 TXT 文件")
    return list(accounts.values())


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        if os.name == "posix":
            os.chmod(str(self.path), 0o600)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY, email TEXT UNIQUE COLLATE NOCASE NOT NULL,
                client_id TEXT NOT NULL, refresh_token TEXT NOT NULL,
                checked_at TEXT, error TEXT NOT NULL DEFAULT '',
                messages TEXT NOT NULL DEFAULT '[]', protocol TEXT NOT NULL DEFAULT '',
                warnings TEXT NOT NULL DEFAULT '[]')""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
            for name in ("deleted", "used", "revision"):
                if name not in columns:
                    db.execute("ALTER TABLE accounts ADD COLUMN %s INTEGER NOT NULL DEFAULT 0" % name)
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            next_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM accounts").fetchone()[0]
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('next_account_id',?)", (next_id,))
            db.execute("""CREATE TABLE IF NOT EXISTS code_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
                message_key TEXT NOT NULL, code TEXT NOT NULL, confidence TEXT NOT NULL,
                received_at TEXT NOT NULL, subject TEXT NOT NULL, sender TEXT NOT NULL,
                folder TEXT NOT NULL, discovered_at TEXT NOT NULL,
                UNIQUE(account_id,message_key))""")
            db.execute("CREATE INDEX IF NOT EXISTS events_account ON code_events(account_id,id)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=20)
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_accounts(self, accounts):
        added = updated = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            next_id = db.execute("SELECT value FROM settings WHERE key='next_account_id'").fetchone()[0]
            for account in accounts:
                exists = db.execute("SELECT id FROM accounts WHERE email=?", (account.email,)).fetchone()
                if exists:
                    db.execute("""UPDATE accounts SET client_id=?,refresh_token=?,error='',
                        messages='[]',checked_at=NULL,protocol='',warnings='[]',deleted=0,
                        revision=revision+1 WHERE id=?""",
                        (account.client_id, account.refresh_token, exists[0]))
                    updated += 1
                else:
                    db.execute("INSERT INTO accounts(id,email,client_id,refresh_token) VALUES(?,?,?,?)",
                               (next_id, account.email, account.client_id, account.refresh_token))
                    next_id += 1
                    added += 1
            db.execute("UPDATE settings SET value=? WHERE key='next_account_id'", (next_id,))
        return {"added": added, "updated": updated}

    def account(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT email,client_id,refresh_token,id,revision FROM accounts WHERE id=? AND deleted=0", (account_id,)).fetchone()
        if not row:
            raise MailError("邮箱不存在，请重新选择")
        return Account(*row)

    def accounts(self, trash=False):
        with self.connect() as db:
            rows = db.execute("SELECT id,email,checked_at,error,used FROM accounts WHERE deleted=? ORDER BY id ASC", (int(trash),)).fetchall()
        return [{**dict(zip(("id", "email", "checked_at", "error", "used"), row)), "number": number}
                for number, row in enumerate(rows, 1)]

    def update_accounts(self, ids, action):
        ids = validate_ids(ids, limit=10000)
        if action not in ("delete", "restore", "used", "unused"):
            raise MailError("不支持的邮箱操作")
        column, value = {"delete": ("deleted", 1), "restore": ("deleted", 0),
                         "used": ("used", 1), "unused": ("used", 0)}[action]
        changed = 0
        with self.connect() as db:
            for account_id in ids:
                result = db.execute("UPDATE accounts SET %s=? WHERE id=?" % column, (value, account_id))
                changed += result.rowcount
        return {"changed": changed}

    def purge_accounts(self, ids):
        ids = validate_ids(ids, limit=10000)
        with self.connect() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            trash_ids = {row[0] for row in db.execute("SELECT id FROM accounts WHERE deleted=1")}
            if not set(ids).issubset(trash_ids):
                raise MailError("只能彻底删除回收站中的邮箱，请刷新后重试")
            for account_id in ids:
                db.execute("DELETE FROM code_events WHERE account_id=?", (account_id,))
                db.execute("DELETE FROM accounts WHERE id=? AND deleted=1", (account_id,))
        return {"changed": len(ids)}

    def rotate_token(self, account, token):
        with self.connect() as db:
            result = db.execute("UPDATE accounts SET refresh_token=? WHERE id=? AND refresh_token=? AND revision=? AND deleted=0",
                                (token, account.id, account.refresh_token, account.revision))
            if result.rowcount != 1:
                raise MailError("账号凭证已更新，请重新收信")

    def save_messages(self, account_id, messages, protocol, warnings, revision=None):
        checked = utc_now()
        with self.connect() as db:
            query = "UPDATE accounts SET messages=?,checked_at=?,error='',protocol=?,warnings=? WHERE id=? AND deleted=0"
            values = (json.dumps(messages, ensure_ascii=False), checked, protocol,
                      json.dumps(warnings, ensure_ascii=False), account_id)
            if revision is not None:
                query += " AND revision=?"
                values += (revision,)
            if db.execute(query, values).rowcount != 1:
                raise MailError("邮箱已删除或凭证已更新，请重新选择")
        return {"messages": messages, "checked_at": checked, "protocol": protocol, "warnings": warnings, "error": ""}

    def save_error(self, account_id, error):
        with self.connect() as db:
            db.execute("UPDATE accounts SET error=? WHERE id=? AND deleted=0", (error, account_id))

    def cached(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT messages,checked_at,error,protocol,warnings FROM accounts WHERE id=? AND deleted=0", (account_id,)).fetchone()
        if not row:
            raise MailError("邮箱不存在")
        messages = json.loads(row[0])
        changed = False
        for message in messages:
            result = extract_code(message.get("subject", ""), message.get("body", ""))
            if any(message.get(key) != value for key, value in result.items()):
                message.update(result)
                changed = True
        if changed:
            with self.connect() as db:
                updated = db.execute("UPDATE accounts SET messages=? WHERE id=? AND deleted=0 AND messages=?",
                    (json.dumps(messages, ensure_ascii=False), account_id, row[0])).rowcount
            if updated:
                self.record_codes(account_id, messages, row[3], utc_now())
                diagnostics = getattr(self, "diagnostics", None)
                if diagnostics:
                    diagnostics.emit("cache_reparsed", account_id=account_id, message_count=len(messages),
                                     code_count=sum(bool(item.get("code")) for item in messages))
        return {"messages": messages, "checked_at": row[1], "error": row[2],
                "protocol": row[3], "warnings": json.loads(row[4])}

    def record_codes(self, account_id, messages, protocol, started_at):
        cutoff = timestamp(started_at) - 600
        with self.connect() as db:
            if not db.execute("SELECT id FROM accounts WHERE id=? AND deleted=0", (account_id,)).fetchone():
                return
            for message in reversed(messages):
                if not message.get("code") or timestamp(message.get("received_at")) < cutoff:
                    continue
                key = message.get("internet_message_id") or protocol + ":" + str(message.get("id", ""))
                if not message.get("id") and not message.get("internet_message_id"):
                    continue
                db.execute("""INSERT INTO code_events
                    (account_id,message_key,code,confidence,received_at,subject,sender,folder,discovered_at)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,message_key) DO UPDATE SET
                    code=excluded.code,confidence=excluded.confidence,received_at=excluded.received_at,
                    subject=excluded.subject,sender=excluded.sender,folder=excluded.folder""",
                    (account_id, key, message["code"], message.get("code_confidence", "possible"),
                     message.get("received_at", ""), message.get("subject", ""), message.get("sender", ""),
                     message.get("folder", ""), utc_now()))

    def code_events(self, ids, started_at):
        if not ids or not started_at:
            return []
        marks = ",".join("?" for _ in ids)
        with self.connect() as db:
            rows = db.execute("""WITH numbered_accounts AS (
                    SELECT id,email,ROW_NUMBER() OVER (ORDER BY id) AS number FROM accounts WHERE deleted=0
                ) SELECT e.id,e.account_id,a.email,e.code,e.confidence,e.received_at,
                e.subject,e.sender,e.folder,e.discovered_at,a.number FROM (
                    SELECT code_events.*, ROW_NUMBER() OVER (
                        PARTITION BY account_id ORDER BY julianday(received_at) DESC,id DESC) AS rank
                    FROM code_events WHERE account_id IN (%s)
                ) e JOIN numbered_accounts a ON a.id=e.account_id WHERE e.rank<=4
                ORDER BY e.id DESC""" % marks, tuple(ids)).fetchall()
        names = ("id", "account_id", "email", "code", "confidence", "received_at", "subject", "sender", "folder", "discovered_at", "number")
        return [{**dict(zip(names, row)), "is_new": timestamp(row[5]) >= timestamp(started_at)}
                for row in rows if timestamp(row[5]) >= timestamp(started_at) - 600]


def validate_ids(ids, limit=MAX_BATCH_SIZE):
    if not isinstance(ids, list) or not ids or len(ids) > limit:
        raise MailError("请选择 1–%s 个邮箱" % limit)
    if any(type(value) is not int or value <= 0 for value in ids):
        raise MailError("邮箱编号格式错误")
    return list(dict.fromkeys(ids))


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        if tag in ("br", "p", "div", "tr", "td", "li", "h1", "h2"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_to_text(body):
    parser = TextExtractor()
    parser.feed(body or "")
    return re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.parts)).strip()


CODE_HINT = re.compile(r"验证码|校验码|动态码|安全码|认证码|登录码|驗證碼|verification(?:\s+code)?|security\s+code|one[- ]time(?:\s+(?:password|code))?|passcode|\botp\b|\bcode\b", re.I)
CODE_LABEL = re.compile(r"验证码|校验码|动态码|安全码|认证码|登录码|驗證碼|verification\s+code|security\s+code|(?:one[- ]time|authentication|login|confirmation)\s+(?:password|code)|passcode|\botp\b|\bcode\b", re.I)
CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(?:[0-9]{4}[ -][0-9]{4}|[0-9]{3}[ -][0-9]{3}|[0-9]{4,8}|[A-Z0-9]{5,8})(?![A-Za-z0-9_])", re.I)
DATE_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?:[-/.]\d{1,2}){2}(?!\d)|(?<!\d)(?:19|20)\d{2}年\d{1,2}月\d{1,2}日")
LINK_PATTERN = re.compile(r"https?://[^\s<>]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", re.I)


def normalize_code_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)


def extract_code(subject, body):
    """Prefer a code directly after its label; dates and tracking links are never candidates."""
    subject, body = normalize_code_text(subject), normalize_code_text(body)
    subject_label = bool(CODE_LABEL.search(subject))
    candidates = []
    for text, subject_bonus, is_body in ((subject, 20, False), (body, 0, True)):
        hints, labels = list(CODE_HINT.finditer(text)), list(CODE_LABEL.finditer(text))
        excluded = list(DATE_PATTERN.finditer(text)) + list(LINK_PATTERN.finditer(text))
        for match in CODE_PATTERN.finditer(text):
            if any(match.start() < span.end() and match.end() > span.start() for span in excluded):
                continue
            code = re.sub(r"[ -]", "", match.group())
            if not any(char.isdigit() for char in code):
                continue
            distance = min((min(abs(match.start() - hint.end()), abs(hint.start() - match.end()))
                            for hint in hints), default=10000)
            contextual = distance <= 80
            direct = any(
                0 <= match.start() - label.end() <= 40 and
                re.fullmatch(r"[\s:：=,，.!！\-]*(?:(?:is|为|是)\s*)?(?:G-)?[\s:：=]*",
                             text[label.end():match.start()], re.I)
                for label in labels if label.end() <= match.start()
            )
            # A short code-only body line can use the explicit label in the subject.
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line = text[line_start:line_end if line_end >= 0 else len(text)].strip()
            subject_context = is_body and subject_label and line == match.group()
            contextual = contextual or subject_context
            if not contextual and (not code.isdigit() or len(code) != 6):
                continue
            if match.start() and text[match.start() - 1] in "+/@":
                continue
            after = text[match.end():match.end() + 2]
            if after[:1] in ("@", "/", "%") or re.match(r"\.\d", after):
                continue
            score = (1000 - distance if direct else 500 if subject_context else 200 - distance if contextual else 10) + subject_bonus
            if code.isdigit() and len(code) == 6:
                score += 10
            candidates.append((score, -match.start(), code, "high" if contextual else "possible"))
    if not candidates:
        return {"code": "", "code_confidence": ""}
    best = max(candidates)
    return {"code": best[2], "code_confidence": best[3]}


def read_json(request):
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(65536))
        except (ValueError, OSError):
            payload = {}
        error = payload.get("error", "")
        code = error.get("code", "") if isinstance(error, dict) else error
        safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "request_failed"
        if safe_code in ("invalid_grant", "interaction_required"):
            raise MailError("授权已失效，请重新授权并更新令牌（%s）" % safe_code) from None
        if exc.code == 429:
            retry = exc.headers.get("Retry-After", "30") if exc.headers else "30"
            try:
                delay = max(1, int(retry))
            except (ValueError, TypeError):
                try:
                    delay = max(1, parsedate_to_datetime(retry).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    delay = 30
            raise MailError("微软请求限流，已按服务端要求延后重试（429）", retry_after=delay) from None
        if safe_code in ("invalid_scope", "consent_required") or exc.code == 403:
            raise MailError("当前应用未获得此协议的读取权限，可切换 Graph / IMAP 或更新授权（%s）" % safe_code) from None
        raise MailError("微软接口请求失败：HTTP %s / %s" % (exc.code, safe_code)) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
        raise MailError("连接微软服务超时或网络不可用，请检查网络后重试") from None
    except (ValueError, TypeError):
        raise MailError("微软接口返回了无法解析的数据") from None


def refresh_access(account, protocol):
    scope = "https://graph.microsoft.com/.default" if protocol == "graph" else "https://outlook.office.com/IMAP.AccessAsUser.All"
    data = urllib.parse.urlencode({"client_id": account.client_id, "refresh_token": account.refresh_token,
                                   "grant_type": "refresh_token", "scope": scope}).encode()
    result = read_json(urllib.request.Request(TOKEN_URL, data=data, method="POST"))
    if not isinstance(result, dict) or not result.get("access_token"):
        raise MailError("微软未返回访问令牌，请检查账号授权")
    return result


def graph_messages(access_token):
    messages, warnings = [], []
    for folder, label in (("inbox", "收件箱"), ("junkemail", "垃圾箱")):
        query = urllib.parse.urlencode({"$top": 10, "$orderby": "receivedDateTime desc",
                                       "$select": "id,internetMessageId,subject,from,receivedDateTime,body,isRead"})
        request = urllib.request.Request(GRAPH_URL + "/" + folder + "/messages?" + query,
                                         headers={"Authorization": "Bearer " + access_token,
                                                  "Prefer": 'outlook.body-content-type="text"'})
        try:
            result = read_json(request)
            if not isinstance(result, dict) or not isinstance(result.get("value"), list):
                raise MailError("微软邮件接口返回了无效的邮件列表")
            for item in result["value"]:
                body_data = item.get("body") or {}
                body = body_data.get("content") or ""
                if str(body_data.get("contentType", "")).lower() == "html":
                    body = html_to_text(body)
                subject = item.get("subject") or "（无主题）"
                sender = ((item.get("from") or {}).get("emailAddress") or {})
                messages.append({"id": item.get("id", ""), "subject": subject,
                                 "internet_message_id": item.get("internetMessageId", ""),
                                 "sender": sender.get("address") or sender.get("name", ""),
                                 "received_at": item.get("receivedDateTime", ""), "folder": label,
                                 "body": body[:100000], **extract_code(subject, body)})
        except MailError as exc:
            if exc.retry_after:
                raise
            warnings.append(label + "：" + str(exc))
    if len(warnings) == 2:
        raise MailError("；".join(warnings))
    return messages, warnings


def parse_mime(raw, uid, folder, internal_date=""):
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    body_part = message.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        try:
            body = body_part.get_content()
        except (LookupError, UnicodeError):
            body = (body_part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if not isinstance(body, str):
            body = ""
        if body_part.get_content_type() == "text/html":
            body = html_to_text(body)
    received_at = ""
    try:
        date = parsedate_to_datetime(internal_date or str(message.get("Date", "")))
        received_at = date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        pass
    subject = str(message.get("Subject", "（无主题）"))
    return {"id": "imap:%s:%s" % (folder, uid), "subject": subject,
            "internet_message_id": str(message.get("Message-ID", "")),
            "sender": str(message.get("From", "")), "received_at": received_at,
            "folder": folder, "body": body[:100000], **extract_code(subject, body)}


def imap_messages(address, access_token):
    client = None
    phase = "connect"
    try:
        client = imaplib.IMAP4_SSL("outlook.office365.com", 993, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
        phase = "authenticate"
        auth = ("user=%s\x01auth=Bearer %s\x01\x01" % (address, access_token)).encode()
        # On a failed XOAUTH2 challenge, terminate SASL instead of repeating the token.
        replies = iter((auth, b""))
        client.authenticate("XOAUTH2", lambda _: next(replies, b""))
        phase = "list_folders"
        junk = ["Junk", '"Junk Email"']
        status, folders = client.list()
        if status == "OK":
            for folder in folders or []:
                if not isinstance(folder, bytes):
                    continue
                match = re.match(rb'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)$', folder)
                if match and b"\\junk" in match[1].lower().split():
                    junk.insert(0, match[2].decode("ascii", errors="replace"))
        messages, warnings = [], []
        for candidates, label in ((["INBOX"], "收件箱"), (junk, "垃圾箱")):
            phase = "select_folder"
            if not any(client.select(name, readonly=True)[0] == "OK" for name in candidates):
                warnings.append(label + "：无法打开文件夹")
                continue
            phase = "search"
            status, data = client.uid("search", None, "ALL")
            if status != "OK":
                warnings.append(label + "：无法查询邮件")
                continue
            uids = sorted((data[0] or b"").split(), key=int, reverse=True)[:10]
            for uid in uids:
                phase = "fetch"
                status, fetched = client.uid("fetch", uid, "(BODY.PEEK[] INTERNALDATE)")
                if status != "OK":
                    warnings.append(label + "：有邮件读取失败")
                    continue
                for part in fetched or []:
                    if isinstance(part, tuple) and isinstance(part[1], bytes):
                        date_match = re.search(rb'INTERNALDATE "([^"]+)"', part[0])
                        internal_date = date_match[1].decode("ascii") if date_match else ""
                        phase = "parse"
                        messages.append(parse_mime(part[1], uid.decode(), label, internal_date))
                        break
        if len(warnings) >= 2 and not messages:
            raise MailError("；".join(warnings))
        return messages, warnings
    except (imaplib.IMAP4.error, OSError, EOFError, ValueError):
        description = {"connect": "IMAP 连接失败，请检查网络或端口是否可用",
                       "authenticate": "IMAP 认证失败，请确认授权有效且邮箱已开通 IMAP",
                       "parse": "IMAP 邮件已读取，但正文解析失败"}.get(phase, "IMAP 读取失败，请稍后重试或切换 Graph")
        error = MailError(description)
        error.imap_stage = phase
        raise error from None
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError, EOFError):
                pass


class MailService:
    def __init__(self, store, diagnostics=None):
        self.store = store
        self.diagnostics = diagnostics
        self.lock = threading.RLock()
        self.account_locks = {}
        self.slots = threading.BoundedSemaphore(8)
        self.token_cache = {}

    def forget(self, ids=None):
        with self.lock:
            if ids is None:
                self.token_cache.clear()
            else:
                for key in list(self.token_cache):
                    if key[0] in ids:
                        self.token_cache.pop(key, None)

    def fetch(self, account_id, protocol):
        if protocol not in ("graph", "imap"):
            raise MailError("不支持的收信协议")
        with self.lock:
            account_lock = self.account_locks.setdefault(account_id, threading.Lock())
        if not account_lock.acquire(blocking=False):
            raise MailError("该邮箱正在收信，请稍后查看结果", retry_after=1)
        self.slots.acquire()
        started, stage = time.monotonic(), "token_refresh"
        if self.diagnostics:
            self.diagnostics.emit("fetch_started", account_id=account_id, protocol=protocol)
        try:
            account = self.store.account(account_id)
            key = (account_id, protocol)
            with self.lock:
                cached = self.token_cache.get(key)
            if cached and cached[1] > time.monotonic() and cached[2:] == (account.refresh_token, account.client_id, account.revision):
                access_token = cached[0]
            else:
                result = refresh_access(account, protocol)
                if self.diagnostics:
                    self.diagnostics.emit("token_refreshed", account_id=account_id, protocol=protocol)
                new_token = result.get("refresh_token") or account.refresh_token
                if new_token != account.refresh_token:
                    self.store.rotate_token(account, new_token)
                access_token = result["access_token"]
                try:
                    lifetime = max(0, min(int(result.get("expires_in", 3600)) - 60, 3600))
                except (ValueError, TypeError):
                    lifetime = 0
                with self.lock:
                    self.token_cache[key] = (access_token, time.monotonic() + lifetime, new_token, account.client_id, account.revision)
            stage = "read_messages"
            if protocol == "graph":
                messages, warnings = graph_messages(access_token)
            else:
                messages, warnings = imap_messages(account.email, access_token)
            messages.sort(key=lambda item: timestamp(item.get("received_at")), reverse=True)
            stage = "save_messages"
            result = self.store.save_messages(account_id, messages, protocol, warnings, account.revision)
            if self.diagnostics:
                self.diagnostics.emit("fetch_completed", account_id=account_id, protocol=protocol,
                    message_count=len(messages), code_count=sum(bool(item.get("code")) for item in messages),
                    code_length_min=min((len(item["code"]) for item in messages if item.get("code")), default=0),
                    code_length_max=max((len(item["code"]) for item in messages if item.get("code")), default=0),
                    warning_count=len(warnings), elapsed_ms=int((time.monotonic() - started) * 1000))
                for warning in warnings:
                    self.diagnostics.emit("folder_warning", account_id=account_id, protocol=protocol,
                        folder="junk" if warning.startswith("垃圾箱") else "inbox" if warning.startswith("收件箱") else "unknown",
                        reason=diagnostic_reason(warning))
            return result
        except Exception as exc:
            if self.diagnostics:
                self.diagnostics.emit("fetch_failed", account_id=account_id, protocol=protocol,
                    stage=stage, imap_stage=getattr(exc, "imap_stage", ""),
                    reason=diagnostic_reason(exc), error_type=type(exc).__name__)
            self.forget([account_id])
            if isinstance(exc, MailError):
                self.store.save_error(account_id, str(exc))
            raise
        finally:
            self.slots.release()
            account_lock.release()


class BatchMonitor:
    """Bounded, server-side polling; changing a batch discards late old-batch results."""

    def __init__(self, store, service, workers=8, stagger=0.12):
        self.store, self.service, self.workers = store, service, workers
        self.stagger = stagger
        self.diagnostics = getattr(service, "diagnostics", None)
        self.condition = threading.Condition()
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mailbox")
        self.running = self.closed = False
        self.generation = 0
        self.ids = []
        self.protocol, self.interval, self.started_at = "graph", 10, None
        self.due, self.failures, self.results, self.tasks = {}, {}, {}, {}
        self.thread = threading.Thread(target=self._loop, daemon=True, name="mailbox-scheduler")
        self.thread.start()

    def start(self, ids, protocol="graph", interval=10):
        ids = validate_ids(ids)
        if protocol not in ("graph", "imap") or type(interval) is not int or not 5 <= interval <= 300:
            raise MailError("协议或刷新间隔无效")
        available = {item["id"] for item in self.store.accounts()}
        if not set(ids).issubset(available):
            raise MailError("有邮箱已删除，请刷新列表后重新选择")
        for account_id in ids:
            self.store.cached(account_id)
        with self.condition:
            self.generation += 1
            self.ids, self.protocol, self.interval = ids, protocol, interval
            self.started_at = utc_now()
            self.running = True
            self.failures, self.results = {}, {}
            now = time.monotonic()
            self.due = {account_id: now + index * self.stagger for index, account_id in enumerate(ids)}
            self.condition.notify_all()
        if self.diagnostics:
            self.diagnostics.emit("batch_started", batch_size=len(ids), protocol=protocol, interval=interval)
        return self.status()

    def stop(self):
        if self.diagnostics:
            self.diagnostics.emit("batch_stopped")
        with self.condition:
            self.running = False
            self.generation += 1
            self.condition.notify_all()
        return self.status()

    def remove(self, ids):
        with self.condition:
            self.ids = [account_id for account_id in self.ids if account_id not in ids]
            for account_id in ids:
                self.results.pop(account_id, None)
                self.failures.pop(account_id, None)
                self.due.pop(account_id, None)
            if not self.ids:
                self.running = False
            self.condition.notify_all()

    def wake(self):
        with self.condition:
            for account_id in self.ids:
                self.due[account_id] = 0
            self.condition.notify_all()

    def status(self):
        with self.condition:
            snapshot = {"running": self.running, "ids": list(self.ids), "protocol": self.protocol,
                        "generation": self.generation,
                        "interval": self.interval, "started_at": self.started_at,
                        "concurrency": self.workers,
                        "in_flight": sum(1 for _, generation in self.tasks.values() if generation == self.generation),
                        "checked": len(self.results), "results": dict(self.results)}
        snapshot["events"] = self.store.code_events(snapshot["ids"], snapshot["started_at"])
        return snapshot

    def _loop(self):
        while True:
            with self.condition:
                if self.closed:
                    return
                if self.running:
                    active = {account_id for account_id, _ in self.tasks.values()}
                    available = self.workers - len(self.tasks)
                    now = time.monotonic()
                    ready = sorted((self.due.get(account_id, 0), account_id) for account_id in self.ids
                                   if account_id not in active and self.due.get(account_id, 0) <= now)
                    for _, account_id in ready[:available]:
                        generation, protocol = self.generation, self.protocol
                        future = self.executor.submit(self.service.fetch, account_id, protocol)
                        self.tasks[future] = (account_id, generation)
                        future.add_done_callback(self._done)
                self.condition.wait(timeout=0.1)

    def _done(self, future):
        try:
            result, error = future.result(), None
        except Exception as exc:
            result = None
            error = exc if isinstance(exc, MailError) else MailError("收信失败，请稍后重试")
        with self.condition:
            task = self.tasks.pop(future, None)
            if not task:
                return
            account_id, generation = task
            if self.closed or not self.running or generation != self.generation or account_id not in self.ids:
                self.condition.notify_all()
                return
            if error:
                failures = min(8, self.failures.get(account_id, 0) + 1)
                self.failures[account_id] = failures
                delay = max(self.interval, min(300, 5 * 2 ** failures), error.retry_after)
                self.results[account_id] = {"error": str(error), "checked_at": utc_now(), "retry_in": delay}
            else:
                delay = self.interval
                self.failures[account_id] = 0
                try:
                    self.store.record_codes(account_id, result["messages"], self.protocol, self.started_at)
                    self.results[account_id] = {"error": "", "checked_at": result["checked_at"], "warnings": result.get("warnings", [])}
                except (MailError, sqlite3.Error):
                    self.results[account_id] = {"error": "本地保存失败，请重试", "checked_at": utc_now()}
            self.due[account_id] = time.monotonic() + delay
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.running = False
            self.condition.notify_all()
        self.thread.join(timeout=2)
        self.executor.shutdown(wait=False, cancel_futures=True)


def diagnostic_reason(error):
    text = str(error)
    for marker, reason in (
        ("invalid_grant", "authorization_rejected"), ("interaction_required", "interactive_signin_required"),
        ("invalid_scope", "permission_denied"), ("consent_required", "permission_denied"),
        ("读取权限", "permission_denied"), ("429", "rate_limited"),
        ("网络", "network_error"), ("超时", "network_error"),
        ("认证", "imap_authentication_or_protocol"), ("解析", "message_parse_error"),
        ("已删除", "local_account_changed"), ("凭证已更新", "local_account_changed"),
    ):
        if marker in text:
            return reason
    return "unclassified"


class DiagnosticLog:
    """Only predefined event names, numeric counts and fixed diagnostic tags may leave the process."""
    EVENTS = {"service_started", "diagnostics_started", "fetch_started", "token_refreshed", "folder_warning",
              "fetch_completed", "fetch_failed", "cache_reparsed", "batch_started", "batch_stopped"}
    INTS = {"account_id", "message_count", "code_count", "warning_count", "batch_size",
            "interval", "elapsed_ms", "port", "code_length_min", "code_length_max"}
    TAGS = {
        "protocol": {"graph", "imap"},
        "folder": {"inbox", "junk", "unknown"},
        "imap_stage": {"connect", "authenticate", "list_folders", "select_folder", "search", "fetch", "parse"},
        "stage": {"token_refresh", "read_messages", "save_messages", "fetch"},
        "reason": {"authorization_rejected", "interactive_signin_required", "permission_denied",
                   "rate_limited", "network_error", "imap_authentication_or_protocol",
                   "message_parse_error", "local_account_changed", "unclassified"},
        "error_type": {"MailError", "OSError", "ValueError", "TypeError", "UnicodeError",
                       "OperationalError", "ProgrammingError", "Error", "Exception"},
    }

    def __init__(self, path=None):
        self.lock = threading.Lock()
        self.entries, self.errors = deque(maxlen=2000), deque(maxlen=100)
        self.sequence = 0
        self.handler = None
        if path is not None:
            path = Path(path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.handler = logging.handlers.RotatingFileHandler(
                str(path), maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
            self.handler.setFormatter(logging.Formatter("%(message)s"))
            if os.name == "posix":
                os.chmod(str(path), 0o600)

    def emit(self, event, **values):
        if event not in self.EVENTS:
            return
        entry = {"time": utc_now(), "event": event}
        for key, value in values.items():
            if key in self.INTS and type(value) is int:
                entry[key] = value
            elif key in self.TAGS and isinstance(value, str) and value in self.TAGS[key]:
                entry[key] = value
        with self.lock:
            self.sequence += 1
            entry["sequence"] = self.sequence
            self.entries.append(entry)
            if event in ("fetch_failed", "folder_warning"):
                self.errors.append(entry)
            if self.handler:
                record = logging.LogRecord("mailtool", logging.INFO, "", 0,
                                           json.dumps(entry, ensure_ascii=False), (), None)
                self.handler.handle(record)

    def read(self, after=0, limit=100):
        with self.lock:
            return {"cursor": self.sequence,
                    "entries": [item.copy() for item in self.entries if item["sequence"] > after][-limit:],
                    "errors": [item.copy() for item in self.errors]}

    def close(self):
        with self.lock:
            if self.handler:
                self.handler.close()
                self.handler = None


def private_client(address):
    try:
        value = ipaddress.ip_address(address)
        return any(value in ipaddress.ip_network(network) for network in
                   ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    except ValueError:
        return False


class DiagnosticHandler(BaseHTTPRequestHandler):
    server_version = "MailtoolDiagnostics"

    def log_message(self, *_):
        pass

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not private_client(self.client_address[0]):
            self.reply(403, {"error": "仅允许局域网或本机访问"})
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/health"):
            self.reply(200, {"service": "mailtool-diagnostics", "version": APP_VERSION,
                             "ready": True, "logs_require_access_key": True})
            return
        credential = self.headers.get("Authorization", "")
        if not secrets.compare_digest(credential.encode("utf-8"), ("Bearer " + self.server.access_key).encode("utf-8")):
            self.reply(401, {"error": "需要诊断窗口显示的只读访问码"})
            return
        if path != "/api/logs":
            self.reply(404, {"error": "诊断服务仅提供只读日志"})
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            after = max(0, int(query.get("after", ["0"])[0]))
            limit = min(200, max(1, int(query.get("limit", ["100"])[0])))
        except ValueError:
            self.reply(400, {"error": "查询参数无效"})
            return
        monitor = self.server.app.monitor
        with monitor.condition:
            summary = {"running": monitor.running, "mailbox_count": len(monitor.ids),
                       "checked": len(monitor.results), "protocol": monitor.protocol,
                       "interval": monitor.interval,
                       "problem_count": sum(bool(item.get("error") or item.get("warnings"))
                                            for item in monitor.results.values())}
        self.reply(200, {"version": APP_VERSION, "monitor": summary,
                         **self.server.app.diagnostics.read(after, limit)})

    def do_POST(self):
        self.reply(405, {"error": "诊断服务只读，不支持操作邮箱"})

    do_DELETE = do_POST
    do_PUT = do_POST


def make_diagnostic_server(port, app, bind="0.0.0.0"):
    server = ThreadingHTTPServer((bind, port), DiagnosticHandler)
    server.daemon_threads = True
    server.app = app
    server.access_key = secrets.token_urlsafe(24)
    return server


def local_ipv4_addresses():
    try:
        return sorted({address for address in socket.gethostbyname_ex(socket.gethostname())[2]
                       if private_client(address) and not address.startswith("127.")})
    except OSError:
        return []


class Handler(BaseHTTPRequestHandler):
    server_version = "Mailtool"

    def log_message(self, *_):
        pass  # Avoid account identifiers or request data in access logs.

    def send_data(self, status, data, content_type="application/json; charset=utf-8"):
        body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in ("127.0.0.1:%s" % port, "localhost:%s" % port)

    def do_GET(self):
        if not self.allowed_host():
            self.send_data(403, {"error": "仅允许本机访问"})
            return
        path = urllib.parse.urlsplit(self.path).path
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in static:
            name, kind = static[path]
            self.send_data(200, (ROOT / "web" / name).read_bytes(), kind)
        elif path == "/api/accounts":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self.send_data(200, {"accounts": self.server.store.accounts(query.get("trash") == ["1"]),
                                 "trash_count": len(self.server.store.accounts(trash=True)), "token": self.server.csrf_token})
        elif path == "/api/monitor":
            self.send_data(200, self.server.monitor.status())
        elif re.fullmatch(r"/api/accounts/\d+/messages", path):
            try:
                self.send_data(200, self.server.store.cached(int(path.split("/")[3])))
            except MailError as exc:
                self.send_data(404, {"error": str(exc)})
        else:
            self.send_data(404, {"error": "页面不存在"})

    def do_POST(self):
        if not self.allowed_host() or not secrets.compare_digest(self.headers.get("X-Mailtool-Token", ""), self.server.csrf_token):
            self.send_data(403, {"error": "请求校验失败，请刷新页面"})
            return
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self.send_data(403, {"error": "不允许跨站请求"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16 * 1024 * 1024:
                raise MailError("导入内容为空或超过 16 MB，请分批导入")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise MailError("请求格式错误")
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/import":
                text = payload.get("text")
                if not isinstance(text, str):
                    raise MailError("导入内容应为文本")
                result = self.server.store.import_accounts(parse_accounts(text))
                self.server.service.forget()
                self.server.monitor.wake()
                self.send_data(200, result)
            elif path == "/api/accounts/action":
                ids = validate_ids(payload.get("ids"), limit=10000)
                action = payload.get("action")
                if action == "purge":
                    if payload.get("confirmed") is not True:
                        raise MailError("彻底删除无法恢复，请先确认删除内容")
                    result = self.server.store.purge_accounts(ids)
                else:
                    result = self.server.store.update_accounts(ids, action)
                if action in ("delete", "purge"):
                    self.server.monitor.remove(ids)
                    self.server.service.forget(ids)
                self.send_data(200, result)
            elif path == "/api/monitor/start":
                self.send_data(200, self.server.monitor.start(payload.get("ids"), payload.get("protocol", "graph"), payload.get("interval", 10)))
            elif path == "/api/monitor/stop":
                self.send_data(200, self.server.monitor.stop())
            elif path == "/api/fetch":
                account_id = int(payload.get("account_id", 0))
                self.send_data(200, self.server.service.fetch(account_id, payload.get("protocol", "graph")))
            else:
                self.send_data(404, {"error": "接口不存在"})
        except (MailError, ValueError, TypeError, UnicodeError) as exc:
            message = str(exc) if isinstance(exc, MailError) else "请求格式错误"
            self.send_data(400, {"error": message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_data(500, {"error": "本地服务处理失败，请重试；账号凭证不会写入日志"})


class LocalServer(ThreadingHTTPServer):
    def server_close(self):
        if hasattr(self, "monitor"):
            self.monitor.close()
        super().server_close()


def make_server(port, store, diagnostics=None):
    server = LocalServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.store = store
    server.diagnostics = diagnostics or DiagnosticLog()
    store.diagnostics = server.diagnostics
    server.service = MailService(store, server.diagnostics)
    server.monitor = BatchMonitor(store, server.service)
    server.csrf_token = secrets.token_urlsafe(32)
    return server


def main():
    parser = argparse.ArgumentParser(description=f"本地邮箱验证码工具：批量导入，每批最多监听 {MAX_BATCH_SIZE} 个邮箱")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=ROOT / ".mailtool" / "accounts.sqlite3")
    parser.add_argument("--open", action="store_true", help="启动时打开浏览器")
    parser.add_argument("--diagnostics", action="store_true", help="启用带访问码的局域网只读诊断服务")
    parser.add_argument("--diagnostics-port", type=int, default=8767)
    parser.add_argument("--diagnostics-bind", default="0.0.0.0")
    args = parser.parse_args()
    server = remote = None
    diagnostics = DiagnosticLog(args.data.parent / "diagnostics.jsonl" if args.diagnostics else None)
    try:
        server = make_server(args.port, Store(args.data), diagnostics)
        if args.diagnostics:
            remote = make_diagnostic_server(args.diagnostics_port, server, args.diagnostics_bind)
            threading.Thread(target=remote.serve_forever, daemon=True, name="diagnostics").start()
    except OSError as exc:
        if remote:
            remote.server_close()
        if server:
            server.server_close()
        diagnostics.close()
        parser.exit(1, "启动失败：%s。主页面可用 --port 8766 更换端口，诊断可用 --diagnostics-port 8768。\n" % exc)
    print("收码台版本：%s" % APP_VERSION, flush=True)
    print("邮箱验证码工具已启动：http://127.0.0.1:%s" % server.server_port, flush=True)
    print("按 Ctrl+C 退出；账号和轮换令牌保存在本机。", flush=True)
    diagnostics.emit("service_started", port=server.server_port)
    if remote:
        diagnostics.emit("diagnostics_started", port=remote.server_port)
        print("\n局域网诊断已启用（只读，需访问码）：", flush=True)
        for address in local_ipv4_addresses() or ["127.0.0.1"]:
            print("诊断地址：http://%s:%s" % (address, remote.server_port), flush=True)
        print("只读访问码：%s" % remote.access_key, flush=True)
        print("把诊断地址和只读访问码发给协助排查的人；重启后访问码会变化。", flush=True)
        print("日志路径：%s\n日志不含验证码、密码、令牌、邮箱地址或邮件正文。\n" %
              (args.data.parent / "diagnostics.jsonl"), flush=True)
    if args.open:
        import webbrowser
        webbrowser.open("http://127.0.0.1:%s" % server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if remote:
            remote.shutdown()
            remote.server_close()
        server.server_close()
        diagnostics.close()


if __name__ == "__main__":
    main()
