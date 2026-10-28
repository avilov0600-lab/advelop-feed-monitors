import unittest

from yandex_cloud.bootstrap_telegram import recent_chats


class TelegramBootstrapTests(unittest.TestCase):
    def test_recent_chats_deduplicates_and_keeps_latest_label(self):
        updates = [
            {"message": {"chat": {"id": 7, "first_name": "Alex"}}},
            {"message": {"chat": {"id": 7, "first_name": "Alexey"}}},
            {"channel_post": {"chat": {"id": -10, "title": "Feed QA"}}},
        ]
        self.assertEqual(
            recent_chats(updates),
            [{"id": "7", "label": "Alexey"}, {"id": "-10", "label": "Feed QA"}],
        )


if __name__ == "__main__":
    unittest.main()
