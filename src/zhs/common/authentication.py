"""Passport login form encoding used by the platform's public login page."""

import json
from base64 import b64encode
from urllib.parse import quote


def passport_form(account, password, validate=""):
    # aes_util.js currently defines Encrypt(word) as btoa(encodeURI(word)).
    # encodeURI preserves reserved URI characters, unlike encodeURIComponent.
    payload = json.dumps(
        {"account": account, "password": password, "validate": validate},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    encoded = quote(payload, safe=";/?:@&=+$,#-_.!~*'()")
    return {"secretStr": b64encode(encoded.encode("ascii")).decode("ascii")}
