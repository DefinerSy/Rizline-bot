#!/usr/bin/env python3
"""Run an external RizLine login script while enforcing TLS verification."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import requests


FAILURE_PREFIX = "RIZLINE_LOCAL_FAILURE_"


def _report_failure(kind: str) -> int:
    """Emit a fixed, non-sensitive marker for the loopback UI to classify."""
    print(f"{FAILURE_PREFIX}{kind}", flush=True)
    return 1


def _require_tls_verification(request):
    def wrapped(*args, **kwargs):
        kwargs["verify"] = True
        return request(*args, **kwargs)

    return wrapped


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if len(arguments) != 1:
        print("Usage: rizline_login_runner.py /path/to/getUser.py", file=sys.stderr)
        return 2
    script = Path(arguments[0]).resolve()
    if not script.is_file():
        print("Login script not found.", file=sys.stderr)
        return 2

    # ``runpy`` keeps the wrapper's script directory on sys.path.  The
    # external script imports sibling modules such as gameDataAes2Json.py, so
    # explicitly add its own directory before executing it.
    if str(script.parent) not in sys.path:
        sys.path.insert(0, str(script.parent))

    # The upstream script currently requests ``verify=False``.  Credentials
    # must never travel through a connection with certificate checks disabled.
    requests.get = _require_tls_verification(requests.get)
    requests.post = _require_tls_verification(requests.post)
    sys.argv = [str(script)]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except requests.exceptions.SSLError:
        return _report_failure("TLS")
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return _report_failure("NETWORK")
    except ModuleNotFoundError as exc:
        # A module name is code/environment metadata, not a login detail.  Do
        # not pass arbitrary exception text through the browser; use a small
        # allow-list so the operator can repair the right environment.
        missing_module = exc.name or ""
        missing_kind = {
            "requests": "REQUESTS",
            "urllib3": "URLLIB3",
            "Crypto": "CRYPTO",
            "gameDataAes2Json": "DECRYPTOR",
        }.get(missing_module, "OTHER")
        return _report_failure(f"DEPENDENCY_{missing_kind}")
    except ImportError:
        return _report_failure("DEPENDENCY_OTHER")
    except EOFError:
        return _report_failure("INPUT")
    except SystemExit as exc:
        if exc.code in (None, 0):
            return 0
        return _report_failure("SCRIPT")
    except (AttributeError, KeyError, TypeError, ValueError):
        # These are commonly caused by an upstream API or save-format change.
        return _report_failure("UPSTREAM")
    except Exception:
        return _report_failure("UNEXPECTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
