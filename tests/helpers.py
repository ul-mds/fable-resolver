import datetime
import string
import random
from typing import Any, Callable
import time

from httpx2 import Response


def random_string(charset=string.ascii_letters, length: int = 20):
    assert length > 0
    assert len(charset) > 0

    return "".join([random.choice(charset) for _ in range(length)])


def random_identity():
    from mosaic_client.epix import Identity

    return Identity(
        first_name=random_string(),
        last_name=random_string(),
        birth_date=datetime.datetime.now(tz=datetime.UTC),
        gender=random.choice(["m", "f"]),
    )


def detail_of(r: Response):
    return r.json()["detail"]


def assert_eventually(func: Callable[[], Any], max_retries: int = 10, delay_millis: int = 1_000):
    e = None
    for _ in range(max_retries):
        try:
            func()
            return  # Return if everything went fine.
        except AssertionError as err:
            e = err

        time.sleep(delay_millis / 1_000)

    assert False, f"Callback failed after {max_retries} retries: {e}"
