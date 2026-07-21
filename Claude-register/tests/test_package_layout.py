"""Architecture tests for the domain package layout."""

from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


class _UiMarkupInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.label_targets: set[str] = set()
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []
        self.attributes_by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            element_id = values["id"] or ""
            self.ids.add(element_id)
            self.attributes_by_id[element_id] = values
        if tag == "label" and values.get("for"):
            self.label_targets.add(values["for"] or "")
        if tag == "script" and values.get("src"):
            self.scripts.append(values["src"] or "")
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.stylesheets.append(values["href"] or "")


class TestPackageLayout(unittest.TestCase):
    def test_repository_root_contains_no_python_scripts_or_legacy_mail_service(self):
        project_root = Path(__file__).resolve().parents[1]

        self.assertEqual(list(project_root.glob("*.py")), [])
        self.assertFalse((project_root / "mail").exists())

    def test_canonical_domain_modules_import(self):
        from claude_register.auth import service as auth_service
        from claude_register.mail import fetcher
        from claude_register.orchestration import service as orchestration_service
        from claude_register.presentation import web

        self.assertTrue(callable(orchestration_service.orchestrate))
        self.assertTrue(callable(auth_service.register))
        self.assertTrue(callable(fetcher.fetch_magic_link))
        self.assertIsNotNone(web.app)

    def test_orchestration_service_reexports_canonical_models(self):
        from claude_register.orchestration import models
        from claude_register.orchestration import service

        self.assertIs(service.Account, models.Account)
        self.assertIs(service.AccountTask, models.AccountTask)
        self.assertIs(service.AccountParseIssue, models.AccountParseIssue)
        self.assertIs(service.AccountParseReport, models.AccountParseReport)
        self.assertIs(service.PublicTaskSnapshot, models.PublicTaskSnapshot)
        self.assertIs(service.OrchestratorConfig, models.OrchestratorConfig)

    def test_orchestration_service_preserves_parsing_entrypoints(self):
        from claude_register.orchestration import parsing
        from claude_register.orchestration import service

        line = "person@example.invalid----password----Person"
        parsed = service.parse_accounts(line)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].email, "person@example.invalid")
        self.assertEqual(parsed[0].password, "password")
        self.assertIs(service._split_account_fields, parsing._split_account_fields)
        self.assertTrue(callable(parsing.parse_accounts_with_report))

    def test_web_implementation_resolves_project_static_directory(self):
        from claude_register.presentation import web

        self.assertTrue((web.HERE / "static" / "index.html").is_file())

    def test_web_app_mounts_static_assets(self):
        from claude_register.presentation import web

        self.assertTrue(any(getattr(route, "path", "") == "/assets" for route in web.app.routes))

    def test_web_app_exposes_a_local_health_check(self):
        from claude_register.presentation import web

        self.assertTrue(any(getattr(route, "path", "") == "/healthz" for route in web.app.routes))

    def test_web_ui_is_split_into_maintainable_static_assets(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        inspector = _UiMarkupInspector()
        inspector.feed(html)

        self.assertTrue((static_dir / "styles.css").is_file())
        self.assertTrue((static_dir / "app.js").is_file())
        self.assertIn("/assets/styles.css", inspector.stylesheets)
        self.assertIn("/assets/app.js", inspector.scripts)
        self.assertNotIn("<style>", html)
        self.assertNotIn("<script>", html)

    def test_web_ui_has_operational_filters_and_accessible_form_labels(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        inspector = _UiMarkupInspector()
        inspector.feed(html)

        required_ids = {
            "task_search", "task_filter", "connection_status", "status_line",
            "accounts_text", "mail_provider", "btn_start", "btn_stop",
        }
        self.assertTrue(required_ids.issubset(inspector.ids))
        self.assertTrue({"accounts_text", "mail_provider", "task_search"}.issubset(inspector.label_targets))
        self.assertIn('value="mailcom"', html)
        self.assertIn('value="imap"', html)
        self.assertIn('value="microsoft"', html)
        self.assertNotIn('value="auto"', html)

    def test_web_ui_keeps_sensitive_raw_logs_out_of_the_dashboard(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("log_console", html)
        self.assertNotIn("实时日志", html)
        self.assertNotIn("data.logs", html)

    def test_web_ui_has_persistent_errors_stop_confirmation_and_mobile_controls(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        inspector = _UiMarkupInspector()
        inspector.feed(html)

        required_ids = {
            "error_banner", "error_message", "btn_dismiss_error", "last_update",
            "stop_confirm", "btn_confirm_stop", "btn_cancel_stop",
            "mobile_run_dock", "mobile_btn_start", "mobile_btn_stop",
        }
        self.assertTrue(required_ids.issubset(inspector.ids))
        self.assertIn('role="alert"', html)

    def test_web_ui_separates_kyc_metrics_and_exposes_pagination(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        inspector = _UiMarkupInspector()
        inspector.feed(html)

        required_ids = {"page_prev", "page_next", "page_label", "task_page_size"}
        self.assertTrue(required_ids.issubset(inspector.ids))
        self.assertIn('class="kyc-summary"', html)
        self.assertIn('aria-pressed="true"', html)
        for element_id in ("task_search", "task_filter", "task_page_size"):
            self.assertIn(element_id, inspector.label_targets)

    def test_web_ui_bounds_task_and_result_dom_work(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        script = (static_dir / "app.js").read_text(encoding="utf-8")

        self.assertIn("TASK_PAGE_SIZE", script)
        self.assertIn("RESULT_CARD_LIMIT", script)
        self.assertNotIn("state.tasks.indexOf(task)", script)
        self.assertNotIn('body.innerHTML = tasks.map', script)

    def test_web_ui_applies_incremental_sse_events_and_uses_native_reconnect(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        script = (static_dir / "app.js").read_text(encoding="utf-8")

        for event_type in (
            "run_started",
            "task_updated",
            "summary_updated",
            "run_finished",
            "heartbeat",
        ):
            self.assertIn(f'addEventListener("{event_type}"', script)
        self.assertIn("task.version", script)
        self.assertIn("current?.version || 0) >= Number(task.version", script)
        self.assertNotIn("window.setTimeout(connectProgress, 2000)", script)

    def test_web_ui_live_ticks_running_task_timing_between_sse_events(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        script = (static_dir / "app.js").read_text(encoding="utf-8")

        self.assertIn("function liveElapsed", script)
        self.assertIn("function liveStageElapsedMs", script)
        self.assertIn("window.setInterval", script)
        self.assertIn("renderTable();", script)
        self.assertIn("refreshOpenTaskDetails();", script)

    def test_web_ui_shows_send_timing_breakdown_in_task_details(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        script = (static_dir / "app.js").read_text(encoding="utf-8")

        self.assertIn("function appendTimingBreakdown", script)
        self.assertIn('substageDurations["send.warm_login"]', script)
        self.assertIn('substageDurations["send.login_methods"]', script)
        self.assertIn('substageDurations["send.magic_link"]', script)
        self.assertIn('"发信请求"', script)

    def test_web_ui_has_professional_runtime_context_and_brand_assets(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        script = (static_dir / "app.js").read_text(encoding="utf-8")

        self.assertTrue((static_dir / "favicon.svg").is_file())
        self.assertIn('href="/assets/favicon.svg"', html)
        self.assertIn('id="access_scope"', html)
        self.assertIn("updateAccessScope", script)
        self.assertIn("window.location.hostname", script)
        self.assertNotIn("仅限本机访问", html)
        self.assertNotIn("Run setup", html)
        self.assertNotIn("Live queue", html)
        self.assertNotIn("Result buckets", html)
        self.assertNotIn("⌕", html)

    def test_web_ui_uses_accessible_control_sizes_and_visible_run_actions(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        css = (static_dir / "styles.css").read_text(encoding="utf-8")

        self.assertRegex(css, r"(?s)input,\s*select\s*\{[^}]*min-height:\s*44px;")
        self.assertRegex(css, r"(?s)\.mobile-run-dock \.button\s*\{[^}]*min-height:\s*44px;")
        self.assertLess(html.index('class="primary-actions"'), html.index('class="advanced-settings"'))

    def test_web_ui_preserves_fluid_feedback_and_accessibility_preferences(self):
        static_dir = Path(__file__).resolve().parents[1] / "claude_register" / "presentation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        css = (static_dir / "styles.css").read_text(encoding="utf-8")

        self.assertIn('class="brand-mark" src="/assets/favicon.svg"', html)
        self.assertIn('class="run-feedback"', html)
        self.assertIn("button:active:not(:disabled)", css)
        self.assertIn(".check-row input:checked::before", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("@media (prefers-reduced-transparency: reduce)", css)
        self.assertIn("@media (prefers-contrast: more)", css)
        self.assertNotRegex(css, r"letter-spacing:\s*-")


if __name__ == "__main__":
    unittest.main(verbosity=2)
