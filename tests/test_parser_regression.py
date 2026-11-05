import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

import mailtool as m

SUBJECT = "[Example] Login Verification - 2026-03-14 09:26:53(UTC)"
BODY = """[https://images.example.invalid/login-banner.png]
Login Verification
Your verification code
654321
This code expires in 15 minutes.
If you did not request it, review your account activity."""


class CodeRegressionTests(unittest.TestCase):
    def test_labelled_body_code_beats_subject_year(self):
        result = m.extract_code(SUBJECT, BODY)
        self.assertEqual(result["code"], "654321")
        self.assertEqual(result["code_confidence"], "high")

    def test_timestamp_never_becomes_a_code_but_real_four_digit_year_like_code_can(self):
        self.assertEqual(m.extract_code(SUBJECT, "")["code"], "")
        for date in ("2026/03/14", "2026.03.14", "2026年03月14日"):
            self.assertEqual(m.extract_code("Login Verification - " + date, "")["code"], "")
        self.assertEqual(m.extract_code("Verification code", "Your code: 2026")["code"], "2026")

    def test_tracking_link_and_unrelated_number_do_not_beat_label(self):
        text = "Order 123456\nhttps://tracking.invalid/click?reference=999999\nYour verification code\n654321"
        self.assertEqual(m.extract_code(SUBJECT, text)["code"], "654321")

    def test_subject_label_unicode_and_zero_width_code_formats(self):
        for subject, body, expected in (
            ("Your verification code", "1234", "1234"),
            ("", "Your verification code: １２３４５６", "123456"),
            ("", "Your verification code: 123\u200b456", "123456"),
        ):
            with self.subTest(body=body):
                self.assertEqual(m.extract_code(subject, body)["code"], expected)

    def test_imap_mime_extracts_a_labelled_code_from_plain_text_and_html(self):
        for kind in ("plain", "html"):
            message = EmailMessage()
            message["Subject"] = SUBJECT
            if kind == "plain":
                message.set_content(BODY)
            else:
                message.set_content(
                    "<h1>Login Verification</h1><div>Your verification code</div>"
                    "<strong>654321</strong><p>This code expires in 15 minutes.</p>",
                    subtype="html",
                )
            result = m.parse_mime(message.as_bytes(), "1", "收件箱")
            self.assertEqual(result["code"], "654321")

    def test_existing_cache_and_deduplicated_event_are_corrected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = m.Store(Path(directory) / "test.sqlite3")
            store.import_accounts(m.parse_accounts("sample@example.com----x----c----t"))
            message = {"id": "same-mail", "subject": SUBJECT, "body": BODY, "code": "2026",
                       "code_confidence": "high", "received_at": m.utc_now()}
            started = m.utc_now()
            store.save_messages(1, [message], "imap", [])
            store.record_codes(1, [message], "imap", started)
            previous = store.code_events([1], started)[0]
            self.assertEqual(store.cached(1)["messages"][0]["code"], "654321")
            corrected = store.code_events([1], started)
            self.assertEqual(len(corrected), 1)
            self.assertEqual(corrected[0]["code"], "654321")
            self.assertEqual(corrected[0]["id"], previous["id"])
            self.assertEqual(corrected[0]["discovered_at"], previous["discovered_at"])


if __name__ == "__main__":
    unittest.main()
