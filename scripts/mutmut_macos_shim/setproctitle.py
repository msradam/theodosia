"""No-op setproctitle stand-in for running mutmut on macOS.

mutmut renames each forked worker via setproctitle; the real C extension
calls into CoreFoundation, and doing that after fork() segfaults on macOS
(EXC_BAD_ACCESS in _os_log_preferences_refresh). Putting this directory on
PYTHONPATH for the mutmut run shadows the C module with harmless no-ops:

    PYTHONPATH=scripts/mutmut_macos_shim uv run mutmut run

Linux (including CI) does not need the shim.
"""


def setproctitle(title):  # signature mirrors the real module
    return None


def getproctitle():
    return ""
