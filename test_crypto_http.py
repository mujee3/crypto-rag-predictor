import os
import ssl
import unittest
import urllib.error
from unittest.mock import patch
from crypto_http import tls_context,is_certificate_error,CERTIFICATE_HELP


class TLSTests(unittest.TestCase):
    def tearDown(self):
        tls_context.cache_clear()

    def test_bundle_works_without_default_ca_files(self):
        tls_context.cache_clear()
        with patch.dict(os.environ,{'SSL_CERT_FILE':'/nonexistent-ca-file','SSL_CERT_DIR':'/nonexistent-ca-directory'}):
            ctx=tls_context()
        self.assertEqual(ctx.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertGreater(ctx.cert_store_stats()['x509_ca'],0)

    def test_wrapped_certificate_error_is_identified(self):
        error=urllib.error.URLError(ssl.SSLCertVerificationError(1,'certificate verify failed'))
        self.assertTrue(is_certificate_error(error))
        from binance_depth import Binance
        with patch('urllib.request.urlopen',side_effect=error) as mocked:
            with self.assertRaisesRegex(RuntimeError,'SSL certificate verification failed'):
                Binance().get('depth',symbol='SEIUSDT')
        self.assertEqual(mocked.call_args.kwargs['context'].verify_mode,ssl.CERT_REQUIRED)


if __name__=='__main__':unittest.main()
