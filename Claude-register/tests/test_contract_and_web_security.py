from __future__ import annotations

import asyncio
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from claude_register.orchestration import service as orch
from claude_register.presentation import web as webui


class TestRegistrationGoldenContract(unittest.TestCase):
    def test_account_task_keeps_legacy_positional_status_argument(self):
        task = orch.AccountTask(
            orch.Account("person@example.invalid", "password", "Person"),
            "success",
        )

        self.assertEqual(task.status, "success")

    def test_register_flow_keeps_the_protected_call_order(self):
        calls: list[str] = []
        versions: list[int] = []
        session = MagicMock()
        session.cookies.get.return_value = "session-fixture"
        verify_response = {
            "account": {
                "uuid": "account-fixture",
                "memberships": [{"organization": {"uuid": "org-fixture"}}],
            }
        }

        def record(name: str, result):
            def invoke(*_args, **_kwargs):
                calls.append(name)
                return result

            return invoke

        with patch.object(orch, "build_session", return_value=session), \
             patch.object(orch, "init_browser_cookies"), \
             patch.object(
                 orch,
                 "_fetch_dynamic_config_with_cache",
                 side_effect=record(
                     "dynamic_config",
                     ("client-sha", [], "sentry-key", "sentry-org", "1.0.0"),
                 ),
             ), \
             patch.object(orch, "warm_claude_login", side_effect=record("warm_login", None)), \
             patch.object(orch, "prime_seen", side_effect=record("prime_seen", set())), \
             patch.object(orch, "login_methods", side_effect=record("login_methods", ["magic_link"])), \
             patch.object(orch, "send_magic_link", side_effect=record("send_magic_link", {"sent": True})), \
             patch.object(
                 orch,
                 "fetch_magic_link",
                 side_effect=record(
                     "fetch_magic_link",
                     {"nonce": "nonce", "encoded_email_address": "encoded", "mail_id": "mail-1"},
                 ),
             ), \
             patch.object(orch, "resolve_arkose_token", side_effect=record("resolve_challenge", "token")), \
             patch.object(orch, "warm_claude_bootstrap", side_effect=record("bootstrap", None)), \
             patch.object(
                 orch,
                 "verify_magic_link",
                 side_effect=record("verify_magic_link", verify_response),
             ), \
             patch.object(orch, "run_onboarding", side_effect=record("run_onboarding", {})), \
             patch.object(orch, "onboarding_failed_steps", return_value=[]), \
             patch.object(
                 orch,
                 "check_kyc_status",
                 side_effect=record("check_kyc", (True, "not_required")),
             ), \
             patch.object(orch, "_write_result", side_effect=record("write_result", None)), \
             patch.object(orch, "_wait_cancelable", return_value=False):
            tasks = orch.orchestrate(
                [orch.Account("person@example.invalid", "password", "Person")],
                orch.OrchestratorConfig(auto_send=True, retry_max=0),
                concurrency=1,
                on_progress=lambda task: versions.append(task.version),
            )

        self.assertEqual(tasks[0].status, "success")
        self.assertEqual(
            calls,
            [
                "dynamic_config",
                "warm_login",
                "prime_seen",
                "login_methods",
                "send_magic_link",
                "fetch_magic_link",
                "resolve_challenge",
                "bootstrap",
                "verify_magic_link",
                "bootstrap",
                "run_onboarding",
                "check_kyc",
                "write_result",
            ],
        )
        self.assertGreater(len(versions), 1)
        self.assertEqual(versions, sorted(set(versions)))
        self.assertEqual(
            set(tasks[0].to_public_dict()["substage_durations_ms"]),
            {
                "send.warm_login",
                "send.login_methods",
                "send.magic_link",
            },
        )

    def test_verify_failure_retries_without_rematerializing_identity_or_proxy(self):
        session = MagicMock()
        session.cookies.get.return_value = "session-fixture"
        task = orch.AccountTask(
            account=orch.Account("person@example.invalid", "password", "Person")
        )
        identity = MagicMock()
        identity.profile = MagicMock()
        identity.anonymous_id = "anonymous"
        identity.device_id = "device"
        verify_response = {
            "account": {
                "uuid": "account-fixture",
                "memberships": [{"organization": {"uuid": "org-fixture"}}],
            }
        }

        with patch.object(
            orch, "materialize_session_proxy", return_value=("http://proxy", "sticky")
        ) as materialize, patch.object(
            orch, "new_browser_identity", return_value=identity
        ) as new_identity, patch.object(
            orch, "build_session", return_value=session
        ), patch.object(
            orch, "init_browser_cookies"
        ), patch.object(
            orch,
            "_fetch_dynamic_config_with_cache",
            return_value=("client-sha", [], "sentry-key", "sentry-org", "1.0.0"),
        ), patch.object(
            orch, "warm_claude_login"
        ), patch.object(
            orch, "prime_seen", return_value=set()
        ) as prime_seen, patch.object(
            orch, "login_methods", return_value=["magic_link"]
        ), patch.object(
            orch, "send_magic_link", return_value={"sent": True}
        ) as send_magic_link, patch.object(
            orch,
            "fetch_magic_link",
            return_value={"nonce": "nonce", "encoded_email_address": "encoded", "mail_id": "mail-1"},
        ) as fetch_mail, patch.object(
            orch, "resolve_arkose_token", return_value="token"
        ), patch.object(
            orch, "warm_claude_bootstrap"
        ), patch.object(
            orch,
            "verify_magic_link",
            side_effect=[RuntimeError("HTTP 503"), verify_response],
        ), patch.object(
            orch, "run_onboarding", return_value={}
        ), patch.object(
            orch, "onboarding_failed_steps", return_value=[]
        ), patch.object(
            orch, "check_kyc_status", return_value=(True, "not_required")
        ), patch.object(
            orch, "_wait_cancelable", return_value=False
        ):
            orch.register_one(
                task,
                orch.OrchestratorConfig(
                    auto_send=True,
                    retry_max=1,
                    mail_fast_path=True,
                    proxy_template="http://proxy/{session}",
                ),
            )

        self.assertEqual(task.status, "success")
        self.assertEqual(task.attempts, 2)
        materialize.assert_called_once()
        new_identity.assert_called_once()
        prime_seen.assert_called_once()
        self.assertIsNone(prime_seen.call_args.kwargs["rate_limiter"])
        self.assertTrue(fetch_mail.call_args.kwargs["mail_fast_path"])
        self.assertEqual(send_magic_link.call_count, 2)


