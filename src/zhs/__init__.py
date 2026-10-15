"""ZHS Toolkit."""

__version__ = "0.2.0"


def __getattr__(name):
    if name == "ZHSClient":
        from zhs.clients.course import ZHSClient

        return ZHSClient
    raise AttributeError(name)
