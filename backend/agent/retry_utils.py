"""
Retry utility for HTTP 429 (Too Many Requests) responses.
Uses exponential backoff with a cap.
"""
import time
import requests


def retry_on_429(fn, *args, max_retries: int = 4, base_delay: float = 2.0, **kwargs):
    """
    Call fn(*args, **kwargs). If it raises HTTPError 429, wait and retry.
    Non-429 errors are re-raised immediately without retry.

    Args:
        fn:          callable to invoke
        *args:       positional args forwarded to fn
        max_retries: total attempts before giving up (default 4)
        base_delay:  initial wait in seconds; doubles each retry (default 2s)
        **kwargs:    keyword args forwarded to fn
    """
    delay = base_delay
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                if attempt == max_retries:
                    raise
                print(f"  [retry] 429 rate-limited — waiting {delay:.0f}s (attempt {attempt}/{max_retries})")
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                raise
