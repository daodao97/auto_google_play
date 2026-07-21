"""注册相关代码的最小测试集——覆盖 Round 1 修复点：去重/校验、代理唯一性、
重试分类、身份稳定性、partial 事务语义。

运行：python3 -m unittest tests.test_registration
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import MagicMock, patch

from claude_register.challenge import arkose
from claude_register.config import dynamic as dynamic_config
from claude_register.mail import fetcher as mail_client
from claude_register.onboarding import service as onboarding
from claude_register.orchestration import service as orch
from claude_register.auth import service as register
from claude_register.presentation import web as webui
from claude_register.core.browser import (
    BrowserProfile,
    build_browser_headers,
    fetch_proxy_exit_ip,
    init_browser_cookies,
    materialize_session_proxy,
    new_browser_identity,
    new_browser_runtime,
    redact_proxy_url,
)
from claude_register.onboarding.service import onboarding_failed_steps


class TestExternalRunGuard(unittest.TestCase):
    @staticmethod
    def _printed_text(mock_print: MagicMock) -> str:
        return "\n".join(" ".join(str(value) for value in call.args)
                         for call in mock_print.call_args_list)

    def test_external_run_is_blocked_without_exact_confirmation(self):
        from claude_register.core.run_guard import ExternalRunNotConfirmed, require_external_confirmation

        with self.assertRaises(ExternalRunNotConfirmed):
            require_external_confirmation([])
        with self.assertRaises(ExternalRunNotConfirmed):
            require_external_confirmation(["--confirm"])

    def test_external_run_accepts_exact_confirmation_flag(self):
        from claude_register.core.run_guard import CONFIRM_FLAG, confirmed_external_args, require_external_confirmation

        require_external_confirmation([CONFIRM_FLAG])
        self.assertEqual(confirmed_external_args(["session-value", CONFIRM_FLAG, "proxy-value"]),
                         ("session-value", "proxy-value"))

    def test_external_entrypoints_stop_before_loading_config(self):
        from claude_register.cli import full_run as run_full
        from claude_register.diagnostics import gt2_probe as test_gt2
        from claude_register.diagnostics import stage0_probe as test_stage0

        for module in (run_full, test_gt2, test_stage0):
            with self.subTest(module=module.__name__), \
                 patch("sys.argv", [module.__name__]), \
                 patch.object(module, "load_config") as load, \
                 patch("builtins.print"):
                self.assertEqual(module.main(), 2)
                load.assert_not_called()

    def test_core_cli_entrypoints_stop_before_external_work(self):
        from claude_register.compliance import kyc

        entrypoints = (
            (register, "load_config"),
            (orch, "json"),
            (mail_client, "fetch_magic_link"),
            (kyc, "check_kyc_status"),
            (onboarding, "session_from_cookie_string"),
        )
        for module, boundary_name in entrypoints:
            boundary = getattr(module, boundary_name)
            patch_target = patch.object(module, boundary_name)
            if module is orch:
                patch_target = patch.object(boundary, "load")
            with self.subTest(module=module.__name__), \
                 patch("sys.argv", [module.__name__]), \
                 patch_target as external_boundary, \
                 patch("builtins.print"):
                external_boundary.side_effect = AssertionError("external boundary reached")
                self.assertEqual(module.main(), 2)
                external_boundary.assert_not_called()

    def test_mail_cli_error_does_not_echo_exception_details(self):
        from claude_register.core.run_guard import CONFIRM_FLAG

        with patch("sys.argv", ["mail_fetcher_client", CONFIRM_FLAG, "a@x.com", "p"]), \
             patch.object(mail_client, "fetch_magic_link",
                          side_effect=RuntimeError("SECRET_MAIL_EXCEPTION")), \
             patch("builtins.print") as output:
            self.assertEqual(mail_client.main(), 2)

        self.assertNotIn("SECRET_MAIL_EXCEPTION", self._printed_text(output))

    def test_onboarding_cli_does_not_print_step_values(self):
        from claude_register.core.run_guard import CONFIRM_FLAG

        with patch("sys.argv", ["onboarding", CONFIRM_FLAG]), \
             patch.object(onboarding, "session_from_cookie_string", return_value=MagicMock()), \
             patch.object(onboarding, "run_onboarding", return_value={
                 "start_onboarding": True,
                 "first_chat": "SECRET_CONVERSATION",
                 "finish_onboarding": True,
             }), \
             patch("builtins.print") as output:
            self.assertEqual(onboarding.main(), 0)

        printed = self._printed_text(output)
        self.assertNotIn("SECRET_CONVERSATION", printed)
        self.assertIn("onboarding_completed=True", printed)

    def test_stage0_probe_does_not_print_identity_or_response_data(self):
        from claude_register.diagnostics import stage0_probe as test_stage0
        from claude_register.core.run_guard import CONFIRM_FLAG

        profile = MagicMock()
        profile.color_scheme = "light"
        with patch("sys.argv", ["test_stage0", CONFIRM_FLAG]), \
             patch.object(test_stage0, "load_config", return_value={
                 "proxy_template": "http://proxy", "email": "test@example.invalid"
             }), \
             patch.object(test_stage0, "materialize_session_proxy", return_value=("http://proxy", "sid")), \
             patch.object(test_stage0, "random_browser_profile", return_value=profile), \
             patch.object(test_stage0, "new_browser_runtime", return_value=MagicMock()), \
             patch.object(test_stage0, "build_session", return_value=MagicMock()), \
             patch.object(test_stage0, "new_identity", return_value=("SECRET_ANON", "SECRET_DEVICE")), \
             patch.object(test_stage0, "init_browser_cookies"), \
             patch.object(test_stage0, "ip_check"), \
             patch.object(test_stage0, "login_methods", return_value=["magic_link"]), \
             patch.object(test_stage0, "send_magic_link", return_value={
                 "sent": True, "detail": "SECRET_RESPONSE"
             }), \
             patch("builtins.print") as output:
            self.assertEqual(test_stage0.main(), 0)

        printed = self._printed_text(output)
        for secret in ("SECRET_ANON", "SECRET_DEVICE", "SECRET_RESPONSE"):
            self.assertNotIn(secret, printed)

    def test_gt2_probe_does_not_print_body_or_token(self):
        from claude_register.diagnostics import gt2_probe as test_gt2
        from claude_register.core.run_guard import CONFIRM_FLAG

        profile = MagicMock()
        profile.accept_language = "en-US"
        profile.sec_ch_ua = '"Chromium"'
        profile.platform = '"macOS"'
        profile.ua = "Test UA"
        response = MagicMock(status_code=200, text="SECRET_BODY")
        response.json.return_value = {"token": "SECRET_TOKEN"}
        session = MagicMock()
        session.post.return_value = response
        with patch("sys.argv", ["test_gt2", CONFIRM_FLAG]), \
             patch.object(test_gt2, "load_config", return_value={"proxy_template": "http://proxy"}), \
             patch.object(test_gt2, "materialize_session_proxy", return_value=("http://proxy", "sid")), \
             patch.object(test_gt2, "random_browser_profile", return_value=profile), \
             patch.object(test_gt2, "build_session", return_value=session), \
             patch("builtins.print") as output:
            self.assertEqual(test_gt2.main(), 0)

        printed = self._printed_text(output)
        self.assertNotIn("SECRET_BODY", printed)
        self.assertNotIn("SECRET_TOKEN", printed)

    def test_full_run_does_not_print_challenge_or_account_details(self):
        from claude_register.cli import full_run as run_full
        from claude_register.core.run_guard import CONFIRM_FLAG

        summary = {
            "send": {"sent": True},
            "magic_link": {"nonce": "SECRET_NONCE"},
            "verify": {"created": True, "account": {"uuid": "SECRET_ACCOUNT"}},
            "onboarding": {"detail": "SECRET_ONBOARDING"},
            "session_key": "test-session-key",
        }
        profile = MagicMock(impersonate="chrome", platform="macOS")
        with tempfile.TemporaryDirectory() as td, \
             patch("sys.argv", ["run_full", CONFIRM_FLAG]), \
             patch.object(run_full, "OUTPUT_FILE", str(Path(td) / "results.txt")), \
             patch.object(run_full, "load_config", return_value={
                 "proxy_template": "http://proxy",
                 "email": "test@example.invalid",
                 "password": "test-password",
             }), \
             patch.object(run_full, "materialize_session_proxy", return_value=("http://proxy", "sid")), \
             patch.object(run_full, "random_browser_profile", return_value=profile), \
             patch.object(run_full, "build_session", return_value=MagicMock()), \
             patch.object(run_full, "random_name_parts", return_value=("Test", "Test User")), \
             patch.object(run_full, "register", return_value=summary), \
             patch("builtins.print") as output:
            self.assertEqual(run_full.main(), 0)

        printed = self._printed_text(output)
        for secret in ("SECRET_NONCE", "SECRET_ACCOUNT", "SECRET_ONBOARDING"):
            self.assertNotIn(secret, printed)
        self.assertIn("onboarding completed=False", printed)


class TestParseAccounts(unittest.TestCase):
    def test_dedup_keep_first(self):
        accs = orch.parse_accounts("a@x.com----p1----alice\na@x.com----p2\n")
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0].password, "p1")  # 保留首次，防重复注册

    def test_case_insensitive_dedup(self):
        accs = orch.parse_accounts("A@x.com----p1\na@x.com----p2\n")
        self.assertEqual(len(accs), 1)

    def test_skip_invalid_and_empty(self):
        accs = orch.parse_accounts("# 注释\nbadline\na@x.com----p1\nb@x.com----\n@no.com----p\n  \n")
        self.assertEqual([a.email for a in accs], ["a@x.com"])

    def test_default_display_name(self):
        with patch("claude_register.orchestration.service.random_american_name", return_value="Jane Smith"):
            accs = orch.parse_accounts("alice@mail.com----p1\n")
        self.assertEqual(accs[0].display_name, "Jane Smith")

    def test_microsoft_token_account_keeps_token_out_of_display_name(self):
        line = "a@outlook.com----pwd----11111111-2222-3333-4444-555555555555----refresh-token-value----Alice"
        accs = orch.parse_accounts(line)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0].password, "pwd")
        self.assertEqual(accs[0].mail_refresh_token, "refresh-token-value")
        self.assertEqual(accs[0].mail_client_id, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(accs[0].display_name, "Alice")

    def test_microsoft_token_account_legacy_reversed_order(self):
        line = "a@outlook.com----pwd----refresh-token-value----11111111-2222-3333-4444-555555555555----Alice"
        accs = orch.parse_accounts(line)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0].password, "pwd")
        self.assertEqual(accs[0].mail_refresh_token, "refresh-token-value")
        self.assertEqual(accs[0].mail_client_id, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(accs[0].display_name, "Alice")

    def test_microsoft_token_account_without_password(self):
        line = "a@outlook.com----refresh-token-value----11111111-2222-3333-4444-555555555555"
        with patch("claude_register.orchestration.service.random_american_name", return_value="Jane Smith"):
            accs = orch.parse_accounts(line)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0].password, "")
        self.assertEqual(accs[0].mail_refresh_token, "refresh-token-value")
        self.assertEqual(accs[0].mail_client_id, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(accs[0].display_name, "Jane Smith")

    def test_microsoft_token_account_key_value_format(self):
        line = "a@outlook.com----pwd----refresh_token=rt----client_id=cid----display_name=Alice"
        accs = orch.parse_accounts(line)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0].mail_refresh_token, "rt")
        self.assertEqual(accs[0].mail_client_id, "cid")
        self.assertEqual(accs[0].display_name, "Alice")

    def test_microsoft_token_account_four_part_format_allows_plain_client_id(self):
        line = "a@outlook.com----pwd----cid----rt"
        with patch("claude_register.orchestration.service.random_american_name", return_value="Jane Smith"):
            accs = orch.parse_accounts(line)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0].password, "pwd")
        self.assertEqual(accs[0].mail_client_id, "cid")
        self.assertEqual(accs[0].mail_refresh_token, "rt")
        self.assertEqual(accs[0].display_name, "Jane Smith")

    def test_uuid_shaped_password_is_not_misread_as_client_id(self):
        line = "a@x.com----550e8400-e29b-41d4-a716-446655440000----Alice"
        accs = orch.parse_accounts(line)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0].password, "550e8400-e29b-41d4-a716-446655440000")
        self.assertEqual(accs[0].mail_client_id, "")
        self.assertEqual(accs[0].display_name, "Alice")

    def test_account_line_records_delivery_prefix_for_microsoft_output(self):
        line = "a@outlook.com----real-pass----client_id=cid----refresh_token=rt----display_name=Alice"
        accs = orch.parse_accounts(line)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0].deliver_prefix, "a@outlook.com----real-pass----cid----rt")


class TestSessionProxy(unittest.TestCase):
    def test_uniqueness_and_placeholder(self):
        t = "http://u_session-{session}_life-1:p@h:1"
        a = materialize_session_proxy(t)
        b = materialize_session_proxy(t)
        self.assertIsNotNone(a)
        self.assertNotEqual(a[1], b[1])           # 每次不同 session id
        self.assertNotIn("{session}", a[0])        # 占位符已替换
        self.assertIn(a[1], a[0])

    def test_empty_and_no_placeholder(self):
        self.assertIsNone(materialize_session_proxy(""))
        self.assertIsNone(materialize_session_proxy(None))
        c = materialize_session_proxy("http://u:p@h:1")
        self.assertEqual(c, ("http://u:p@h:1", ""))

    def test_redact_proxy_url(self):
        self.assertEqual(redact_proxy_url("http://u:p@h:1"), "http://***:***@h:1")
        self.assertEqual(redact_proxy_url("host:1234:user:pass"), "host:1234:***:***")
        self.assertEqual(redact_proxy_url("http://h:1"), "http://h:1")


class TestProxyExitIp(unittest.TestCase):
    class Response:
        def __init__(self, text: str, status: int = 200):
            self.text = text
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("http error")

    def test_probe_accepts_only_a_valid_ip(self):
        session = MagicMock()
        session.get.return_value = self.Response(" 203.0.113.9\n")

        self.assertEqual(fetch_proxy_exit_ip(session, timeout=5), "203.0.113.9")
        session.get.assert_called_once()
        self.assertEqual(session.get.call_args.kwargs["timeout"], 5)

    def test_probe_normalizes_ipv6_and_rejects_non_ip_response(self):
        session = MagicMock()
        session.get.side_effect = [
            self.Response("2001:0db8:0:0:0:0:0:1"),
            self.Response("<html>gateway error</html>"),
        ]

        self.assertEqual(fetch_proxy_exit_ip(session), "2001:db8::1")
        self.assertEqual(fetch_proxy_exit_ip(session), "")


class TestRetryable(unittest.TestCase):
    def test_retryable(self):
        for msg in ("Connection reset", "HTTP 429", "timed out", "curl: (35) ssl", "HTTP 503"):
            self.assertTrue(orch._is_retryable(RuntimeError(msg)), msg)

    def test_not_retryable(self):
        for msg in ("HTTP 403", "verify 失败", "arkose 无配置"):
            self.assertFalse(orch._is_retryable(RuntimeError(msg)), msg)

    def test_not_retryable_for_config_and_certificate_errors(self):
        for msg in (
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
            "curl: (1) unsupported protocol",
            "curl: (56) CONNECT tunnel failed, response 400",
            "HTTP 407 Proxy Authentication Required",
        ):
            self.assertFalse(orch._is_retryable(RuntimeError(msg)), msg)

    def test_safe_error_labels_do_not_echo_exception_details(self):
        from claude_register.core.diagnostics import safe_error_label

        cases = (
            (RuntimeError("verify HTTP 403: SECRET_BODY"), "http_403"),
            (RuntimeError("curl: (28) timed out via https://user:pass@proxy.invalid"), "timeout"),
            (TimeoutError("polling stopped after 180 seconds due timeout"), "timeout"),
            (RuntimeError("onboarding 失败: first_chat"), "onboarding_failed:first_chat"),
            (RuntimeError("onboarding 失败: SECRET_TOKEN"), "onboarding_failed"),
            (ValueError("SECRET_UNKNOWN"), "ValueError"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                label = safe_error_label(error)
                self.assertEqual(label, expected)
                self.assertNotIn("SECRET", label)


class TestIdentity(unittest.TestCase):
    def test_stable_when_passed(self):
        anon, did = register.new_identity()
        h1 = register._auth_headers(anonymous_id=anon, device_id=did)
        h2 = register._auth_headers(anonymous_id=anon, device_id=did)
        self.assertEqual(h1["anthropic-anonymous-id"], h2["anthropic-anonymous-id"])
        self.assertEqual(h1["anthropic-device-id"], h2["anthropic-device-id"])
        self.assertEqual(h1["anthropic-device-id"], did)

    def test_rotates_when_not_passed(self):
        h1 = register._auth_headers()
        h2 = register._auth_headers()
        self.assertNotEqual(h1["anthropic-device-id"], h2["anthropic-device-id"])

    def test_browser_runtime_headers_match_cookie(self):
        runtime = new_browser_runtime(
            activity_session_id="23cfeb08-9779-42e2-b733-d29c551711cb",
            datadog_trace_id="13456083532195625354",
        )
        session = MagicMock()
        init_browser_cookies(session, "anon", "dev", browser_runtime=runtime)
        session.cookies.set.assert_any_call(
            "activitySessionId", runtime.activity_session_id, domain=".claude.ai"
        )

        headers = register._auth_headers(
            anonymous_id="anon",
            device_id="dev",
            browser_runtime=runtime,
        )
        self.assertEqual(headers["x-activity-session-id"], runtime.activity_session_id)
        self.assertEqual(headers["x-datadog-trace-id"], runtime.datadog_trace_id)
        self.assertEqual(headers["x-datadog-sampling-priority"], "1")
        self.assertEqual(headers["tracestate"], "dd=s:1;o:rum")

    def test_traceparent_uses_datadog_trace_and_parent_ids(self):
        headers = build_browser_headers(
            client_sha="a" * 40,
            anonymous_id="anon",
            device_id="dev",
            sentry_trace_id="b" * 32,
            datadog_trace_id="13456083532195625354",
            datadog_parent_id="8693358186769430195",
        )
        self.assertEqual(
            headers["traceparent"],
            "00-0000000000000000babd9caddc80f98a-78a5033623a036b3-01",
        )

    def test_one_identity_reuses_session_cookie_fingerprint(self):
        identity = new_browser_identity()
        first_session = MagicMock()
        second_session = MagicMock()

        init_browser_cookies(
            first_session,
            identity.anonymous_id,
            identity.device_id,
            identity=identity,
        )
        init_browser_cookies(
            second_session,
            identity.anonymous_id,
            identity.device_id,
            identity=identity,
        )

        def cookie_values(session):
            return {call.args[0]: call.args[1] for call in session.cookies.set.call_args_list}

        first = cookie_values(first_session)
        second = cookie_values(second_session)
        for name in ("activitySessionId", "anthropic-device-id", "ajs_anonymous_id", "__ssid", "_fbp",
                     "anthropic-consent-preferences"):
            self.assertEqual(first[name], second[name], name)

    def test_auth_and_onboarding_resolve_the_same_identity(self):
        identity = new_browser_identity()
        headers = register._auth_headers(identity=identity)
        ctx = onboarding.OnboardingContext(
            session=MagicMock(),
            org_uuid="ou",
            display_name="Fixture",
            identity=identity,
        )
        onboarding_headers = onboarding._headers(ctx)

        for candidate in (headers, onboarding_headers):
            self.assertEqual(candidate["anthropic-anonymous-id"], identity.anonymous_id)
            self.assertEqual(candidate["anthropic-device-id"], identity.device_id)
            self.assertEqual(candidate["x-activity-session-id"], identity.runtime.activity_session_id)
            self.assertEqual(candidate["x-datadog-trace-id"], identity.runtime.datadog_trace_id)
        self.assertEqual(ctx.impersonate, identity.profile.impersonate)

    def test_configured_impersonate_selects_a_matching_profile(self):
        for impersonate, major in (("chrome131", "131"), ("chrome142", "142")):
            with self.subTest(impersonate=impersonate):
                identity = new_browser_identity(impersonate=impersonate)
                self.assertEqual(identity.profile.impersonate, impersonate)
                self.assertIn(f'v="{major}"', identity.profile.sec_ch_ua)
                self.assertIn(f"Chrome/{major}.", identity.profile.ua)

    def test_unknown_impersonate_is_rejected_instead_of_mixing_profiles(self):
        with self.assertRaisesRegex(ValueError, "没有匹配的浏览器指纹"):
            new_browser_identity(impersonate="chrome999")


class TestArkoseFingerprint(unittest.TestCase):
    def test_direct_replay_uses_profile_and_float_rnd(self):
        profile = BrowserProfile(
            impersonate="chrome131",
            ua="Test-UA",
            sec_ch_ua='"Chromium";v="131"',
            platform='"Windows"',
            accept_language="en-GB,en;q=0.8",
        )
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"token": "tok"}
        with patch.object(arkose.curl_requests, "post", return_value=resp) as post, \
             patch.object(arkose, "_send_arkose_followups") as followups:
            token = arkose._replay_direct(
                arkose.ArkoseConfig(),
                arkose.ReplayConfig(c_blob="blob", x_ark_arid="arid"),
                profile=profile,
            )

        self.assertEqual(token, "tok")
        followups.assert_called_once()
        kwargs = post.call_args.kwargs
        headers = kwargs["headers"]
        self.assertEqual(kwargs["impersonate"], "chrome131")
        self.assertEqual(headers["user-agent"], "Test-UA")
        self.assertEqual(headers["sec-ch-ua"], '"Chromium";v="131"')
        self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
        self.assertEqual(headers["accept-language"], "en-GB,en;q=0.8")
        form = parse_qs(kwargs["data"])
        self.assertEqual(form["userbrowser"], ["Test-UA"])
        self.assertRegex(form["rnd"][0], r"^0\.\d{16}$")

    def test_arkose_followups_are_best_effort(self):
        session = MagicMock()
        with patch.object(arkose, "build_session", return_value=session):
            arkose._send_arkose_followups(arkose.ArkoseConfig(), "token|r=us-east-1", profile=None)

        self.assertGreaterEqual(session.get.call_count, 2)
        self.assertEqual(session.post.call_count, 2)
        called_urls = [call.args[0] for call in session.get.call_args_list]
        self.assertTrue(any("/settings" in url for url in called_urls))
        self.assertTrue(any("/fc/a/" in url for url in called_urls))

    def test_direct_replay_http_error_does_not_echo_response_body(self):
        response = MagicMock(status_code=403, text="SECRET_ARKOSE_BODY")
        with patch.object(arkose.curl_requests, "post", return_value=response):
            with self.assertRaises(RuntimeError) as raised:
                arkose._replay_direct(
                    arkose.ArkoseConfig(),
                    arkose.ReplayConfig(c_blob="blob", x_ark_arid="arid"),
                )

        self.assertIn("HTTP 403", str(raised.exception))
        self.assertNotIn("SECRET_ARKOSE_BODY", str(raised.exception))

    def test_arkose_fallback_log_does_not_echo_exception_details(self):
        with patch.object(arkose, "_replay_passive", side_effect=RuntimeError("SECRET_PROXY_DETAIL")), \
             self.assertLogs("arkose", level="WARNING") as captured, \
             self.assertRaises(RuntimeError):
            arkose.resolve_arkose_token(arkose.ArkoseConfig())

        self.assertNotIn("SECRET_PROXY_DETAIL", "\n".join(captured.output))

    def test_solver_error_does_not_echo_provider_response(self):
        response = MagicMock()
        response.json.return_value = {"errorId": 1, "errorDescription": "SECRET_SOLVER_DETAIL"}
        with patch.object(arkose.curl_requests, "post", return_value=response):
            with self.assertRaises(RuntimeError) as raised:
                arkose._solve_yescaptcha(arkose.ArkoseConfig(), arkose.SolverConfig(api_key="test"))

        self.assertNotIn("SECRET_SOLVER_DETAIL", str(raised.exception))


class TestRequestContracts(unittest.TestCase):
    def test_send_magic_link_body_stays_captured_shape(self):
        runtime = new_browser_runtime(activity_session_id="23cfeb08-9779-42e2-b733-d29c551711cb")
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"sent": True}
        session.post.return_value = resp

        register.send_magic_link(
            session,
            "a@x.com",
            utc_offset=-480,
            anonymous_id="anon",
            device_id="dev",
            sentry_trace_id="a" * 32,
            browser_runtime=runtime,
        )

        body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(
            body,
            {
                "utc_offset": -480,
                "email_address": "a@x.com",
                "login_intent": None,
                "locale": "en-US",
                "return_to": None,
                "source": "claude",
            },
        )
        self.assertNotIn("arkose_session_token", body)
        headers = session.post.call_args.kwargs["headers"]
        self.assertEqual(headers["x-activity-session-id"], runtime.activity_session_id)
        self.assertIn("x-datadog-trace-id", headers)
        self.assertIn("traceparent", headers)

    def test_verify_magic_link_body_stays_captured_shape(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"success": True}
        session.post.return_value = resp

        register.verify_magic_link(session, "nonce", "encoded", "arkose-token")

        body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(
            body,
            {
                "credentials": {
                    "method": "nonce",
                    "nonce": "nonce",
                    "encoded_email_address": "encoded",
                },
                "locale": "en-US",
                "arkose_session_token": "arkose-token",
                "source": "claude",
            },
        )

    def test_auth_http_errors_do_not_echo_response_bodies(self):
        for operation, invoke in (
            ("send", lambda session: register.send_magic_link(session, "a@x.com")),
            ("verify", lambda session: register.verify_magic_link(
                session, "nonce", "encoded", "arkose-token"
            )),
        ):
            response = MagicMock(status_code=403, text=f"SECRET_{operation.upper()}_BODY")
            session = MagicMock()
            session.post.return_value = response
            with self.subTest(operation=operation), self.assertRaises(RuntimeError) as raised:
                invoke(session)
            self.assertIn("HTTP 403", str(raised.exception))
            self.assertNotIn("SECRET_", str(raised.exception))

    def test_onboarding_error_log_does_not_echo_response_body(self):
        response = MagicMock(status_code=400, text="SECRET_ONBOARDING_BODY")
        with self.assertLogs("onboarding", level="ERROR") as captured:
            self.assertFalse(onboarding._ok(response, "first_chat"))

        self.assertNotIn("SECRET_ONBOARDING_BODY", "\n".join(captured.output))


class TestDynamicConfig(unittest.TestCase):
    def test_js_urls_are_normalized_and_deduplicated(self):
        html = """
            <script src="https://assets.claude.ai/app.js"></script>
            <script src="https://assets.claude.ai/app.js"></script>
            <script src="/_next/static/chunks/runtime.js"></script>
        """

        self.assertEqual(
            dynamic_config._collect_js_urls(html),
            [
                "https://assets.claude.ai/app.js",
                "https://claude.ai/_next/static/chunks/runtime.js",
            ],
        )

    def test_nested_dynamic_values_are_found_in_dicts_and_lists(self):
        payload = {
            "outer": [
                {"ignored": "value"},
                {"nested": {"anthropic-client-sha": "a" * 40}},
            ]
        }

        self.assertEqual(
            dynamic_config._find_string_by_key(payload, {"anthropic_client_sha"}),
            "a" * 40,
        )
        self.assertEqual(dynamic_config._find_string_by_key(payload, {"missing"}), "")

    def test_bootstrap_uses_response_text_when_json_is_invalid(self):
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError("invalid json")
        response.text = (
            '{"clientSha":"' + "b" * 40 + '","clientVersion":"3.4.5"}'
        )
        session = MagicMock()
        session.get.return_value = response

        sha, version = dynamic_config._fetch_bootstrap_config(session, timeout=9)

        self.assertEqual(sha, "b" * 40)
        self.assertEqual(version, "3.4.5")
        self.assertEqual(session.get.call_args.kwargs["timeout"], 9)

    def test_bootstrap_non_success_status_uses_empty_fallback(self):
        session = MagicMock()
        session.get.return_value = MagicMock(status_code=503)

        self.assertEqual(dynamic_config._fetch_bootstrap_config(session), ("", ""))

    def test_js_config_extracts_all_values_from_one_chunk(self):
        sha = "c" * 40
        sentry_key = "d" * 32
        login = MagicMock(
            status_code=200,
            text='<script src="/_next/static/chunks/app.js"></script>',
        )
        chunk = MagicMock(
            status_code=200,
            text=(
                f'anthropic-client-sha","{sha}" '
                f'https://{sentry_key}@o12345.ingest.sentry.io/ '
                'anthropic-client-version","4.5.6"'
            ),
        )
        session = MagicMock()
        session.get.side_effect = [login, chunk]

        values = dynamic_config._fetch_js_config(session, timeout=11)

        self.assertEqual(values, (sha, sentry_key, "12345", "4.5.6"))
        self.assertEqual(session.get.call_args_list[1].kwargs["timeout"], 11)

    def test_owned_dynamic_sessions_are_always_closed(self):
        first = MagicMock()
        second = MagicMock()
        third = MagicMock()
        bad_legal = MagicMock(status_code=503)
        third.get.return_value = bad_legal

        with patch.object(dynamic_config, "build_session", side_effect=[first, second, third]), \
             patch.object(
                 dynamic_config,
                 "_fetch_js_config",
                 return_value=("a" * 40, "key", "org", "1.2.3"),
             ):
            self.assertEqual(dynamic_config.fetch_client_sha(), "a" * 40)
            self.assertEqual(dynamic_config.fetch_sentry_config(), ("key", "org"))
            self.assertEqual(dynamic_config.fetch_legal_docs(), dynamic_config.DEFAULT_LEGAL_DOCS)

        first.close.assert_called_once()
        second.close.assert_called_once()
        third.close.assert_called_once()

    def test_legal_docs_reject_invalid_payloads_and_request_errors(self):
        invalid = MagicMock(status_code=200)
        invalid.json.return_value = {"unexpected": "shape"}
        session = MagicMock()
        session.get.side_effect = [invalid, RuntimeError("request failed")]

        self.assertEqual(dynamic_config.fetch_legal_docs(session), dynamic_config.DEFAULT_LEGAL_DOCS)
        self.assertEqual(dynamic_config.fetch_legal_docs(session), dynamic_config.DEFAULT_LEGAL_DOCS)

    def test_owned_combined_dynamic_session_closes_after_fallback_merge(self):
        session = MagicMock()
        with patch.object(dynamic_config, "build_session", return_value=session), \
             patch.object(
                 dynamic_config,
                 "_fetch_js_config",
                 return_value=(
                     dynamic_config.DEFAULT_CLIENT_SHA,
                     "key",
                     "org",
                     dynamic_config.DEFAULT_CLIENT_VERSION,
                 ),
             ), \
             patch.object(dynamic_config, "_fetch_bootstrap_config", return_value=("e" * 40, "7.8.9")), \
             patch.object(dynamic_config, "fetch_legal_docs", return_value=[]):
            values = dynamic_config.fetch_dynamic_config()

        self.assertEqual(values, ("e" * 40, [], "key", "org", "7.8.9"))
        session.close.assert_called_once()

    def test_bootstrap_supplies_client_sha_when_login_is_blocked(self):
        login = MagicMock(status_code=403, text="")
        bootstrap = MagicMock(status_code=200)
        bootstrap.json.return_value = {
            "client_sha": "f" * 40,
            "anthropic_client_version": "2.1.3",
        }
        legal = MagicMock(status_code=200)
        legal.json.return_value = {
            "aup": "v3:aup:new",
            "consumer-terms": "v3:consumer:new",
            "privacy": "v3:privacy:new",
        }
        session = MagicMock()
        session.get.side_effect = [login, bootstrap, legal]

        sha, docs, _sentry_key, _sentry_org, version = dynamic_config.fetch_dynamic_config(session)

        self.assertEqual(sha, "f" * 40)
        self.assertEqual(version, "2.1.3")
        self.assertEqual(docs[0]["document_id"], "v3:aup:new")

    def test_dynamic_config_log_does_not_echo_exception_details(self):
        session = MagicMock()
        session.get.side_effect = RuntimeError("SECRET_DYNAMIC_PROXY")
        with self.assertLogs("dynamic_config", level="WARNING") as captured:
            self.assertEqual(dynamic_config._fetch_bootstrap_config(session), ("", ""))

        self.assertNotIn("SECRET_DYNAMIC_PROXY", "\n".join(captured.output))


class TestDynamicConfigCache(unittest.TestCase):
    def test_dynamic_config_is_reused_within_short_cache_window(self):
        values = ("a" * 40, [], "key", "org", "1.0.0")
        session = MagicMock()
        with patch.object(orch, "_dynamic_config_cache", None, create=True), \
             patch.object(orch, "fetch_dynamic_config", return_value=values) as fetch, \
             patch.object(orch.time, "monotonic", return_value=100.0):
            self.assertEqual(orch._fetch_dynamic_config_with_cache(session), values)
            self.assertEqual(orch._fetch_dynamic_config_with_cache(session), values)

        fetch.assert_called_once_with(session)

    def test_dynamic_config_is_refreshed_after_cache_expiry(self):
        values = ("a" * 40, [], "key", "org", "1.0.0")
        session = MagicMock()
        with patch.object(orch, "_dynamic_config_cache", None, create=True), \
             patch.object(orch, "fetch_dynamic_config", return_value=values) as fetch, \
             patch.object(orch.time, "monotonic", side_effect=[100.0, 100.0, 161.0, 161.0]):
            orch._fetch_dynamic_config_with_cache(session)
            orch._fetch_dynamic_config_with_cache(session)

        self.assertEqual(fetch.call_count, 2)


class TestKycFingerprint(unittest.TestCase):
    def test_kyc_classification_keeps_dead_distinct_from_probe_errors(self):
        from claude_register.compliance import kyc

        cases = (
            ([MagicMock(status_code=401)], (False, "dead")),
            ([MagicMock(status_code=503)], (False, "error")),
            ([MagicMock(status_code=200, **{"json.return_value": []})], (True, "error")),
            (
                [MagicMock(status_code=200, **{"json.return_value": [{}]})],
                (True, "error"),
            ),
            (
                [
                    MagicMock(status_code=200, **{"json.return_value": [{"uuid": "ou"}]}),
                    MagicMock(status_code=503),
                ],
                (True, "error"),
            ),
        )
        for responses, expected in cases:
            session = MagicMock()
            session.get.side_effect = responses
            with self.subTest(expected=expected):
                self.assertEqual(
                    kyc.check_kyc_status(
                        "sk",
                        session=session,
                        anonymous_id="anon",
                        device_id="device",
                        sentry_trace_id="f" * 32,
                    ),
                    expected,
                )

        failed_session = MagicMock()
        failed_session.get.side_effect = RuntimeError("probe failed")
        self.assertEqual(
            kyc.check_kyc_status(
                "sk",
                session=failed_session,
                anonymous_id="anon",
                device_id="device",
                sentry_trace_id="f" * 32,
            ),
            (False, "error"),
        )

    def test_kyc_can_reuse_registration_identity(self):
        session = MagicMock()
        org_resp = MagicMock(status_code=200)
        org_resp.json.return_value = [{"uuid": "ou"}]
        kyc_resp = MagicMock(status_code=200)
        kyc_resp.json.return_value = {"status": "not_required"}
        session.get.side_effect = [org_resp, kyc_resp]

        with patch("claude_register.compliance.kyc.build_session", return_value=session):
            from claude_register.compliance import kyc

            alive, status = kyc.check_kyc_status(
                "sk",
                anonymous_id="anon",
                device_id="dev",
                sentry_trace_id="b" * 32,
            )

        self.assertTrue(alive)
        self.assertEqual(status, "not_required")
        headers = session.get.call_args_list[0].kwargs["headers"]
        self.assertEqual(headers["anthropic-anonymous-id"], "anon")
        self.assertEqual(headers["anthropic-device-id"], "dev")
        session.close.assert_called_once()

    def test_kyc_reuses_existing_authenticated_session_and_identity(self):
        identity = new_browser_identity()
        session = MagicMock()
        org_resp = MagicMock(status_code=200)
        org_resp.json.return_value = [{"uuid": "ou"}]
        kyc_resp = MagicMock(status_code=200)
        kyc_resp.json.return_value = {"status": "not_required"}
        session.get.side_effect = [org_resp, kyc_resp]

        from claude_register.compliance import kyc

        with patch.object(kyc, "build_session") as build_session:
            alive, status = kyc.check_kyc_status(
                "sk",
                session=session,
                identity=identity,
            )

        self.assertTrue(alive)
        self.assertEqual(status, "not_required")
        build_session.assert_not_called()
        session.close.assert_not_called()
        session.cookies.set.assert_not_called()
        headers = session.get.call_args_list[0].kwargs["headers"]
        self.assertEqual(headers["anthropic-anonymous-id"], identity.anonymous_id)
        self.assertEqual(headers["anthropic-device-id"], identity.device_id)
        self.assertEqual(headers["x-activity-session-id"], identity.runtime.activity_session_id)


class TestRegisterOneTransactions(unittest.TestCase):
    """verify 成功=账号已创建：后续失败应标 partial 且不整体重试；verify 前失败按可重试性重试。"""

    def setUp(self):
        # 跳过全局邮件节流器的真实 sleep
        self._acquire_patch = patch.object(orch._mail_limiter, "acquire")
        self._acquire_patch.start()

    def tearDown(self):
        self._acquire_patch.stop()

    def _cfg(self, retry_max=0, auto_send=False):
        return orch.OrchestratorConfig(retry_max=retry_max, auto_send=auto_send)

    def test_work_function_candidates_are_server_safe(self):
        self.assertEqual(orch._WORK_FUNCTIONS, ["Other"])
        for value in ("Other", "Software Engineering", "", None):
            self.assertEqual(orch._safe_work_function(value), "Other")

    @patch("claude_register.orchestration.service.check_kyc_status", return_value=(True, "not_required"))
    @patch("claude_register.orchestration.service.run_onboarding")
    @patch("claude_register.orchestration.service.verify_magic_link")
    @patch("claude_register.orchestration.service.fetch_magic_link")
    @patch("claude_register.orchestration.service.resolve_arkose_token", return_value="tok")
    @patch("claude_register.orchestration.service.send_magic_link")
    @patch("claude_register.orchestration.service.login_methods", return_value=["magic_link"])
    @patch("claude_register.orchestration.service.build_session")
    def test_session_flow_skips_onboarding_and_extracts_session(
        self, bs, _lm, send_ml, _ark, fml, vml, run_ob, _kyc
    ):
        session = MagicMock()
        session.cookies.get.return_value = "sk"
        bs.return_value = session
        fml.return_value = {"nonce": "n", "encoded_email_address": "e"}
        vml.return_value = {"created": False, "account": {"uuid": "au", "memberships": []}}
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))

        orch.register_one(t, orch.OrchestratorConfig(flow_mode="session", retry_max=2, auto_send=True))

        self.assertEqual(t.status, "success")
        self.assertEqual(t.session_key, "sk")
        self.assertEqual(t.kyc_status, "not_required")
        run_ob.assert_not_called()
        self.assertNotIn("login_intent", send_ml.call_args.kwargs)

    @patch("claude_register.orchestration.service.run_onboarding", side_effect=RuntimeError("onboarding HTTP 500"))
    @patch("claude_register.orchestration.service.verify_magic_link")
    @patch("claude_register.orchestration.service.fetch_magic_link")
    @patch("claude_register.orchestration.service.resolve_arkose_token", return_value="tok")
    @patch("claude_register.orchestration.service.build_session")
    def test_partial_on_onboarding_fail(self, _bs, _ark, fml, vml, _ob):
        fml.return_value = {"nonce": "n", "encoded_email_address": "e"}
        vml.return_value = {"account": {"uuid": "au",
                                        "memberships": [{"organization": {"uuid": "ou"}}]}}
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        orch.register_one(t, self._cfg(retry_max=2))
        self.assertEqual(t.status, "partial")
        self.assertEqual(t.account_uuid, "au")
        self.assertEqual(t.org_uuid, "ou")
        self.assertEqual(t.attempts, 1)            # verify 后不整体重试

    @patch("claude_register.orchestration.service.run_onboarding", return_value={"start_onboarding": True, "first_chat": None})
    @patch("claude_register.orchestration.service.verify_magic_link")
    @patch("claude_register.orchestration.service.fetch_magic_link")
    @patch("claude_register.orchestration.service.resolve_arkose_token", return_value="tok")
    @patch("claude_register.orchestration.service.build_session")
    def test_partial_when_onboarding_summary_contains_failed_step(self, _bs, _ark, fml, vml, _ob):
        fml.return_value = {"nonce": "n", "encoded_email_address": "e"}
        vml.return_value = {"account": {"uuid": "au",
                                        "memberships": [{"organization": {"uuid": "ou"}}]}}
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        orch.register_one(t, self._cfg(retry_max=2))
        self.assertEqual(t.status, "partial")
        self.assertIn("first_chat", t.error)
        self.assertEqual(t.attempts, 1)

    def test_cancel_after_verify_does_not_start_post_verify_stages(self):
        cancel = threading.Event()
        session = MagicMock()
        session.cookies.get.return_value = "sk"
        verify_response = {
            "account": {
                "uuid": "au",
                "memberships": [{"organization": {"uuid": "ou"}}],
            }
        }

        def verify_then_cancel(*_args, **_kwargs):
            cancel.set()
            return verify_response

        task = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        with patch.object(orch, "build_session", return_value=session), \
             patch.object(orch, "fetch_dynamic_config", return_value=("sha", [], "sentry", "org", "1.0")), \
             patch.object(orch, "fetch_magic_link", return_value={"nonce": "n", "encoded_email_address": "e"}), \
             patch.object(orch, "resolve_arkose_token", return_value="tok"), \
             patch.object(orch, "verify_magic_link", side_effect=verify_then_cancel), \
             patch.object(orch, "run_onboarding") as run_onboarding, \
             patch.object(orch, "warm_claude_login"), \
             patch.object(orch, "warm_claude_bootstrap") as warm_bootstrap:
            orch.register_one(task, self._cfg(retry_max=2), cancel=cancel)

        self.assertEqual(task.status, "partial")
        self.assertEqual(task.account_uuid, "au")
        self.assertEqual(task.error_class, "cancelled")
        self.assertEqual(task.attempts, 1)
        run_onboarding.assert_not_called()
        self.assertEqual(warm_bootstrap.call_count, 1)

    def test_cancel_after_onboarding_does_not_start_kyc(self):
        cancel = threading.Event()
        session = MagicMock()
        session.cookies.get.return_value = "sk"
        verify_response = {
            "account": {
                "uuid": "au",
                "memberships": [{"organization": {"uuid": "ou"}}],
            }
        }

        def onboarding_then_cancel(_ctx):
            cancel.set()
            return {"start_onboarding": True, "first_chat": "conv"}

        task = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        with patch.object(orch, "build_session", return_value=session), \
             patch.object(orch, "fetch_dynamic_config", return_value=("sha", [], "sentry", "org", "1.0")), \
             patch.object(orch, "fetch_magic_link", return_value={"nonce": "n", "encoded_email_address": "e"}), \
             patch.object(orch, "resolve_arkose_token", return_value="tok"), \
             patch.object(orch, "verify_magic_link", return_value=verify_response), \
             patch.object(orch, "run_onboarding", side_effect=onboarding_then_cancel), \
             patch.object(orch, "check_kyc_status") as check_kyc, \
             patch.object(orch, "warm_claude_login"), \
             patch.object(orch, "warm_claude_bootstrap"):
            orch.register_one(task, self._cfg(retry_max=2), cancel=cancel)

        self.assertEqual(task.status, "partial")
        self.assertEqual(task.error_class, "cancelled")
        self.assertEqual(task.attempts, 1)
        check_kyc.assert_not_called()

    @patch("claude_register.orchestration.service.check_kyc_status", return_value=(False, "error"))
    @patch("claude_register.orchestration.service.run_onboarding", return_value={"start_onboarding": True, "first_chat": "conv"})
    @patch("claude_register.orchestration.service.verify_magic_link")
    @patch("claude_register.orchestration.service.fetch_magic_link")
    @patch("claude_register.orchestration.service.resolve_arkose_token", return_value="tok")
    @patch("claude_register.orchestration.service.build_session")
    def test_kyc_request_error_is_not_marked_dead(self, bs, _ark, fml, vml, _ob, _kyc):
        session = MagicMock()
        session.cookies.get.return_value = "sk"
        bs.return_value = session
        fml.return_value = {"nonce": "n", "encoded_email_address": "e"}
        vml.return_value = {"account": {"uuid": "au",
                                        "memberships": [{"organization": {"uuid": "ou"}}]}}
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        orch.register_one(t, self._cfg(retry_max=0))
        self.assertEqual(t.status, "success")
        self.assertEqual(t.kyc_status, "error")

    @patch("claude_register.orchestration.service.verify_magic_link", side_effect=RuntimeError("verify HTTP 403"))
    @patch("claude_register.orchestration.service.fetch_magic_link")
    @patch("claude_register.orchestration.service.resolve_arkose_token", return_value="tok")
    @patch("claude_register.orchestration.service.build_session")
    def test_failed_on_verify_4xx_no_retry(self, _bs, _ark, fml, vml):
        fml.return_value = {"nonce": "n", "encoded_email_address": "e"}
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        orch.register_one(t, self._cfg(retry_max=2))
        self.assertEqual(t.status, "failed")
        self.assertEqual(t.attempts, 1)            # 403 不可重试

    @patch("claude_register.orchestration.service.fetch_magic_link", side_effect=mail_client.MailFetcherFatalError("邮箱登录失败"))
    @patch("claude_register.orchestration.service.build_session")
    def test_mail_fatal_error_does_not_retry_account(self, _bs, _fml):
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        orch.register_one(t, self._cfg(retry_max=2))
        self.assertEqual(t.status, "failed")
        self.assertEqual(t.attempts, 1)

    @patch("claude_register.orchestration.service.verify_magic_link", side_effect=RuntimeError("HTTP 503"))
    @patch("claude_register.orchestration.service.fetch_magic_link")
    @patch("claude_register.orchestration.service.resolve_arkose_token", return_value="tok")
    @patch("claude_register.orchestration.service.build_session")
    def test_retry_on_5xx_before_verify(self, _bs, _ark, fml, vml):
        fml.return_value = {"nonce": "n", "encoded_email_address": "e"}
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        with patch("claude_register.orchestration.service._wait_cancelable", return_value=False) as wait:
            orch.register_one(t, self._cfg(retry_max=2))
        self.assertEqual(t.status, "failed")
        self.assertEqual(t.attempts, 3)            # retry_max+1 次
        self.assertEqual(wait.call_count, 2)       # 最后一次失败后不再空等

    @patch("claude_register.orchestration.service.fetch_dynamic_config", return_value=("sha", [], "sentry", "org"))
    @patch("claude_register.orchestration.service.fetch_magic_link", side_effect=RuntimeError("HTTP 503 SECRET_TASK_BODY"))
    @patch("claude_register.orchestration.service.build_session")
    def test_task_error_uses_safe_classification(self, _bs, _fml, _dyn):
        task = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        with patch("claude_register.orchestration.service.warm_claude_login"):
            orch.register_one(task, self._cfg(retry_max=0))

        self.assertNotIn("SECRET_TASK_BODY", task.error)
        self.assertEqual(task.error_class, "http_503")
        self.assertTrue(task.retryable)

    @patch("claude_register.orchestration.service.fetch_dynamic_config", return_value=("sha", [], "sentry", "org"))
    @patch("claude_register.orchestration.service.fetch_magic_link", side_effect=RuntimeError("HTTP 503"))
    @patch("claude_register.orchestration.service.build_session")
    def test_sessions_are_closed_after_each_failed_attempt(self, bs, _fml, _dyn):
        sessions = [MagicMock(), MagicMock(), MagicMock()]
        bs.side_effect = sessions
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        with patch("claude_register.orchestration.service._wait_cancelable", return_value=False), \
             patch("claude_register.orchestration.service.warm_claude_login"):
            orch.register_one(t, self._cfg(retry_max=1))
        self.assertEqual(bs.call_count, 3)  # dynamic config + 2 attempts
        for session in sessions:
            session.close.assert_called_once()

    @patch("claude_register.orchestration.service.check_kyc_status", return_value=(True, "not_required"))
    @patch("claude_register.orchestration.service.run_onboarding", return_value={"start_onboarding": True, "first_chat": "conv"})
    @patch("claude_register.orchestration.service.verify_magic_link")
    @patch("claude_register.orchestration.service.fetch_magic_link")
    @patch("claude_register.orchestration.service.resolve_arkose_token", return_value="tok")
    @patch("claude_register.orchestration.service.fetch_dynamic_config", return_value=("sha", [], "sentry", "org"))
    @patch("claude_register.orchestration.service.build_session")
    def test_register_one_warms_claude_login_session(self, bs, _dyn, _ark, fml, vml, _ob, _kyc):
        session = MagicMock()
        session.cookies.get.return_value = "sk"
        bs.return_value = session
        fml.return_value = {"nonce": "n", "encoded_email_address": "e"}
        vml.return_value = {"account": {"uuid": "au",
                                        "memberships": [{"organization": {"uuid": "ou"}}]}}
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        with patch("claude_register.orchestration.service.warm_claude_login") as warm:
            orch.register_one(t, self._cfg(retry_max=0))
        warm.assert_called_once_with(session)

    @patch("claude_register.orchestration.service.check_kyc_status", return_value=(True, "not_required"))
    @patch("claude_register.orchestration.service.run_onboarding", return_value={"start_onboarding": True, "first_chat": "conv"})
    @patch("claude_register.orchestration.service.verify_magic_link")
    @patch("claude_register.orchestration.service.fetch_magic_link")
    @patch("claude_register.orchestration.service.resolve_arkose_token", return_value="tok")
    @patch("claude_register.orchestration.service.fetch_dynamic_config", return_value=("sha", [], "sentry", "org"))
    @patch("claude_register.orchestration.service.build_session")
    def test_register_one_passes_identity_to_kyc(self, bs, _dyn, _ark, fml, vml, _ob, kyc_check):
        session = MagicMock()
        session.cookies.get.return_value = "sk"
        bs.return_value = session
        fml.return_value = {"nonce": "n", "encoded_email_address": "e"}
        vml.return_value = {"account": {"uuid": "au",
                                        "memberships": [{"organization": {"uuid": "ou"}}]}}
        t = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))

        orch.register_one(t, self._cfg(retry_max=0))

        self.assertIn("identity", kyc_check.call_args.kwargs)
        self.assertIs(kyc_check.call_args.kwargs["session"], session)

    def test_register_one_reuses_one_identity_through_every_http_stage(self):
        identity = new_browser_identity()
        session = MagicMock()
        session.cookies.get.return_value = "sk"
        task = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))
        cancel = threading.Event()
        verify_response = {
            "account": {"uuid": "au", "memberships": [{"organization": {"uuid": "ou"}}]},
        }

        with patch.object(orch, "new_browser_identity", return_value=identity), \
             patch.object(orch, "build_session", return_value=session) as build_session, \
             patch.object(orch, "fetch_dynamic_config", return_value=("sha", [], "sentry", "org", "1.0")), \
             patch.object(orch, "fetch_magic_link", return_value={"nonce": "n", "encoded_email_address": "e"}), \
             patch.object(orch, "resolve_arkose_token", return_value="tok") as resolve_arkose, \
             patch.object(orch, "verify_magic_link", return_value=verify_response) as verify, \
             patch.object(orch, "run_onboarding", return_value={"start_onboarding": True, "first_chat": "conv"}) as run_onboarding, \
             patch.object(orch, "check_kyc_status", return_value=(True, "not_required")) as check_kyc, \
             patch.object(orch, "warm_claude_login"), \
             patch.object(orch, "warm_claude_bootstrap"):
            orch.register_one(task, self._cfg(retry_max=0), cancel=cancel)

        self.assertEqual(task.status, "success")
        self.assertTrue(build_session.call_args_list)
        for call in build_session.call_args_list:
            self.assertIs(call.kwargs["identity"], identity)
        self.assertIs(resolve_arkose.call_args.kwargs["profile"], identity.profile)
        self.assertIs(verify.call_args.kwargs["identity"], identity)
        self.assertIs(run_onboarding.call_args.args[0].identity, identity)
        self.assertIs(run_onboarding.call_args.args[0].cancel_event, cancel)
        self.assertIs(check_kyc.call_args.kwargs["identity"], identity)
        self.assertIs(check_kyc.call_args.kwargs["session"], session)


class TestTaskTimingTelemetry(unittest.TestCase):
    def test_stage_switch_records_queue_wait_and_current_stage_elapsed(self):
        task = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))

        task.mark_queued(now=10.0)
        task.mark_running(now=12.0)
        task.set_stage("mail", now=13.0)
        task.set_stage("verify", now=15.0)

        with patch("claude_register.orchestration.service.time.time", return_value=16.0):
            data = task.to_dict()

        self.assertEqual(data["queue_wait_ms"], 2000)
        self.assertEqual(data["stage_durations_ms"], {"mail": 2000})
        self.assertEqual(data["stage_elapsed_ms"], 1000)

    def test_retry_accumulates_stage_duration_without_overwriting_previous_attempt(self):
        task = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))

        task.mark_running(now=0.0)
        task.set_stage("mail", now=1.0)
        task.complete_current_stage(now=3.0)
        task.set_stage("mail", now=5.0)
        task.complete_current_stage(now=7.0)

        self.assertEqual(task.to_dict()["stage_durations_ms"], {"mail": 4000})

    def test_substage_duration_accumulates_without_changing_stage_duration(self):
        task = orch.AccountTask(account=orch.Account("a@x.com", "p", "a"))

        task.record_substage_duration("send.warm_login", 120)
        task.record_substage_duration("send.warm_login", 80)

        data = task.to_public_dict()
        self.assertEqual(data["stage_durations_ms"], {})
        self.assertEqual(data["substage_durations_ms"], {"send.warm_login": 200})

    def test_unstarted_finished_task_reports_zero_elapsed(self):
        task = orch.AccountTask(
            account=orch.Account("a@x.com", "p", "a"),
            status="failed",
            finished_at=100.0,
        )

        data = task.to_public_dict()

        self.assertEqual(data["elapsed"], 0)
        self.assertEqual(data["queue_wait_ms"], 0)

    def test_task_summary_log_does_not_include_account_or_raw_error_data(self):
        task = orch.AccountTask(
            account=orch.Account("SECRET_EMAIL@example.invalid", "SECRET_PASSWORD", "a"),
            status="failed",
            stage="mail",
            started_at=1.0,
            finished_at=3.0,
            session_key="SECRET_SESSION",
            error="SECRET_RAW_EXCEPTION",
            attempts=2,
            error_class="http_503",
            retryable=True,
        )

        with self.assertLogs("orchestrator", level="INFO") as captured:
            orch._log_task_summary(task, orch.OrchestratorConfig(flow_mode="register"))

        output = "\n".join(captured.output)
        for secret in ("SECRET_EMAIL", "SECRET_PASSWORD", "SECRET_SESSION", "SECRET_RAW_EXCEPTION"):
            self.assertNotIn(secret, output)
        self.assertIn('"event": "account_task_summary"', output)
        self.assertIn('"outcome": "failed"', output)
        self.assertIn('"error_class": "http_503"', output)

    def test_task_summary_keeps_success_partial_and_failed_outcomes_separate(self):
        outcomes = []
        for status in ("success", "partial", "failed"):
            task = orch.AccountTask(
                account=orch.Account("a@x.com", "p", "a"),
                status=status,
                stage="kyc",
                started_at=1.0,
                finished_at=2.0,
            )
            outcomes.append(orch._task_summary_event(task, orch.OrchestratorConfig())["outcome"])

        self.assertEqual(outcomes, ["success", "partial", "failed"])


class TestResultWriting(unittest.TestCase):
    def test_task_snapshot_includes_microsoft_mail_token_fields(self):
        t = orch.AccountTask(account=orch.Account("a@outlook.com", "p", "a", "cid", "rt"))
        data = t.to_dict()
        self.assertEqual(data["mail_client_id"], "cid")
        self.assertEqual(data["mail_refresh_token"], "rt")

    def test_task_snapshot_includes_delivery_prefix(self):
        t = orch.AccountTask(
            account=orch.Account(
                "a@outlook.com", "x", "a", "cid", "rt",
                deliver_prefix="a@outlook.com----real-pass----cid----rt",
            )
        )
        data = t.to_dict()
        self.assertEqual(data["deliver_prefix"], "a@outlook.com----real-pass----cid----rt")

    def test_unknown_kyc_is_written_to_unknown_file(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = orch.OrchestratorConfig(
                output_file=str(base / "results.txt"),
                failed_file=str(base / "failed.txt"),
                kyc_pass_file=str(base / "kyc_pass.txt"),
                kyc_required_file=str(base / "kyc_required.txt"),
                kyc_unknown_file=str(base / "kyc_unknown.txt"),
            )
            t = orch.AccountTask(
                account=orch.Account("a@x.com", "p", "a"),
                status="success",
                session_key="sk",
                routing_hint="rh",
                kyc_status="error",
            )
            orch._write_result(t, cfg)
            self.assertEqual((base / "results.txt").read_text(encoding="utf-8"), "a@x.com----p----sk\n")
            self.assertEqual((base / "kyc_unknown.txt").read_text(encoding="utf-8"), "a@x.com----p----sk\n")
            self.assertFalse((base / "kyc_pass.txt").exists())
            self.assertFalse((base / "kyc_required.txt").exists())

    def test_microsoft_token_account_output_preserves_login_fields_before_session(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = orch.OrchestratorConfig(
                output_file=str(base / "results.txt"),
                failed_file=str(base / "failed.txt"),
                kyc_pass_file=str(base / "kyc_pass.txt"),
                kyc_required_file=str(base / "kyc_required.txt"),
                kyc_unknown_file=str(base / "kyc_unknown.txt"),
            )
            t = orch.AccountTask(
                account=orch.Account("a@outlook.com", "p", "a", "cid", "rt"),
                status="success",
                session_key="sk",
                routing_hint="rh",
                kyc_status="not_required",
            )
            orch._write_result(t, cfg)
            expected = "a@outlook.com----p----cid----rt----sk\n"
            self.assertEqual((base / "results.txt").read_text(encoding="utf-8"), expected)
            self.assertEqual((base / "kyc_pass.txt").read_text(encoding="utf-8"), expected)

    def test_result_writing_prefers_original_delivery_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = orch.OrchestratorConfig(
                output_file=str(base / "results.txt"),
                failed_file=str(base / "failed.txt"),
                kyc_pass_file=str(base / "kyc_pass.txt"),
                kyc_required_file=str(base / "kyc_required.txt"),
                kyc_unknown_file=str(base / "kyc_unknown.txt"),
            )
            t = orch.AccountTask(
                account=orch.Account(
                    "a@outlook.com", "x", "a", "cid", "rt",
                    deliver_prefix="a@outlook.com----real-pass----cid----rt",
                ),
                status="success",
                session_key="sk",
                kyc_status="not_required",
            )
            orch._write_result(t, cfg)
            expected = "a@outlook.com----real-pass----cid----rt----sk\n"
            self.assertEqual((base / "results.txt").read_text(encoding="utf-8"), expected)
            self.assertEqual((base / "kyc_pass.txt").read_text(encoding="utf-8"), expected)

    def test_dead_kyc_is_written_to_dead_file(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = orch.OrchestratorConfig(
                output_file=str(base / "results.txt"),
                failed_file=str(base / "failed.txt"),
                kyc_pass_file=str(base / "kyc_pass.txt"),
                kyc_required_file=str(base / "kyc_required.txt"),
                kyc_unknown_file=str(base / "kyc_unknown.txt"),
                kyc_dead_file=str(base / "kyc_dead.txt"),
            )
            t = orch.AccountTask(
                account=orch.Account("a@x.com", "p", "a"),
                status="success",
                session_key="sk",
                kyc_status="dead",
            )
            orch._write_result(t, cfg)
            self.assertEqual((base / "results.txt").read_text(encoding="utf-8"), "a@x.com----p----sk\n")
            self.assertEqual((base / "kyc_dead.txt").read_text(encoding="utf-8"), "a@x.com----p----sk\n")
            self.assertFalse((base / "kyc_unknown.txt").exists())

    def test_partial_with_session_is_written_to_partial_and_kyc_bucket_not_results(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = orch.OrchestratorConfig(
                output_file=str(base / "results.txt"),
                failed_file=str(base / "failed.txt"),
                partial_file=str(base / "partial.txt"),
                kyc_pass_file=str(base / "kyc_pass.txt"),
                kyc_required_file=str(base / "kyc_required.txt"),
                kyc_unknown_file=str(base / "kyc_unknown.txt"),
            )
            t = orch.AccountTask(
                account=orch.Account("a@x.com", "p", "a"),
                status="partial",
                session_key="sk",
                kyc_status="error",
            )
            orch._write_result(t, cfg)
            self.assertEqual((base / "partial.txt").read_text(encoding="utf-8"), "a@x.com----p----sk\n")
            self.assertEqual((base / "kyc_unknown.txt").read_text(encoding="utf-8"), "a@x.com----p----sk\n")
            self.assertFalse((base / "results.txt").exists())


class TestOnboardingSteps(unittest.TestCase):
    def test_all_onboarding_requests_use_the_configured_timeout(self):
        response = MagicMock(status_code=200)
        session = MagicMock()
        session.patch.return_value = response
        session.put.return_value = response
        session.post.return_value = response
        ctx = onboarding.OnboardingContext(
            session=session,
            org_uuid="organization",
            display_name="Fixture",
            request_timeout=57.0,
        )

        with patch.object(onboarding, "_step_delay"):
            onboarding.run_onboarding(ctx)

        calls = (
            session.patch.call_args_list
            + session.put.call_args_list
            + session.post.call_args_list
        )
        self.assertEqual(len(calls), 11)
        self.assertTrue(all(call.kwargs["timeout"] == 57.0 for call in calls))

    def test_cancelled_step_wait_stops_before_the_next_request(self):
        from claude_register.orchestration.errors import FlowCancelled

        cancel = threading.Event()
        cancel.set()
        ctx = onboarding.OnboardingContext(
            session=MagicMock(),
            org_uuid="organization",
            display_name="Fixture",
            cancel_event=cancel,
        )

        with patch.object(onboarding, "step_start_onboarding", return_value=True), \
             patch.object(onboarding, "step_privacy_consents") as privacy:
            with self.assertRaises(FlowCancelled):
                onboarding.run_onboarding(ctx)

        privacy.assert_not_called()

    def test_onboarding_start_log_does_not_echo_identity(self):
        ctx = onboarding.OnboardingContext(
            session=MagicMock(),
            org_uuid="SECRET_ORG",
            display_name="SECRET_NAME",
        )
        with patch("claude_register.onboarding.service.step_start_onboarding", return_value=False):
            with self.assertLogs("onboarding", level="INFO") as captured:
                onboarding.run_onboarding(ctx)

        output = "\n".join(captured.output)
        self.assertNotIn("SECRET_ORG", output)
        self.assertNotIn("SECRET_NAME", output)

    def test_onboarding_completed_requires_final_and_required_steps(self):
        self.assertFalse(onboarding.onboarding_completed({"first_chat": "conversation"}))
        self.assertFalse(onboarding.onboarding_completed({
            "start_onboarding": True,
            "first_chat": None,
            "finish_onboarding": True,
        }))
        self.assertTrue(onboarding.onboarding_completed({
            "start_onboarding": True,
            "privacy_consents": False,
            "first_chat": "conversation",
            "finish_onboarding": True,
        }))

    def test_privacy_and_grove_are_optional(self):
        result = {
            "start_onboarding": True,
            "privacy_consents": False,
            "grove": False,
            "first_chat": "conv",
        }
        self.assertEqual(onboarding_failed_steps(result), [])

    def test_required_onboarding_step_still_fails(self):
        result = {
            "start_onboarding": True,
            "privacy_consents": False,
            "first_chat": None,
        }
        self.assertEqual(onboarding_failed_steps(result), ["first_chat"])


class TestWebConfig(unittest.TestCase):
    def test_health_check_is_minimal_and_does_not_expose_runtime_state(self):
        self.assertEqual(webui.healthz(), {"status": "ok"})

    def test_security_headers_apply_a_strict_browser_policy(self):
        headers = webui.SENSITIVE_RESPONSE_HEADERS

        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Permissions-Policy"], "camera=(), microphone=(), geolocation=()")
        self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")

    def test_config_does_not_return_secret_values(self):
        cfg = {
            "proxy_template": "p",
            "arkose": {"solver": {"api_key": "secret-key"}, "passive_token": "secret-token"},
        }
        with patch("claude_register.presentation.web.load_config", return_value=cfg):
            result = webui.get_config()

        self.assertNotIn("arkose_key", result)
        self.assertNotIn("passive_token", result)
        self.assertNotIn("arkose_key_configured", result)
        self.assertNotIn("passive_token_configured", result)

    def test_run_ids_are_unique(self):
        self.assertNotEqual(webui._new_run_id(), webui._new_run_id())

    def test_progress_stream_is_localhost_scoped_by_default(self):
        self.assertTrue(webui._is_loopback_host("127.0.0.1"))
        self.assertTrue(webui._is_loopback_host("::1"))
        self.assertFalse(webui._is_loopback_host("192.168.1.20"))

    def test_webui_is_local_only_by_default(self):
        for path in ("/", "/api/config", "/api/start", "/api/stop", "/api/results.txt"):
            with self.subTest(path=path):
                self.assertTrue(webui._request_allowed(
                    path, "127.0.0.1", {}, request_host="localhost"
                ))
                self.assertFalse(webui._request_allowed(
                    path, "192.168.1.20", {}, request_host="localhost"
                ))
                self.assertFalse(webui._request_allowed(
                    path, "127.0.0.1", {}, request_host="external.example"
                ))

    def test_remote_progress_cannot_bypass_local_only_policy(self):
        self.assertFalse(webui._request_allowed("/api/progress", "192.168.1.20", {}))
        self.assertFalse(webui._request_allowed(
            "/api/progress", "192.168.1.20", {"allow_remote_progress": True}
        ))

    def test_public_webui_token_has_no_source_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(webui._configured_access_token(), "")

    def test_public_webui_accepts_query_bearer_and_cookie_tokens(self):
        from starlette.requests import Request

        def request(query: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
            return Request({
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": query,
                "headers": headers or [],
                "client": ("192.0.2.1", 1234),
                "server": ("example.invalid", 80),
            })

        expected = "test-access-token"
        self.assertTrue(webui._request_has_valid_access_token(
            request(b"token=test-access-token"), expected
        ))
        self.assertTrue(webui._request_has_valid_access_token(
            request(headers=[(b"authorization", b"Bearer test-access-token")]), expected
        ))
        self.assertTrue(webui._request_has_valid_access_token(
            request(headers=[(b"cookie", b"webui_token=test-access-token")]), expected
        ))
        self.assertFalse(webui._request_has_valid_access_token(
            request(b"token=wrong"), expected
        ))

    def test_downloaded_results_are_not_cacheable(self):
        with tempfile.TemporaryDirectory() as td:
            result_file = Path(td) / "results.txt"
            result_file.write_text("redacted-test-data\n", encoding="utf-8")
            with patch("claude_register.presentation.web.load_config", return_value={"output_file": str(result_file)}):
                response = webui._download_output_file("output_file", "results.txt")

        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_downloaded_results_resolve_from_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            project_root = Path(td)
            result_file = project_root / "runtime" / "results.txt"
            result_file.parent.mkdir()
            result_file.write_text("redacted-test-data\n", encoding="utf-8")
            with patch.object(webui, "PROJECT_ROOT", project_root), patch(
                "claude_register.presentation.web.load_config",
                return_value={"output_file": "runtime/results.txt"},
            ):
                response = webui._download_output_file("output_file", "results.txt")

        self.assertEqual(Path(response.path), result_file)

    def test_run_download_uses_only_its_own_archive(self):
        with tempfile.TemporaryDirectory() as td:
            project_root = Path(td)
            run_id = "run_123456789_abcdef12"
            run_file = project_root / "runtime" / "runs" / run_id / "results.txt"
            run_file.parent.mkdir(parents=True)
            run_file.write_text("current-run-only\n", encoding="utf-8")
            (project_root / "runtime" / "results.txt").write_text(
                "historical-record\n", encoding="utf-8"
            )

            with patch.object(webui, "PROJECT_ROOT", project_root):
                response = webui._download_run_output_file(run_id, "results.txt")

        self.assertEqual(Path(response.path), run_file)

    def test_start_assigns_a_dedicated_archive_to_each_run(self):
        captured: dict[str, str] = {}

        def fake_config_from_dict_full(config):
            captured["output_file"] = config["output_file"]
            captured["kyc_pass_file"] = config["kyc_pass_file"]
            return orch.OrchestratorConfig(), 1

        with tempfile.TemporaryDirectory() as td:
            with webui._run_lock:
                webui._state["running"] = False
                webui._state["tasks"] = []
            with patch.object(webui, "PROJECT_ROOT", Path(td)), \
                 patch("claude_register.presentation.web.load_config", return_value={}), \
                 patch("claude_register.presentation.web.orchestrator.parse_accounts", return_value=[orch.Account("a@x.com", "p", "a")]), \
                 patch("claude_register.presentation.web.orchestrator.config_from_dict_full", side_effect=fake_config_from_dict_full), \
                 patch("claude_register.presentation.web.orchestrator.orchestrate"):
                result = webui.start(webui.StartReq(accounts_text="a@x.com----p"))

        self.assertTrue(result["ok"])
        self.assertEqual(captured["output_file"], f"runtime/runs/{result['run_id']}/results.txt")
        self.assertEqual(captured["kyc_pass_file"], f"runtime/runs/{result['run_id']}/kyc_pass.txt")
        with webui._run_lock:
            webui._state["running"] = False

    def test_start_passes_session_flow_mode_to_config(self):
        captured: dict[str, object] = {}

        def fake_config_from_dict_full(cfg):
            captured["flow_mode"] = cfg.get("flow_mode")
            captured["mail_fast_path"] = cfg.get("mail_fast_path")
            return orch.OrchestratorConfig(flow_mode=cfg.get("flow_mode", "register")), 1

        with webui._run_lock:
            webui._state["running"] = False
            webui._state["tasks"] = []
        with patch("claude_register.presentation.web.load_config", return_value={}), \
             patch("claude_register.presentation.web.orchestrator.parse_accounts", return_value=[orch.Account("a@x.com", "p", "a")]), \
             patch("claude_register.presentation.web.orchestrator.config_from_dict_full", side_effect=fake_config_from_dict_full), \
             patch("claude_register.presentation.web.orchestrator.orchestrate"):
            result = webui.start(
                webui.StartReq(
                    flow_mode="session",
                    mail_fast_path=True,
                    accounts_text="a@x.com----p",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["flow_mode"], "session")
        self.assertTrue(captured["mail_fast_path"])
        with webui._run_lock:
            webui._state["running"] = False

    def test_shutdown_cancel_sets_current_cancel_event(self):
        cancel = threading.Event()
        with webui._run_lock:
            webui._cancel = cancel
            webui._worker_thread = None
            webui._state["running"] = True

        stopped = webui._cancel_current_run(wait_seconds=0)

        self.assertTrue(stopped)
        self.assertTrue(cancel.is_set())
        with webui._run_lock:
            webui._cancel = None
            webui._state["running"] = False


class TestWorkerOrchestration(unittest.TestCase):
    def test_config_accepts_session_flow_mode(self):
        oc, _ = orch.config_from_dict_full({"flow_mode": "session"})
        self.assertEqual(oc.flow_mode, "session")

    def test_config_rejects_unknown_flow_mode(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            orch.config_from_dict_full({"flow_mode": "bad"})

    def test_config_workers_override_concurrency(self):
        oc, concurrency = orch.config_from_dict_full({
            "workers": [{"proxy": "p1"}, {"proxy": "p2"}],
            "concurrency": 9,
        })
        self.assertEqual(concurrency, 2)
        self.assertEqual([w["proxy"] for w in oc.workers], ["p1", "p2"])

    def test_orchestrate_applies_worker_proxy_configs(self):
        accounts = [orch.Account("a@x.com", "p", "a")]
        cfg = orch.OrchestratorConfig(workers=[{"proxy": "p1"}])
        seen: list[str | None] = []

        def fake_register(task, local_cfg, _on_progress=None, _cancel=None):
            seen.append(local_cfg.proxy)
            task.status = "failed"
            task.error = "done"
            task.finished_at = 1
            return task

        with patch("claude_register.orchestration.service.register_one", side_effect=fake_register), \
             patch("claude_register.orchestration.service._write_result"):
            orch.orchestrate(accounts, cfg, concurrency=1)

        self.assertEqual(seen, ["p1"])

    def test_cancel_does_not_start_pending_tasks_or_write_them(self):
        accounts = [
            orch.Account("a@x.com", "p", "a"),
            orch.Account("b@x.com", "p", "b"),
            orch.Account("c@x.com", "p", "c"),
        ]
        cancel = threading.Event()
        calls: list[str] = []

        def fake_register(task, _cfg, _on_progress=None, _cancel=None):
            calls.append(task.account.email)
            task.status = "failed"
            task.error = "first failed"
            task.finished_at = 1
            cancel.set()
            return task

        with patch("claude_register.orchestration.service.register_one", side_effect=fake_register), \
             patch("claude_register.orchestration.service._write_result") as write_result:
            tasks = orch.orchestrate(
                accounts, orch.OrchestratorConfig(), concurrency=1, cancel=cancel
            )

        self.assertEqual(calls, ["a@x.com"])
        self.assertEqual(write_result.call_count, 1)
        self.assertEqual(tasks[1].status, "failed")
        self.assertEqual(tasks[1].error, "已取消（未开始）")
        self.assertEqual(tasks[2].error, "已取消（未开始）")


class TestRegisterSessionHandling(unittest.TestCase):
    def test_register_initializes_send_session_cookies(self):
        main_session = MagicMock()
        main_session.cookies.get.return_value = "sk"
        send_session = MagicMock()
        with patch.object(register, "build_session", return_value=main_session), \
             patch.object(register, "fetch_dynamic_config", return_value=("sha", [], "sentry", "org")), \
             patch.object(register, "init_browser_cookies") as init_cookies, \
             patch.object(register, "warm_claude_login") as warm, \
             patch.object(register, "prime_seen", return_value=set()), \
             patch.object(register, "login_methods", return_value=["magic_link"]), \
             patch.object(register, "send_magic_link", return_value={"sent": True}), \
             patch.object(register, "fetch_magic_link", return_value={"nonce": "n", "encoded_email_address": "e"}), \
             patch.object(register, "resolve_arkose_token", return_value="tok"), \
             patch.object(register, "verify_magic_link", return_value={
                 "account": {"uuid": "au", "memberships": [{"organization": {"uuid": "ou"}}]},
             }), \
             patch.object(register, "run_onboarding", return_value={}):
            register.register(
                "a@x.com", "p", display_name="Alice",
                send_session=send_session, arkose_config=orch.ArkoseConfig(),
            )
        sessions = [call.args[0] for call in init_cookies.call_args_list]
        self.assertIn(main_session, sessions)
        self.assertIn(send_session, sessions)
        warm.assert_any_call(main_session)
        warm.assert_any_call(send_session)

    def test_cancel_event_stops_mail_polling(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(mail_client.MailFetcherFatalError) as cm:
            mail_client.fetch_magic_link(
                "a@x.com", "p", poll_interval=0,
                poll_timeout=60, cancel_event=cancel
            )
        self.assertIn("已取消", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
