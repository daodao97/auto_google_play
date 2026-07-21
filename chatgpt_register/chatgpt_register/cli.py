"""Command-line entry point for the batch registration machine."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from chatgpt_register.orchestration.models import OrchestratorConfig
from chatgpt_register.orchestration.service import orchestrate, parse_accounts


def _load_config(path: Path) -> tuple[dict, Path]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("config root must be an object")
    return data, path.parent


def _resolve_path(value: str, *, project_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def main() -> int:
    parser = argparse.ArgumentParser(description="ChatGPT OTP registration machine")
    parser.add_argument("--config", default="runtime/config.json")
    parser.add_argument("--confirm-external-run", action="store_true")
    args = parser.parse_args()
    if not args.confirm_external_run:
        parser.error("real registration requires --confirm-external-run")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = Path(args.config).resolve()
    raw, _config_dir = _load_config(config_path)
    project_root = Path.cwd()
    accounts_path = _resolve_path(str(raw.get("accounts_file") or "runtime/accounts.txt"), project_root=project_root)
    output_root = _resolve_path(str(raw.get("output_root") or "runtime/runs"), project_root=project_root)
    accounts = parse_accounts(accounts_path.read_text(encoding="utf-8"))
    if not accounts:
        raise SystemExit("no valid accounts")

    config = OrchestratorConfig(
        output_root=output_root,
        concurrency=int(raw.get("concurrency", 1)),
        retry_max=int(raw.get("retry_max", 0)),
        proxy=str(raw.get("proxy") or ""),
        proxy_template=str(raw.get("proxy_template") or ""),
        country_code=str(raw.get("country_code") or "US"),
        impersonate=str(raw.get("impersonate") or "chrome136"),
        mail_api_base=str(raw.get("mail_api_base") or "http://127.0.0.1:8787"),
        mail_app_token=str(raw.get("mail_app_token") or ""),
    )

    def progress(snapshot: dict) -> None:
        logging.info(
            "task=%s email=%s status=%s stage=%s attempts=%s created=%s",
            snapshot["task_id"][:8],
            snapshot["email"],
            snapshot["status"],
            snapshot["stage"],
            snapshot["attempts"],
            snapshot["created"],
        )

    run_id, tasks = orchestrate(accounts, config, on_progress=progress)
    success = sum(task.status == "success" for task in tasks)
    partial = sum(task.status == "partial" for task in tasks)
    failed = sum(task.status == "failed" for task in tasks)
    print(json.dumps({"run_id": run_id, "success": success, "partial": partial, "failed": failed}))
    return 0 if failed == 0 and partial == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

