"""Generic retry helper used for flaky I/O."""

import time


def retry_with_backoff(fn, *, attempts=3, base_delay=0.1, retry_on=(Exception,)):
    """Call ``fn`` until it succeeds, sleeping base_delay * 2^n between tries.

    Re-raises the last error once ``attempts`` are exhausted.
    """
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except retry_on as error:  # noqa: PERF203
            last_error = error
            if attempt < attempts - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last_error
