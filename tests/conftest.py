"""Never let mocked HTTP requests write to the operator's real audit/usage store."""
from pathlib import Path
import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolate_bridge_state(tmp_path, monkeypatch):
    from code_cn_bridge import stats, server
    state = tmp_path / "bridge-state"
    monkeypatch.setenv("LAN_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setattr(stats, "_default_usage_dir", lambda: state / "usage")
    monkeypatch.setattr(stats, "_stats", None)

    def isolated_audit():
        cfg = server.get_config()
        settings = getattr(cfg, "_data", {}).get("server", {})
        if str(settings.get("audit_enabled", settings.get("audit_requests", True))).lower() in {"false", "0", "off"}:
            return False, None
        configured = settings.get("audit_log_path")
        if configured:
            path = Path(configured).resolve()
            if path.is_relative_to(Path(tempfile.gettempdir()).resolve()):
                return True, path
        return True, state / "audit.jsonl"

    monkeypatch.setattr(server, "_audit_enabled", isolated_audit)
