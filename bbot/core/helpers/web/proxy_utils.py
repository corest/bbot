import ipaddress
import fnmatch
from urllib.parse import urlparse


def should_bypass_proxy(url, exclusion_list):
    """Check if a URL should bypass the proxy based on exclusion patterns.

    Follows standard NO_PROXY conventions:
    - Exact hostname: "elasticsearch.internal"
    - Domain suffix: ".internal.corp" (matches any subdomain)
    - Wildcard: "*.internal.corp"
    - CIDR: "10.0.0.0/8" (only for IP-literal URLs)
    - IP: "192.168.1.100"
    - "*": bypass all

    Args:
        url: The URL string to check.
        exclusion_list: List of exclusion patterns.

    Returns:
        True if the URL should bypass the proxy, False otherwise.
    """
    if not exclusion_list:
        return False

    parsed = urlparse(str(url))
    hostname = (parsed.hostname or "").lower().strip(".")
    if not hostname:
        return False

    # check if hostname is an IP address
    host_ip = None
    try:
        host_ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass

    for pattern in exclusion_list:
        pattern = str(pattern).strip().lower().strip(".")
        if not pattern:
            continue

        # wildcard all
        if pattern == "*":
            return True

        # IP/CIDR matching when URL host is an IP
        if host_ip is not None:
            try:
                network = ipaddress.ip_network(pattern, strict=False)
                if host_ip in network:
                    return True
                continue
            except ValueError:
                pass

        # domain suffix match (.example.com)
        if pattern.startswith("."):
            if hostname == pattern[1:] or hostname.endswith(pattern):
                return True
            continue

        # wildcard match (*.example.com)
        if "*" in pattern:
            if fnmatch.fnmatch(hostname, pattern):
                return True
            continue

        # exact match
        if hostname == pattern:
            return True

        # bare domain as suffix (NO_PROXY convention: "example.com" also matches "sub.example.com")
        if hostname.endswith("." + pattern):
            return True

    return False
