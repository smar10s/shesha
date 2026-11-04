"""Suite-wide test policy."""
import pytest


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if any test was skipped.

    Hardware is required and every conditional skip has been removed.  A
    skip here means a test silently stopped covering its path, so treat it
    as a failure rather than a clean run.
    """
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter and reporter.stats.get("skipped"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
