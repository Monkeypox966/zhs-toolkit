"""Session setup and JSON API requests with bounded waits."""

import json

import requests
from requests.adapters import HTTPAdapter, Retry

from zhs.common.control import check_stop
from zhs.common.models import ObjDict


def create_session(retries=3):
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    )
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def api_query(session, url, data, method="POST", content_type="form", proxies=None):
    check_stop()
    method = method.upper()
    kwargs = {"proxies": proxies, "timeout": (10, 30)}
    if method == "POST":
        if content_type == "json":
            kwargs["json"] = json.loads(data) if isinstance(data, str) else data
        else:
            kwargs["data"] = data
    elif method == "GET":
        kwargs["params"] = data
    else:
        raise ValueError(f"Unsupported method: {method}")
    with session.request(method, url, **kwargs) as response:
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise ValueError(f"API returned invalid JSON: {url}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"API returned {type(payload).__name__}, expected an object: {url}")
    return ObjDict(payload)
