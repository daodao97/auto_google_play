"""Contract tests for the mail.xcaigc.com tRPC adapter.

These tests intentionally mock the requests boundary: they define the API
contract without contacting the real mailbox service.
"""

from __future__ import annotations

import base64
import json
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from claude_register.mail import fetcher as mail_client


MAIL_API_BASE = "https://mail.xcaigc.com"
TRPC_BASE = f"{MAIL_API_BASE}/api/trpc"
FETCH_URL = f"{TRPC_BASE}/mail.fetch?batch=1"
GRAPH_FETCH_URL = f"{TRPC_BASE}/mail.fetchMsGraphByCredential?batch=1"
MESSAGE_URL = f"{TRPC_BASE}/mail.message?batch=1"


def _response(payload: object, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = json.dumps(payload)
    response.json.return_value = payload
    return response


def _success(data: object) -> list[dict[str, object]]:
    return [{"result": {"data": {"json": data}}}]


class TestXcaigcMailContract(unittest.TestCase):
    def test_default_mail_api_base_is_xcaigc(self):
        self.assertEqual(getattr(mail_client, "MAIL_API_BASE", None), MAIL_API_BASE)

    def test_polling_uses_a_small_recent_message_window(self):
        self.assertLessEqual(mail_client.MAIL_FETCH_LIMIT, 20)

    def test_platform_console_magic_link_is_recognized(self):
        encoded_email = base64.b64encode(b"person@kissfans.com").decode("ascii")
        magic_url = f"https://platform.claude.com/magic-link#abc123:{encoded_email}"

        result = mail_client._extract_magic_link_from_text(
            f'<a href="{magic_url}">Open Claude Console</a>'
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["nonce"], "abc123")
        self.assertEqual(result["encoded_email_address"], encoded_email)

    def test_prime_seen_posts_mail_fetch_batch_and_returns_existing_ids(self):
        inbox = {
            "sessionId": "session-1",
            "email": "old@mail.com",
            "count": 2,
            "sourceHost": "mail.com",
            "messages": [
                {"mailId": "old-1", "sender": "Anthropic"},
                {"mailId": "old-2", "fromAddress": "noreply@anthropic.com"},
            ],
        }

        with patch.object(mail_client.requests, "post", return_value=_response(_success(inbox))) as post:
            seen = mail_client.prime_seen(
                "old@mail.com",
                "mail-password",
                base_url=MAIL_API_BASE,
                provider="mailcom",
            )

        self.assertEqual(seen, {"old-1", "old-2"})
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], FETCH_URL)
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "0": {
                    "json": {
                        "credential": "old@mail.com:mail-password",
                        "limit": 20,
                        "provider": "mailcom",
                    }
                }
            },
        )

    def test_removed_auto_provider_is_rejected_for_mailcom_aliases(self):
        with patch.object(mail_client.requests, "post") as post:
            with self.assertRaisesRegex(ValueError, "mailcom.*imap.*microsoft"):
                mail_client.prime_seen(
                    "person@reggaefan.com",
                    "mail-password",
                    base_url=MAIL_API_BASE,
                    provider="auto",
                )

        post.assert_not_called()

    def test_fetch_magic_link_uses_list_then_message_and_filters_seen_and_old_mail(self):
        encoded_email = base64.b64encode(b"person@zohomail.com").decode("ascii")
        magic_url = f"https://claude.ai/magic-link#abc123:{encoded_email}"
        inbox = {
            "sessionId": "session-2",
            "email": "person@zohomail.com",
            "count": 3,
            "sourceHost": "imap.zoho.com",
            "messages": [
                {
                    "mailId": "already-seen",
                    "sender": "Anthropic",
                    "subjectPreview": "Sign in to Claude",
                    "dateOrTime": "Fri, 10 Jul 2026 12:00:00 +0000",
                },
                {
                    "mailId": "too-old",
                    "fromAddress": "noreply@anthropic.com",
                    "subjectPreview": "Sign in to Claude",
                    "dateOrTime": "Fri, 10 Jul 2026 09:00:00 +0000",
                },
                {
                    "mailId": "new-message",
                    "sender": "Anthropic",
                    "subjectPreview": "Sign in to Claude",
                    "dateOrTime": "Fri, 10 Jul 2026 11:00:00 +0000",
                },
            ],
        }
        detail = {
            "bodyText": "",
            "bodyHtml": f'<a href="{magic_url}">Sign in</a>',
            "links": [],
        }
        not_before_ms = datetime(2026, 7, 10, 10, tzinfo=timezone.utc).timestamp() * 1000

        with patch.object(
            mail_client.requests,
            "post",
            side_effect=[_response(_success(inbox)), _response(_success(detail))],
        ) as post, patch.object(
            mail_client,
            "_imap_connect",
            create=True,
            side_effect=AssertionError("legacy IMAP path must not be used"),
        ):
            result = mail_client.fetch_magic_link(
                "person@zohomail.com",
                "mail-password",
                base_url=MAIL_API_BASE,
                provider="imap",
                seen={"already-seen"},
                not_before_ms=not_before_ms,
                poll_interval=0,
                poll_timeout=1,
            )

        self.assertEqual(result["nonce"], "abc123")
        self.assertEqual(result["email_address"], "person@zohomail.com")
        self.assertEqual(result["mail_id"], "new-message")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0], FETCH_URL)
        self.assertEqual(
            post.call_args_list[0].kwargs["json"],
            {
                "0": {
                    "json": {
                        "credential": "person@zohomail.com:mail-password",
                        "limit": 20,
                        "provider": "imap",
                    }
                }
            },
        )
        self.assertEqual(post.call_args_list[1].args[0], MESSAGE_URL)
        self.assertEqual(
            post.call_args_list[1].kwargs["json"],
            {"0": {"json": {"sessionId": "session-2", "mailId": "new-message"}}},
        )

    def test_microsoft_credentials_use_graph_fetch_then_message(self):
        encoded_email = base64.b64encode(b"person@outlook.com").decode("ascii")
        magic_url = f"https://claude.ai/magic-link#def456:{encoded_email}"
        inbox = {
            "sessionId": "graph-session",
            "email": "person@outlook.com",
            "count": 1,
            "sourceHost": "graph.microsoft.com",
            "messages": [
                {
                    "mailId": "graph-mail-1",
                    "fromAddress": "noreply@anthropic.com",
                    "subjectPreview": "Your Claude login link",
                    "dateOrTime": "Fri, 10 Jul 2026 11:00:00 +0000",
                }
            ],
        }
        detail = {"bodyText": magic_url, "bodyHtml": "", "links": []}

        with patch.object(
            mail_client.requests,
            "post",
            side_effect=[_response(_success(inbox)), _response(_success(detail))],
        ) as post:
            result = mail_client.fetch_magic_link(
                "person@outlook.com",
                "mail-password",
                base_url=MAIL_API_BASE,
                provider="microsoft",
                client_id="client-id",
                refresh_token="refresh-token",
                not_before_ms=datetime(2026, 7, 10, 10, tzinfo=timezone.utc).timestamp() * 1000,
                poll_interval=0,
                poll_timeout=1,
            )

        self.assertEqual(result["nonce"], "def456")
        self.assertEqual(result["mail_id"], "graph-mail-1")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0], GRAPH_FETCH_URL)
        self.assertEqual(
            post.call_args_list[0].kwargs["json"],
            {
                "0": {
                    "json": {
                        "credential": (
                            "person@outlook.com----mail-password----client-id----refresh-token"
                        ),
                        "limit": 20,
                    }
                }
            },
        )
        self.assertEqual(post.call_args_list[1].args[0], MESSAGE_URL)
        self.assertEqual(
            post.call_args_list[1].kwargs["json"],
            {"0": {"json": {"sessionId": "graph-session", "mailId": "graph-mail-1"}}},
        )

    def test_magic_link_for_another_email_is_skipped(self):
        other_encoded = base64.b64encode(b"other@example.com").decode("ascii")
        target_encoded = base64.b64encode(b"person@example.com").decode("ascii")
        inbox = {
            "sessionId": "session-3",
            "email": "person@example.com",
            "count": 2,
            "sourceHost": "mail.com",
            "messages": [
                {"mailId": "other", "sender": "Anthropic", "subjectPreview": "Claude login"},
                {"mailId": "target", "sender": "Anthropic", "subjectPreview": "Claude login"},
            ],
        }
        other_detail = {
            "bodyText": f"https://claude.ai/magic-link#aaa111:{other_encoded}",
            "bodyHtml": "",
            "links": [],
        }
        target_detail = {
            "bodyText": f"https://claude.ai/magic-link#bbb222:{target_encoded}",
            "bodyHtml": "",
            "links": [],
        }

        with patch.object(
            mail_client.requests,
            "post",
            side_effect=[
                _response(_success(inbox)),
                _response(_success(other_detail)),
                _response(_success(target_detail)),
            ],
        ):
            result = mail_client.fetch_magic_link(
                "person@example.com",
                "mail-password",
                base_url=MAIL_API_BASE,
                provider="mailcom",
                poll_interval=0,
                poll_timeout=1,
            )

        self.assertEqual(result["nonce"], "bbb222")
        self.assertEqual(result["mail_id"], "target")
        self.assertEqual(result["email_address"], "person@example.com")

    def test_microsoft_prime_seen_does_not_exchange_refresh_token(self):
        with patch.object(mail_client.requests, "post") as post:
            seen = mail_client.prime_seen(
                "person@outlook.com",
                "mail-password",
                base_url=MAIL_API_BASE,
                provider="microsoft",
                client_id="client-id",
                refresh_token="refresh-token",
            )

        self.assertEqual(seen, set())
        post.assert_not_called()

    def test_microsoft_fetch_requires_both_client_id_and_refresh_token(self):
        with patch.object(mail_client.requests, "post") as post:
            with self.assertRaisesRegex(
                mail_client.MailFetcherFatalError,
                "refresh_token",
            ):
                mail_client.fetch_magic_link(
                    "person@outlook.com",
                    "mail-password",
                    provider="microsoft",
                    client_id="client-id",
                    poll_interval=0,
                    poll_timeout=1,
                )

        post.assert_not_called()

    def test_trpc_error_message_is_preserved_in_the_public_exception(self):
        error = [{"error": {"json": {"message": "credential rejected by mailbox"}}}]

        with patch.object(mail_client.requests, "post", return_value=_response(error)):
            with self.assertRaisesRegex(RuntimeError, "credential rejected by mailbox"):
                mail_client.fetch_magic_link(
                    "person@mail.com",
                    "bad-password",
                    base_url=MAIL_API_BASE,
                    provider="mailcom",
                    max_fetch_errors=1,
                    poll_interval=0,
                    poll_timeout=1,
                )

    def test_mail_service_url_is_fixed_to_xcaigc_https(self):
        with patch.object(mail_client.requests, "post") as post:
            with self.assertRaisesRegex(ValueError, "mail.xcaigc.com"):
                mail_client.prime_seen(
                    "person@mail.com",
                    "mail-password",
                    base_url="http://127.0.0.1:8787",
                    provider="mailcom",
                )
        post.assert_not_called()

    def test_removed_legacy_mail_options_are_rejected_before_remote_fetch(self):
        removed_options = {
            "imap_host": "imap.example.net",
            "app_token": "legacy-app-token",
            "client_secret": "legacy-client-secret",
        }

        for option, value in removed_options.items():
            with self.subTest(option=option), \
                 patch.object(mail_client, "_fetch_inbox", return_value=("session", [])) as fetch:
                with self.assertRaisesRegex(ValueError, option):
                    mail_client.prime_seen(
                        "person@example.net",
                        "mail-password",
                        provider="imap",
                        **{option: value},
                    )
                fetch.assert_not_called()

    def test_remote_error_redacts_the_submitted_password(self):
        error = [{"error": {"json": {"message": "login failed for secret-password"}}}]

        with patch.object(mail_client.requests, "post", return_value=_response(error)):
            with self.assertRaises(mail_client.MailFetcherFatalError) as caught:
                mail_client.fetch_magic_link(
                    "person@mail.com",
                    "secret-password",
                    base_url=MAIL_API_BASE,
                    provider="mailcom",
                    max_fetch_errors=1,
                    poll_interval=0,
                    poll_timeout=1,
                )

        self.assertNotIn("secret-password", str(caught.exception))

    def test_cancelled_fetch_stops_before_any_remote_request(self):
        cancel = threading.Event()
        cancel.set()

        with patch.object(mail_client.requests, "post") as post:
            with self.assertRaisesRegex(mail_client.MailFetcherFatalError, r"cancel|\u53d6\u6d88"):
                mail_client.fetch_magic_link(
                    "person@mail.com",
                    "mail-password",
                    base_url=MAIL_API_BASE,
                    provider="mailcom",
                    cancel_event=cancel,
                    poll_interval=0,
                    poll_timeout=1,
                )

        post.assert_not_called()

    def test_transient_detail_failures_recover_within_the_poll_budget(self):
        clock = {"now": 0.0}
        encoded_email = base64.b64encode(b"person@example.net").decode("ascii")
        magic_url = f"https://claude.ai/magic-link#abc123:{encoded_email}"
        detail_attempts = {"count": 0}

        def monotonic() -> float:
            return clock["now"]

        def fetch_inbox(*_args, **_kwargs):
            clock["now"] += 0.1
            return "session-detail-error", [
                {
                    "mailId": "candidate",
                    "sender": "Anthropic",
                    "subjectPreview": "Sign in to Claude",
                }
            ]

        def fetch_detail(*_args, **_kwargs):
            clock["now"] += 0.1
            detail_attempts["count"] += 1
            if detail_attempts["count"] <= 2:
                raise RuntimeError("detail unavailable")
            return {"bodyText": magic_url}

        def wait(seconds: float, _cancel) -> None:
            clock["now"] += seconds

        with patch.object(mail_client.time, "monotonic", side_effect=monotonic), \
             patch.object(mail_client, "_fetch_inbox", side_effect=fetch_inbox), \
             patch.object(mail_client, "_fetch_detail", side_effect=fetch_detail), \
             patch.object(mail_client, "_wait", side_effect=wait):
            result = mail_client.fetch_magic_link(
                "person@example.net",
                "mail-password",
                provider="imap",
                poll_interval=0.1,
                poll_timeout=10,
                max_fetch_errors=2,
            )

        self.assertEqual(result["mail_id"], "candidate")
        self.assertEqual(detail_attempts["count"], 3)

    def test_successful_decoy_detail_is_not_fetched_again_next_poll(self):
        encoded_email = base64.b64encode(b"person@example.net").decode("ascii")
        magic_url = f"https://claude.ai/magic-link#abc123:{encoded_email}"
        inbox_rounds = [
            ("session-1", [
                {"mailId": "decoy", "sender": "Anthropic", "subject": "Claude login"},
            ]),
            ("session-2", [
                {"mailId": "decoy", "sender": "Anthropic", "subject": "Claude login"},
                {"mailId": "target", "sender": "Anthropic", "subject": "Claude login"},
            ]),
        ]

        def detail(_base, _session_id, mail_id, **_kwargs):
            if mail_id == "target":
                return {"bodyText": magic_url}
            return {"bodyText": "No link here"}

        with patch.object(mail_client, "_fetch_inbox", side_effect=inbox_rounds), \
             patch.object(mail_client, "_fetch_detail", side_effect=detail) as fetch_detail, \
             patch.object(mail_client, "_wait"):
            result = mail_client.fetch_magic_link(
                "person@example.net",
                "mail-password",
                provider="imap",
                poll_interval=0,
                poll_timeout=1,
            )

        self.assertEqual(result["mail_id"], "target")
        self.assertEqual([call.args[2] for call in fetch_detail.call_args_list], ["decoy", "target"])

    def test_newest_candidate_is_inspected_first(self):
        encoded_email = base64.b64encode(b"person@example.net").decode("ascii")
        magic_url = f"https://claude.ai/magic-link#abc123:{encoded_email}"
        messages = [
            {
                "mailId": "old",
                "sender": "Anthropic",
                "subject": "Claude login",
                "dateOrTime": "2026-07-10T09:00:00Z",
            },
            {
                "mailId": "new",
                "sender": "Anthropic",
                "subject": "Claude login",
                "dateOrTime": "2026-07-10T10:00:00Z",
            },
        ]

        def detail(_base, _session_id, mail_id, **_kwargs):
            return {"bodyText": magic_url if mail_id == "new" else "No link"}

        with patch.object(mail_client, "_fetch_inbox", return_value=("session", messages)), \
             patch.object(mail_client, "_fetch_detail", side_effect=detail) as fetch_detail:
            result = mail_client.fetch_magic_link(
                "person@example.net",
                "mail-password",
                provider="imap",
                poll_interval=0,
                poll_timeout=1,
            )

        self.assertEqual(result["mail_id"], "new")
        fetch_detail.assert_called_once()
        self.assertEqual(fetch_detail.call_args.args[2], "new")

    def test_rate_limiter_gates_mailbox_list_and_message_fetch(self):
        encoded_email = base64.b64encode(b"person@example.net").decode("ascii")
        magic_url = f"https://claude.ai/magic-link#abc123:{encoded_email}"
        message = {"mailId": "target", "sender": "Anthropic", "subject": "Claude login"}
        limiter = MagicMock()

        with patch.object(mail_client, "_fetch_inbox", return_value=("session", [message])), \
             patch.object(mail_client, "_fetch_detail", return_value={"bodyText": magic_url}):
            result = mail_client.fetch_magic_link(
                "person@example.net",
                "mail-password",
                provider="mailcom",
                poll_interval=0,
                poll_timeout=1,
                rate_limiter=limiter,
            )

        self.assertEqual(result["mail_id"], "target")
        self.assertEqual(limiter.call_count, 2)

    def test_fast_path_skips_only_initial_mailbox_slots(self):
        encoded_email = base64.b64encode(b"person@example.net").decode("ascii")
        magic_url = f"https://claude.ai/magic-link#abc123:{encoded_email}"
        message = {"mailId": "target", "sender": "Anthropic", "subject": "Claude login"}
        limiter = MagicMock()

        with patch.object(mail_client, "_fetch_inbox", return_value=("session", [message])), \
             patch.object(mail_client, "_fetch_detail", return_value={"bodyText": magic_url}):
            result = mail_client.fetch_magic_link(
                "person@example.net",
                "mail-password",
                provider="mailcom",
                poll_interval=0,
                poll_timeout=1,
                rate_limiter=limiter,
                mail_fast_path=True,
            )

        self.assertEqual(result["mail_id"], "target")
        self.assertEqual(limiter.call_count, 0)

    def test_fast_path_keeps_limiter_after_initial_mailbox_slots(self):
        encoded_email = base64.b64encode(b"person@example.net").decode("ascii")
        magic_url = f"https://claude.ai/magic-link#abc123:{encoded_email}"
        decoy = {"mailId": "decoy", "sender": "Anthropic", "subject": "Claude login"}
        target = {"mailId": "target", "sender": "Anthropic", "subject": "Claude login"}
        limiter = MagicMock()

        with patch.object(
            mail_client,
            "_fetch_inbox",
            side_effect=[("session", [decoy]), ("session", [target])],
        ), patch.object(
            mail_client,
            "_fetch_detail",
            side_effect=[{"bodyText": "not a magic link"}, {"bodyText": magic_url}],
        ):
            result = mail_client.fetch_magic_link(
                "person@example.net",
                "mail-password",
                provider="mailcom",
                poll_interval=0,
                poll_timeout=1,
                rate_limiter=limiter,
                mail_fast_path=True,
            )

        self.assertEqual(result["mail_id"], "target")
        self.assertEqual(limiter.call_count, 2)

    def test_rate_limiter_deadline_exhaustion_is_transient(self):
        limiter = MagicMock(side_effect=TimeoutError("no request slot before deadline"))

        with self.assertRaises(mail_client.MailFetcherTransientError):
            mail_client.fetch_magic_link(
                "person@example.net",
                "mail-password",
                provider="mailcom",
                poll_interval=0,
                poll_timeout=1,
                rate_limiter=limiter,
            )

    def test_cancel_during_rate_limiter_wait_stays_fatal(self):
        cancel = threading.Event()

        def limiter(cancel_event, _deadline):
            cancel_event.set()
            raise TimeoutError("request slot wait cancelled")

        with self.assertRaisesRegex(mail_client.MailFetcherFatalError, "取消"):
            mail_client.fetch_magic_link(
                "person@example.net",
                "mail-password",
                provider="mailcom",
                poll_interval=0,
                poll_timeout=1,
                cancel_event=cancel,
                rate_limiter=limiter,
            )

    def test_mailcom_display_timestamp_is_parsed(self):
        parsed = mail_client._msg_datetime_ms("Friday, July 10, 2026 at 6:58 AM")
        expected = datetime(2026, 7, 10, 6, 58, tzinfo=timezone.utc).timestamp() * 1000

        self.assertEqual(parsed, expected)

    def test_invalid_mailcom_display_timestamp_is_treated_as_unknown(self):
        parsed = mail_client._msg_datetime_ms("Friday, February 31, 2026 at 6:58 AM")

        self.assertIsNone(parsed)

    def test_minute_precision_timestamp_allows_recent_mail(self):
        timestamp = datetime(2026, 7, 10, 6, 58, tzinfo=timezone.utc).timestamp() * 1000

        self.assertFalse(mail_client._is_before_not_before(timestamp, timestamp + 89_000))
        self.assertTrue(mail_client._is_before_not_before(timestamp, timestamp + 91_000))

    def test_unknown_timestamp_is_rejected_when_freshness_is_required(self):
        self.assertTrue(mail_client._is_before_not_before(None, 1_000.0))

    def test_unknown_timestamp_stays_eligible_for_manual_mail_fetch(self):
        self.assertFalse(mail_client._is_before_not_before(None, None))

    def test_inbox_request_timeout_is_bounded_by_remaining_poll_budget(self):
        observed_timeouts: list[float | None] = []

        def fetch_inbox(*_args, **kwargs):
            observed_timeouts.append(kwargs.get("timeout"))
            raise mail_client.MailFetcherFatalError("stop after observing timeout")

        with patch.object(mail_client.time, "monotonic", return_value=10.0), \
             patch.object(mail_client, "_fetch_inbox", side_effect=fetch_inbox):
            with self.assertRaisesRegex(mail_client.MailFetcherFatalError, "observing timeout"):
                mail_client.fetch_magic_link(
                    "person@example.net",
                    "mail-password",
                    provider="imap",
                    poll_interval=8,
                    poll_timeout=4,
                )

        self.assertEqual(len(observed_timeouts), 1)
        self.assertIsNotNone(observed_timeouts[0])
        self.assertGreater(observed_timeouts[0], 0)
        self.assertLessEqual(observed_timeouts[0], 4)

    def test_detail_request_timeout_uses_the_remaining_poll_budget(self):
        clock = {"now": 0.0}
        observed_timeouts: list[float | None] = []

        def monotonic() -> float:
            return clock["now"]

        def fetch_inbox(*_args, **_kwargs):
            clock["now"] = 3.75
            return "session-detail-budget", [
                {
                    "mailId": "candidate",
                    "sender": "Anthropic",
                    "subjectPreview": "Sign in to Claude",
                }
            ]

        def fetch_detail(*_args, **kwargs):
            observed_timeouts.append(kwargs.get("timeout"))
            raise mail_client.MailFetcherFatalError("stop after observing detail timeout")

        with patch.object(mail_client.time, "monotonic", side_effect=monotonic), \
             patch.object(mail_client, "_fetch_inbox", side_effect=fetch_inbox), \
             patch.object(mail_client, "_fetch_detail", side_effect=fetch_detail):
            with self.assertRaisesRegex(mail_client.MailFetcherFatalError, "detail timeout"):
                mail_client.fetch_magic_link(
                    "person@example.net",
                    "mail-password",
                    provider="imap",
                    poll_interval=8,
                    poll_timeout=4,
                )

        self.assertEqual(len(observed_timeouts), 1)
        self.assertIsNotNone(observed_timeouts[0])
        self.assertGreater(observed_timeouts[0], 0)
        self.assertLessEqual(observed_timeouts[0], 0.25)

    def test_final_poll_sleep_does_not_exceed_remaining_budget(self):
        clock = {"now": 0.0}
        observed_sleeps: list[float] = []

        def monotonic() -> float:
            return clock["now"]

        def fetch_inbox(*_args, **_kwargs):
            clock["now"] += 3.0
            return "session-empty", []

        def wait(seconds: float, _cancel) -> None:
            observed_sleeps.append(seconds)
            clock["now"] += seconds

        with patch.object(mail_client.time, "monotonic", side_effect=monotonic), \
             patch.object(mail_client, "_fetch_inbox", side_effect=fetch_inbox), \
             patch.object(mail_client, "_wait", side_effect=wait):
            with self.assertRaises(TimeoutError):
                mail_client.fetch_magic_link(
                    "person@example.net",
                    "mail-password",
                    provider="imap",
                    poll_interval=8,
                    poll_timeout=4,
                )

        self.assertEqual(len(observed_sleeps), 1)
        self.assertAlmostEqual(observed_sleeps[0], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
