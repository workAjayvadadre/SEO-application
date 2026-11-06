"""Consolidated market-research aggregator across Google, Reddit, Quora, YouTube, Instagram and Facebook."""

from .aggregator import ALL_PLATFORMS, Aggregator
from .config import Settings
from .models import Record, SearchRequest

__all__ = ["ALL_PLATFORMS", "Aggregator", "Record", "SearchRequest", "Settings", "use_system_certificates"]


def use_system_certificates() -> bool:
    """Trust the operating system's certificate store (Windows / macOS keychain) instead of only certifi.

    Needed on corporate networks that inspect HTTPS with their own root certificate: browsers and curl
    trust it through the OS store, Python does not. Call once at program start, before any connection.
    """
    try:
        import truststore
    except ImportError:
        return False
    truststore.inject_into_ssl()
    return True
__version__ = "1.0.0"
