"""MailVerdict - AI-powered email management."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mail-verdict")
except PackageNotFoundError:  # a source tree with nothing installed
    __version__ = "0.0.0"
