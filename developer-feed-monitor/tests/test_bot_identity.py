import unittest
from unittest.mock import patch
from deploy import configure_bot


class IdentityTests(unittest.TestCase):
    def test_preflight_reads_identity_without_mutations(self):
        with patch.object(configure_bot,'call',return_value={'username':configure_bot.DATA['username']}) as call:
            configure_bot.main(verify_only=True)
        call.assert_called_once_with('getMe')

    def test_wrong_bot_never_changes_profile(self):
        with patch.object(configure_bot,'call',return_value={'username':'another_bot'}) as call:
            with self.assertRaises(ValueError):
                configure_bot.main()
        call.assert_called_once_with('getMe')


if __name__=='__main__':unittest.main()
