import unittest

from src.common.dingtalk import redact_dingtalk_error


class DingTalkTransportTests(unittest.TestCase):
    def test_errors_hide_signed_url_credentials(self):
        error = RuntimeError("failed /robot/send?access_token=token123&timestamp=123456&sign=secret%2F (ConnectTimeout)")
        result = redact_dingtalk_error(error)
        for value in ('token123', '123456', 'secret%2F'):
            self.assertNotIn(value, result)
        self.assertIn('ConnectTimeout', result)
