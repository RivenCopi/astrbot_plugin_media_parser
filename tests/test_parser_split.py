import unittest

from core.config_manager import (
    ConfigManager,
    OUTPUT_MODE_ALL,
    OUTPUT_MODE_DISABLED,
)
from core.parser.platform import DouyinParser, TikTokParser


class ParserSplitTests(unittest.TestCase):

    def test_douyin_and_tiktok_have_independent_url_ownership(self):
        douyin = DouyinParser()
        tiktok = TikTokParser()

        douyin_url = "https://www.douyin.com/video/7123456789012345678"
        tiktok_url = "https://www.tiktok.com/@alice/video/7123456789012345678"

        self.assertEqual(douyin.name, "douyin")
        self.assertEqual(tiktok.name, "tiktok")
        self.assertTrue(douyin.can_parse(douyin_url))
        self.assertFalse(douyin.can_parse(tiktok_url))
        self.assertFalse(tiktok.can_parse(douyin_url))
        self.assertTrue(tiktok.can_parse(tiktok_url))

    def test_extract_links_are_platform_scoped_after_split(self):
        text = (
            "抖音 https://v.douyin.com/abc123/ "
            "TikTok https://vm.tiktok.com/ZMabc123/"
        )

        self.assertEqual(
            DouyinParser().extract_links(text),
            ["https://v.douyin.com/abc123/"],
        )
        self.assertEqual(
            TikTokParser().extract_links(text),
            ["https://vm.tiktok.com/ZMabc123/"],
        )

    def test_config_creates_tiktok_parser_independently(self):
        cfg = ConfigManager({
            "parsers": {
                "douyin": OUTPUT_MODE_DISABLED,
                "tiktok": OUTPUT_MODE_ALL,
            },
            "download": {
                "cache_dir": "",
            },
        })

        parser_names = [parser.name for parser in cfg.create_parsers()]

        self.assertIn("tiktok", parser_names)
        self.assertNotIn("douyin", parser_names)
        self.assertEqual(cfg.message.output_for_controller("tiktok"), (True, True))
        self.assertEqual(cfg.message.output_for_controller("douyin"), (False, False))

    def test_missing_tiktok_setting_uses_tiktok_default_not_douyin_setting(self):
        cfg = ConfigManager({
            "parsers": {
                "douyin": OUTPUT_MODE_DISABLED,
            },
            "download": {
                "cache_dir": "",
            },
        })

        self.assertEqual(cfg.message.output_for_controller("douyin"), (False, False))
        self.assertEqual(cfg.message.output_for_controller("tiktok"), (True, True))


if __name__ == "__main__":
    unittest.main()
