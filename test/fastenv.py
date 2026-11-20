"""Shortens the harness's built-in delays for tests (inherited by the servers they start). Imported by test
modules for its side effect; values already set in the environment win."""
import os

FAST_ENV = {
    "SFH_MOCK_DELAY": "0",            # mock provider: seconds per streamed chunk (default 0.02)
    "SFH_MOCK_SLOW_DELAY": "0.05",    # ... for prompts with [[slow]] (default 0.3)
    "SFH_SAVE_DELAY": "0.05",         # debounce before writing the html file (default 1.5)
    "SFH_SHUTDOWN_TIMEOUT": "0.2",    # server stop: wait for open connections (default 2)
}
for _k, _v in FAST_ENV.items():
    os.environ.setdefault(_k, _v)
