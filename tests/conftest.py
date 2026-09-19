import logging
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Mirrors viva.cli's own _NOISY_LOGGER_NAMES -- duplicated as a plain
# tuple rather than imported, so collecting this file doesn't force
# every test in the suite (most of which have nothing to do with the
# CLI) to import viva.cli and its typer/rich dependencies just for this
# fixture. test_cli_logging.py asserts the two stay in sync.
_NOISY_LOGGER_NAMES = ("httpx", "httpcore", "viva.questiongen.retrieval")


def _reset_noisy_loggers():
    for name in _NOISY_LOGGER_NAMES:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def _reset_cli_configured_loggers():
    # Any test anywhere in this suite that calls runner.invoke(app, ...)
    # (11 test files do, as of this writing -- test_cli_*.py plus
    # anywhere else that exercises the CLI) runs viva.cli's main()
    # callback, which calls _configure_logging() -- Phase 17's
    # httpx/httpcore file-redirect, extended in Phase 16's follow-up to
    # also cover viva.questiongen.retrieval. That function mutates
    # logging.getLogger(name).propagate/handlers on real, process-wide
    # singleton logger objects, not anything scoped to the test process
    # or the file that happened to trigger it.
    #
    # A real bug shipped from scoping this reset to test_cli_logging.py
    # alone (see its own history): that file's tests correctly isolated
    # from *each other*, but any of the other 10 CLI-invoking test
    # files running afterward -- none of which have any reason to know
    # this global state exists -- could just as easily leave
    # propagate=False behind for whichever test ran next in the same
    # process, wherever in the suite that landed. Only a suite-wide
    # autouse fixture actually closes that gap; a single file's fixture
    # only ever covers leaks it causes to itself.
    _reset_noisy_loggers()
    yield
    _reset_noisy_loggers()