class TestPublicWebBoundary(unittest.TestCase):
    def setUp(self):
        with webui._run_lock:
            self.previous_state = dict(webui._state)

    def tearDown(self):
        with webui._run_lock:
            webui._state.clear()
            webui._state.update(self.previous_state)

    def test_sse_snapshot_is_an_allowlisted_public_task_shape(self):
        account = orch.Account(
            "visible@example.invalid",
            "PASSWORD_SENTINEL",
            "Visible Person",
            mail_client_id="MAIL_CLIENT_ID_SENTINEL",
            mail_refresh_token="REFRESH_TOKEN_SENTINEL",
            deliver_prefix="DELIVERY_SENTINEL",
        )
        task = orch.AccountTask(
            account=account,
            status="success",
            session_key="SESSION_KEY_SENTINEL",
            account_uuid="ACCOUNT_UUID_SENTINEL",
            org_uuid="ORG_UUID_SENTINEL",
            proxy_session="PROXY_PASSWORD_SENTINEL",
            proxy_exit_ip="203.0.113.9",
        )
        with webui._run_lock:
            webui._state.update(
                {
                    "running": True,
                    "run_id": "run_123456789_abcdef12",
                    "flow_mode": "register",
                    "tasks": [task],
                }
            )

        snapshot = webui._snapshot()
        encoded = json.dumps(snapshot, ensure_ascii=False)

        for sentinel in (
            "PASSWORD_SENTINEL",
            "MAIL_CLIENT_ID_SENTINEL",
            "REFRESH_TOKEN_SENTINEL",
            "DELIVERY_SENTINEL",
            "SESSION_KEY_SENTINEL",
            "ACCOUNT_UUID_SENTINEL",
            "ORG_UUID_SENTINEL",
            "PROXY_PASSWORD_SENTINEL",
        ):
            self.assertNotIn(sentinel, encoded)
        self.assertTrue(snapshot["tasks"][0]["has_session"])
        self.assertEqual(snapshot["tasks"][0]["proxy_session"], "sticky")
        self.assertEqual(snapshot["tasks"][0]["proxy_exit_ip"], "203.0.113.9")
        self.assertEqual(
            set(snapshot["tasks"][0]),
            {
                "task_id",
                "version",
                "email",
                "display_name",
                "status",
                "stage",
                "kyc_status",
                "worker_id",
                "proxy_session",
                "attempts",
                "error_class",
                "retryable",
                "elapsed",
                "queue_wait_ms",
                "stage_elapsed_ms",
                "stage_durations_ms",
                "substage_durations_ms",
                "proxy_exit_ip",
                "has_session",
                "persistence_status",
                "persistence_error_class",
            },
        )

    def test_incremental_sse_event_contains_only_the_changed_public_task(self):
        account = orch.Account(
            "visible@example.invalid",
            "PASSWORD_SENTINEL",
            "Visible Person",
            mail_refresh_token="REFRESH_TOKEN_SENTINEL",
        )
        task = orch.AccountTask(
            account=account,
            session_key="SESSION_KEY_SENTINEL",
            proxy_session="http://user:PROXY_PASSWORD_SENTINEL@proxy.invalid",
        )
        with webui._run_lock:
            webui._state.update(
                {
                    "running": True,
                    "run_id": "run_123456789_abcdef12",
                    "flow_mode": "register",
                    "tasks": [task],
                }
            )
        webui._event_bus.reset()
        cursor = webui._event_bus.cursor

        task.publish_update()
        webui._publish_task_update(task)

        events = webui._event_bus.events_after(cursor)
        self.assertIsNotNone(events)
        self.assertEqual([event.event_type for event in events or []], [
            "task_updated",
            "summary_updated",
        ])
        task_payload = (events or [])[0].data["task"]
        self.assertEqual(task_payload["task_id"], task.task_id)
        self.assertEqual(task_payload["version"], 1)
        encoded = json.dumps([event.data for event in events or []])
        for sentinel in (
            "PASSWORD_SENTINEL",
            "REFRESH_TOKEN_SENTINEL",
            "SESSION_KEY_SENTINEL",
            "PROXY_PASSWORD_SENTINEL",
        ):
            self.assertNotIn(sentinel, encoded)

    def test_sse_replays_after_last_event_id_without_repeating_the_snapshot(self):
        webui._event_bus.reset()
        cursor = webui._event_bus.cursor
        event = webui._event_bus.publish(
            "task_updated",
            {
                "run_id": "run_123456789_abcdef12",
                "task": {"task_id": "task-fixture", "version": 4},
            },
        )

        async def first_chunk() -> str:
            stream = webui._progress_events(cursor)
            try:
                return await anext(stream)
            finally:
                await stream.aclose()

        chunk = asyncio.run(first_chunk())

        self.assertIn(f"id: {event.event_id}", chunk)
        self.assertIn("event: task_updated", chunk)
        self.assertNotIn("event: run_started", chunk)

    def test_sse_sends_heartbeat_without_repeating_tasks_while_idle(self):
        webui._event_bus.reset()
        with webui._run_lock:
            webui._state.update(
                {
                    "running": False,
                    "run_id": "run_123456789_abcdef12",
                    "flow_mode": "register",
                    "tasks": [],
                }
            )

        async def first_two_chunks() -> tuple[str, str]:
            stream = webui._progress_events(None)
            try:
                first = await anext(stream)
                with patch.object(webui, "SSE_HEARTBEAT_SECONDS", 0):
                    second = await anext(stream)
                return first, second
            finally:
                await stream.aclose()

        snapshot, heartbeat = asyncio.run(first_two_chunks())

        self.assertIn("event: run_started", snapshot)
        self.assertIn('"tasks":[]', snapshot)
        self.assertIn("event: heartbeat", heartbeat)
        self.assertNotIn('"tasks"', heartbeat)

    def test_invalid_last_event_id_falls_back_to_a_fresh_snapshot(self):
        self.assertIsNone(webui._parse_last_event_id("not-an-event"))
        self.assertIsNone(webui._parse_last_event_id("-1"))
        self.assertEqual(webui._parse_last_event_id("12"), 12)

    def test_query_access_token_is_removed_before_access_logging(self):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/healthz",
            "raw_path": b"/healthz",
            "query_string": b"view=summary&token=ACCESS_LOG_SENTINEL",
            "root_path": "",
            "headers": [(b"host", b"external.example")],
            "client": ("192.0.2.1", 1234),
            "server": ("external.example", 80),
            "state": {},
        }
        request_pending = True
        sent: list[dict] = []

        async def receive() -> dict:
            nonlocal request_pending
            if request_pending:
                request_pending = False
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        with patch.dict("os.environ", {webui.ACCESS_TOKEN_ENV: "ACCESS_LOG_SENTINEL"}):
            asyncio.run(webui.app(scope, receive, send))

        response_start = next(message for message in sent if message["type"] == "http.response.start")
        response_headers = dict(response_start["headers"])
        self.assertEqual(response_start["status"], 303)
        self.assertEqual(scope["query_string"], b"view=summary")
        self.assertNotIn(b"ACCESS_LOG_SENTINEL", scope["query_string"])
        self.assertNotIn(webui.ACCESS_TOKEN_SCOPE_KEY, scope)
        self.assertEqual(
            response_headers[b"location"],
            b"http://external.example/healthz?view=summary",
        )
        self.assertIn(b"webui_token=ACCESS_LOG_SENTINEL", response_headers[b"set-cookie"])

    def test_config_returns_only_a_redacted_proxy_preview(self):
        config = {
            "proxy_template": "http://proxy-user:PROXY_PASSWORD_SENTINEL@proxy.invalid:8080",
            "arkose": {
                "solver": {"api_key": "CHALLENGE_KEY_SENTINEL"},
                "passive_token": "CHALLENGE_TOKEN_SENTINEL",
            },
        }
        with patch.object(webui, "load_config", return_value=config):
            result = webui.get_config()

        encoded = json.dumps(result)
        self.assertTrue(result["proxy_configured"])
        self.assertEqual(result["proxy_preview"], "http://***:***@proxy.invalid:8080")
        self.assertNotIn("proxy_template", result)
        self.assertNotIn("arkose_key", result)
        self.assertNotIn("arkose_key_configured", result)
        self.assertNotIn("passive_token", result)
        self.assertNotIn("passive_token_configured", result)
        for sentinel in (
            "PROXY_PASSWORD_SENTINEL",
            "CHALLENGE_KEY_SENTINEL",
            "CHALLENGE_TOKEN_SENTINEL",
        ):
            self.assertNotIn(sentinel, encoded)

    def test_browser_cannot_submit_or_render_challenge_secrets(self):
        for field_name in ("arkose_key", "passive_token"):
            with self.subTest(field_name=field_name), self.assertRaises(ValidationError):
                webui.StartReq.model_validate(
                    {
                        "accounts_text": "person@example.invalid----password",
                        field_name: "CHALLENGE_SECRET_SENTINEL",
                    }
                )

        markup = (webui.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        script = (webui.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for field_name in ("arkose_key", "passive_token"):
            self.assertNotIn(field_name, markup)
            self.assertNotIn(field_name, script)

    def test_config_detects_worker_proxy_without_exposing_it(self):
        config = {
            "workers": [
                {
                    "proxy": "http://worker:PROXY_PASSWORD_SENTINEL@worker.invalid:8080"
                }
            ]
        }
        with patch.object(webui, "load_config", return_value=config):
            result = webui.get_config()

        self.assertTrue(result["proxy_configured"])
        self.assertEqual(result["proxy_preview"], "http://***:***@worker.invalid:8080")
        self.assertNotIn("PROXY_PASSWORD_SENTINEL", json.dumps(result))

    def test_current_run_returns_only_safe_run_metadata(self):
        with webui._run_lock:
            webui._state.update(
                {
                    "running": True,
                    "run_id": "run_123456789_abcdef12",
                    "flow_mode": "register",
                    "started_at": 123.0,
                    "tasks": [
                        orch.AccountTask(
                            account=orch.Account(
                                "visible@example.invalid", "PASSWORD_SENTINEL", "Visible"
                            ),
                            session_key="SESSION_KEY_SENTINEL",
                        )
                    ],
                }
            )

        result = webui.current_run()

        self.assertEqual(
            result,
            {
                "running": True,
                "run_id": "run_123456789_abcdef12",
                "flow_mode": "register",
                "started_at": 123.0,
            },
        )
        self.assertNotIn("SENTINEL", json.dumps(result))

    def test_proxy_modes_have_unambiguous_server_semantics(self):
        configured = {
            "proxy_template": "http://configured",
            "proxy": "http://fallback",
            "workers": [{"proxy": "worker-proxy", "interval_seconds": 7}],
        }
        webui._apply_proxy_mode(
            configured,
            webui.StartReq(proxy_mode="configured", accounts_text="fixture"),
        )
        self.assertEqual(configured["proxy_template"], "http://configured")
        self.assertEqual(configured["workers"][0]["proxy"], "worker-proxy")

        overridden = {
            "proxy_template": "http://configured",
            "proxy": "http://fallback",
            "workers": [{"proxy": "worker-proxy", "interval_seconds": 7}],
        }
        webui._apply_proxy_mode(
            overridden,
            webui.StartReq(
                proxy_mode="override",
                proxy_template="http://override",
                accounts_text="fixture",
            ),
        )
        self.assertEqual(overridden["proxy_template"], "http://override")
        self.assertIsNone(overridden["proxy"])
        self.assertEqual(overridden["workers"][0]["proxy_template"], "http://override")
        self.assertIsNone(overridden["workers"][0]["proxy"])
        self.assertEqual(overridden["workers"][0]["interval_seconds"], 7)

        direct = {
            "proxy_template": "http://configured",
            "proxy": "http://fallback",
            "workers": [
                {
                    "proxy": "worker-proxy",
                    "proxy_template": "worker-template",
                    "interval_seconds": 7,
                }
            ],
        }
        webui._apply_proxy_mode(
            direct,
            webui.StartReq(proxy_mode="direct", accounts_text="fixture"),
        )
        self.assertIsNone(direct["proxy_template"])
        self.assertIsNone(direct["proxy"])
        self.assertIsNone(direct["workers"][0]["proxy"])
        self.assertIsNone(direct["workers"][0]["proxy_template"])
        self.assertEqual(direct["workers"][0]["interval_seconds"], 7)

    def test_validation_errors_do_not_echo_submitted_secrets(self):
        error = RequestValidationError(
            [
                {
                    "type": "int_parsing",
                    "loc": ("body", "concurrency"),
                    "msg": "Input should be a valid integer",
                    "input": "PASSWORD_SENTINEL",
                }
            ]
        )

        response = asyncio.run(webui.redacted_validation_error(None, error))

        self.assertEqual(response.status_code, 422)
        self.assertNotIn("PASSWORD_SENTINEL", response.body.decode("utf-8"))

    def test_frontend_keeps_only_the_active_run_id_in_session_storage(self):
        script = (webui.STATIC_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn('sessionStorage.getItem("activeRunId")', script)
        self.assertIn('sessionStorage.setItem("activeRunId", state.runId)', script)
        self.assertNotIn("task.session_key", script)
        self.assertNotIn("task.password", script)
        self.assertNotIn("task.mail_refresh_token", script)
        self.assertNotIn("task.deliver_prefix", script)

    def test_concurrent_starts_reserve_before_initializing_run_resources(self):
        first_load = threading.Event()
        second_observed = threading.Event()
        release_load = threading.Event()
        release_run = threading.Event()
        load_lock = threading.Lock()
        load_count = 0
        results: list[dict] = []

        def blocked_load_config():
            nonlocal load_count
            with load_lock:
                load_count += 1
                if load_count == 1:
                    first_load.set()
                else:
                    second_observed.set()
            self.assertTrue(release_load.wait(2))
            return {}

        def invoke_start(index: int) -> None:
            try:
                results.append(
                    webui.start(
                        webui.StartReq(
                            accounts_text=f"person{index}@example.invalid----password----Person"
                        )
                    )
                )
            finally:
                if index == 2:
                    second_observed.set()

        def blocked_orchestrate(*_args, **_kwargs):
            self.assertTrue(release_run.wait(2))
            return []

        state_store = MagicMock()
        recovery_store = MagicMock()
        with webui._run_lock:
            webui._state.update({"running": False, "starting": False, "tasks": []})
            webui._cancel = None
            webui._worker_thread = None

        with patch.object(webui, "load_config", side_effect=blocked_load_config), patch.object(
            webui, "_configure_run_outputs"
        ) as configure_outputs, patch.object(
            webui.orchestrator,
            "config_from_dict_full",
            return_value=(webui.orchestrator.OrchestratorConfig(), 1),
        ), patch.object(
            webui, "RunStateStore", return_value=state_store
        ) as run_state_store, patch.object(
            webui, "VerifiedRecoveryStore", return_value=recovery_store
        ) as verified_recovery_store, patch.object(
            webui.orchestrator, "orchestrate", side_effect=blocked_orchestrate
        ):
            first = threading.Thread(target=invoke_start, args=(1,))
            second = threading.Thread(target=invoke_start, args=(2,))
            first.start()
            self.assertTrue(first_load.wait(2))
            second.start()
            self.assertTrue(second_observed.wait(2))
            release_load.set()
            first.join(2)
            second.join(2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(sum(bool(result.get("ok")) for result in results), 1)
            configure_outputs.assert_called_once()
            run_state_store.assert_called_once()
            verified_recovery_store.assert_called_once()

            with webui._run_lock:
                worker = webui._worker_thread
            release_run.set()
            if worker is not None:
                worker.join(2)

        with webui._run_lock:
            webui._state.update({"running": False, "starting": False, "tasks": []})
            webui._cancel = None
            webui._worker_thread = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
