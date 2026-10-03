"""Verified TLS with system roots plus certifi, including Python installs on macOS."""
import ssl
from functools import lru_cache


@lru_cache(maxsize=1)
def tls_context():
    import certifi
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def is_certificate_error(exc):
    reason = getattr(exc, 'reason', exc)
    return isinstance(reason, ssl.SSLCertVerificationError)


CERTIFICATE_HELP = ('SSL certificate verification failed. Run .sei_venv/bin/python -m pip install --upgrade certifi. '
                    'If this persists on a managed network, ask IT for its trusted CA setup; verification remains enabled.')
