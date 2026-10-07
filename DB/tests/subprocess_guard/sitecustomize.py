"""Installs the test network guard in the Python processes the tests start.

tests/conftest.py puts this directory first on PYTHONPATH and describes the
allowed endpoints in PEOPLE_PUBS_TEST_NETWORK_GUARD, so every Python process
started with the test process's environment loads tests/network_guard.py
before it runs anything else. Outside a test run neither is set, and a
process that clears its environment (or runs with python -I, -E or -S) does
not load this file at all.

The guard fails closed. Python reports an exception raised here and then runs
the program anyway, so a guard that cannot be installed is reported, recorded
in the refusal log, and ends the process instead.
"""
import importlib.machinery
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_GUARD_FAILURE_STATUS = 70  # as network_guard.GUARD_FAILURE_STATUS


def _run(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    module = type(sys)(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, loader, origin=path)
    module.__file__, module.__loader__ = path, loader
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def _stop(exc):
    message = (
        f"the test network guard could not be installed: {type(exc).__name__}: {exc}"
        f" (process {os.getpid()})"
    )
    try:
        with open(os.environ["PEOPLE_PUBS_TEST_NETWORK_LOG"], "a", encoding="utf-8") as log:
            log.write(message + "\n")
    except (KeyError, OSError):
        pass
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    finally:
        os._exit(_GUARD_FAILURE_STATUS)


if "PEOPLE_PUBS_TEST_NETWORK_GUARD" in os.environ:
    try:
        _run("_test_network_guard", os.path.join(os.path.dirname(_HERE), "network_guard.py")).install_from_environment()
    except Exception as exc:
        _stop(exc)

# Hand over to a sitecustomize module that this one shadows, if there is one.
_spec = importlib.machinery.PathFinder.find_spec(
    "sitecustomize", [entry for entry in sys.path if os.path.abspath(entry or os.curdir) != _HERE]
)
if _spec is not None and _spec.origin and os.path.abspath(_spec.origin) != os.path.abspath(__file__):
    _run("_shadowed_sitecustomize", _spec.origin)
