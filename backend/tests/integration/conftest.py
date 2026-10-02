"""Collect owned Redis subprocess tests only when explicitly enabled."""

import os

collect_ignore = (
    [] if os.environ.get("SEOKPAN_REDIS_TEST_SERVER") else ["test_departure_redis_lua.py"]
)
