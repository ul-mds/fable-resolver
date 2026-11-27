import hashlib
from itertools import islice
from typing import Iterable, Iterator, TypeVar


T = TypeVar("T")


def batch(iterable: Iterable[T], n: int = 500) -> Iterator[list[T]]:
    it = iter(iterable)
    while b := list(islice(it, n)):
        yield b


def session_hash(session_id: str, length: int = 8) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:length]
