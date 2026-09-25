"""AAD-QUAL-007 (logging configured before the rest of the app imports),
AAD-QUAL-009 (`_housekeeping`'s unused `app` param and function-local
imports that turned out to hide no real circular dependency), and
AAD-QUAL-011 (API version sourced from the build, not hardcoded).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
import sys

from app import main as main_module
from app.core.config import Settings, _default_app_version


class TestHousekeepingNoLongerTakesAnUnusedAppParam:
    def test_housekeeping_takes_no_arguments(self):
        sig = inspect.signature(main_module._housekeeping)
        assert list(sig.parameters) == []

    async def test_housekeeping_still_runs_the_sweep_loop(self, monkeypatch):
        ran = asyncio.Event()

        async def _fake_sweep_once():
            ran.set()

        monkeypatch.setattr(main_module, "_run_sweep_once", _fake_sweep_once)
        task = asyncio.create_task(main_module._housekeeping())
        try:
            await asyncio.wait_for(ran.wait(), timeout=1.0)
        finally:
            task.cancel()


class TestNoCircularImportBehindTheHoistedImports:
    """AAD-QUAL-009: the imports `_run_sweep_once` used to make function-local
    (sqlalchemy.text, session_scope, get_payment_provider, four repositories,
    OrderService) are now at module level in app/main.py. Function-local
    imports usually mean a circular dependency is being avoided — there
    wasn't one here, and this test is the standing proof: importing
    `app.main` fresh, in its own interpreter, must not raise ImportError."""

    def test_app_main_imports_cleanly_in_a_fresh_process(self):
        result = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=str(__file__.rsplit("/tests/", 1)[0]),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_the_repository_and_service_imports_are_module_level_not_local(self):
        source = inspect.getsource(main_module._run_sweep_once)
        # The function body itself no longer contains any import statement —
        # everything it uses was already bound at module import time.
        assert "    import " not in source
        assert "    from " not in source


class TestAppVersionComesFromTheBuildNotAHardcodedString:
    def test_default_app_version_falls_back_to_dev_with_nothing_set(self, monkeypatch):
        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
        monkeypatch.delenv("GIT_SHA", raising=False)
        assert _default_app_version() == "dev"

    def test_default_app_version_prefers_the_railway_injected_sha(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123def456789")
        monkeypatch.delenv("GIT_SHA", raising=False)
        assert _default_app_version() == "abc123def456"  # truncated to 12 chars

    def test_default_app_version_falls_back_to_git_sha_env_var(self, monkeypatch):
        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
        monkeypatch.setenv("GIT_SHA", "deadbeef")
        assert _default_app_version() == "deadbeef"

    def test_app_version_is_directly_settable_like_any_other_setting(self):
        assert Settings(app_version="1.2.3-rc1").app_version == "1.2.3-rc1"

    def test_the_fastapi_app_uses_settings_app_version_not_a_literal(self):
        assert main_module.app.version == main_module.settings.app_version
        assert main_module.app.version != "1.0.0"  # the old hardcoded literal


class TestLoggingIsConfiguredBeforeTheRestOfMainImports:
    """AAD-QUAL-007: `configure_logging()` used to run inside `lifespan()`,
    which only executes once uvicorn's ASGI lifespan startup event fires —
    strictly after every module `app.main` imports has already finished
    importing. It's now called at the very top of app/main.py, right after
    `settings`/`configure_logging` themselves are resolved, before any of
    the route/service/repository imports below it run.
    """

    def test_root_logger_already_has_a_handler_after_importing_app_main(self):
        # By the time this test runs, `app.main` has already been imported
        # (this module's own top-level `from app import main` above did it)
        # and nothing has called `lifespan()` — proving configure_logging()
        # ran at import time, not deferred to ASGI startup.
        assert logging.getLogger().handlers, (
            "root logger has no handler after importing app.main — "
            "configure_logging() must run before lifespan(), not inside it"
        )

    def test_a_fresh_subprocess_has_a_configured_root_logger_right_after_import(self):
        """Belt-and-braces version of the test above, immune to another test
        file having already imported (and configured logging for) app.main
        first: a brand-new interpreter that only imports app.main, with
        lifespan() never invoked, must still end up with a root handler."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import logging, app.main; "
                "assert logging.getLogger().handlers, 'no root handler after import'",
            ],
            cwd=str(__file__.rsplit("/tests/", 1)[0]),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_configure_logging_call_precedes_the_route_imports_in_source_order(self):
        source = inspect.getsource(main_module)
        configure_call_pos = source.index("configure_logging(settings.log_level)")
        router_import_pos = source.index("from app.api.v1.router import")
        assert configure_call_pos < router_import_pos
