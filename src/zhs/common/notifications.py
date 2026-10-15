"""
Author: Vincent Young, jiajiu123
Date: 2023-03-30 00:15:22
LastEditors: Vincent Young
LastEditTime: 2023-03-30 02:40:33
Telegram: https://t.me/missuo(Vincent Young)

Copyright © 2023 by Vincent, All Rights Reserved.
"""

import requests


def pushpluser(title: str, content, token: str) -> None:
    with requests.get(
        "http://www.pushplus.plus/send",
        params={"token": token, "title": title, "content": str(content)},
        timeout=(5, 10),
    ) as response:
        response.raise_for_status()


def barkpusher(title: str, content, token: str) -> None:
    from urllib.parse import quote

    with requests.get(
        f"{token.rstrip('/')}/{quote(title, safe='')}/{quote(str(content), safe='')}",
        timeout=(5, 10),
    ) as response:
        response.raise_for_status()
