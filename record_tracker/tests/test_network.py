from django.test import RequestFactory, SimpleTestCase, override_settings

from record_tracker.utils import (
    _ip_matches_allowed_range,
    get_client_ip,
    is_on_school_network,
)


@override_settings(
    ALLOWED_IP_RANGES=['127.0.0.1', '192.168.1.', '10.0.0.0/24'],
    WIFI_BYPASS=False,
    TRUST_X_FORWARDED_FOR=False,
)
class NetworkAccessTests(SimpleTestCase):
    def test_matches_exact_prefix_and_cidr_ranges(self):
        self.assertTrue(_ip_matches_allowed_range('127.0.0.1', ['127.0.0.1']))
        self.assertTrue(_ip_matches_allowed_range('192.168.1.42', ['192.168.1.']))
        self.assertTrue(_ip_matches_allowed_range('10.0.0.7', ['10.0.0.0/24']))

    def test_rejects_outside_and_invalid_addresses(self):
        self.assertFalse(_ip_matches_allowed_range('127.0.0.10', ['127.0.0.1']))
        self.assertFalse(_ip_matches_allowed_range('192.168.2.42', ['192.168.1.']))
        self.assertFalse(_ip_matches_allowed_range('10.0.1.7', ['10.0.0.0/24']))
        self.assertFalse(_ip_matches_allowed_range('not-an-ip', ['127.0.0.1']))

    def test_forwarded_header_is_ignored_by_default(self):
        request = RequestFactory().get('/', HTTP_X_FORWARDED_FOR='192.168.1.50')
        request.META['REMOTE_ADDR'] = '8.8.8.8'

        self.assertEqual(get_client_ip(request), '8.8.8.8')
        self.assertFalse(is_on_school_network(request))

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_forwarded_header_is_used_when_trusted(self):
        request = RequestFactory().get('/', HTTP_X_FORWARDED_FOR='192.168.1.50, 10.0.0.1')
        request.META['REMOTE_ADDR'] = '8.8.8.8'

        self.assertEqual(get_client_ip(request), '192.168.1.50')
        self.assertTrue(is_on_school_network(request))

    @override_settings(WIFI_BYPASS=True)
    def test_bypass_allows_any_address(self):
        request = RequestFactory().get('/')
        request.META['REMOTE_ADDR'] = '8.8.8.8'

        self.assertTrue(is_on_school_network(request))
