"""Default QR display and automatic refresh without real platform access."""

import io
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from PIL import Image

from zhs.clients.course import ZHSClient
from zhs.common.utils import showImage


class LoginExperienceTests(unittest.TestCase):
    def image(self):
        stream = io.BytesIO()
        Image.new("RGB", (47, 47), "white").save(stream, format="PNG")
        return stream.getvalue()

    def test_popup_display_is_used_when_available(self):
        with (
            patch("zhs.common.utils.ImageShow.show", return_value=True) as display,
            patch("zhs.common.utils.terminalShowImage_tty") as terminal,
            patch("builtins.print"),
        ):
            showImage(self.image())
        display.assert_called_once()
        terminal.assert_not_called()

    def test_unavailable_popup_falls_back_to_terminal(self):
        for outcome in (False, OSError("no image viewer")):
            with (
                self.subTest(outcome=outcome),
                patch(
                    "zhs.common.utils.ImageShow.show",
                    side_effect=outcome if isinstance(outcome, Exception) else None,
                    return_value=False,
                ),
                patch("zhs.common.utils.terminalShowImage_tty") as terminal,
                patch("builtins.print"),
            ):
                image = self.image()
                showImage(image)
            terminal.assert_called_once_with(image)

    def test_explicit_terminal_display_does_not_launch_viewer(self):
        with (
            patch("zhs.common.utils.ImageShow.show") as display,
            patch("zhs.common.utils.terminalShowImage_unicode") as terminal,
            patch("builtins.print"),
        ):
            image = self.image()
            showImage(image, show_in_terminal=True, ensure_unicode=True)
        display.assert_not_called()
        terminal.assert_called_once_with(image)

    def test_default_qr_refreshes_after_more_than_three_expirations(self):
        self.qr_flow([2, 2, 2, 1])

    def test_canceled_scan_refreshes_instead_of_exiting(self):
        self.qr_flow([3, 1])

    def qr_flow(self, statuses):
        client = ZHSClient(proxies={})
        self.addCleanup(client.close)
        client._sessionReady = Mock()
        client.session.cookies.set("CASLOGC", json.dumps({"uuid": "test-account"}))
        response = MagicMock()
        response.__enter__.return_value = response
        payloads = []
        for status in statuses:
            payloads.extend(
                [
                    {"qrToken": "test-token", "img": ""},
                    {"status": status, "oncePassword": "test-ticket"},
                ]
            )
        response.json.side_effect = payloads
        client.session.get = Mock(return_value=response)
        callback = Mock()
        with patch("zhs.clients.course.sleep"), patch("builtins.print"):
            client._qrlogin(callback)
        self.assertEqual(callback.call_count, len(statuses))
        self.assertEqual(client.uuid, "test-account")
        self.assertEqual(client.session.get.call_count, 2 * len(statuses) + 1)
