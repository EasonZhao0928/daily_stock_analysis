"""Feature flags default to safe, inert values in a fresh process."""

from __future__ import annotations

import unittest

from bot.platforms.wechat_ilink import FakeILinkServer, WeChatChannelAdapter, WeChatILinkClient
from src.config import Config


class FeatureFlagTest(unittest.TestCase):
    def test_personal_wechat_is_disabled_by_default(self):
        field = Config.__dataclass_fields__["wechat_channel_enabled"]
        self.assertIs(field.default, False)
        adapter = WeChatChannelAdapter(
            WeChatILinkClient(base_url="https://fake.invalid", token="token", transport=FakeILinkServer())
        )
        self.assertFalse(adapter.enabled)
        with self.assertRaises(RuntimeError):
            adapter.start(lambda _envelope: True)

    def test_integration_flags_are_disabled_by_default(self):
        fields = Config.__dataclass_fields__
        self.assertIs(fields["paper_auto_mode_enabled"].default, False)
        self.assertIs(fields["extended_market_data_enabled"].default, False)

    def test_every_declared_integration_flag_has_a_consumer(self):
        """A flag that gates nothing is worse than no flag (N1 regression)."""
        import subprocess

        for flag in ("paper_auto_mode_enabled", "extended_market_data_enabled"):
            hits = subprocess.run(
                ["grep", "-rl", flag, "--include=*.py", "src", "data_provider", "api", "bot"],
                capture_output=True,
                text=True,
            ).stdout.split()
            # config.py declares/parses it; at least one other module must read it.
            consumers = [path for path in hits if not path.endswith("src/config.py")]
            self.assertTrue(consumers, f"{flag} is declared but never consumed")

    def test_auto_paper_is_refused_unless_flag_is_enabled(self):
        """N1 regression: the highest-risk approval mode must be opt-in."""
        from src.paper_account.service import auto_paper_mode_enabled

        class _Cfg:
            paper_auto_mode_enabled = False

        class _CfgOn:
            paper_auto_mode_enabled = True

        self.assertFalse(auto_paper_mode_enabled(_Cfg()))
        self.assertTrue(auto_paper_mode_enabled(_CfgOn()))
        # A config object without the attribute must fail closed, not open.
        self.assertFalse(auto_paper_mode_enabled(object()))

    def test_extended_capabilities_are_refused_unless_flag_is_enabled(self):
        from data_provider.extended_capabilities import extended_market_data_enabled

        class _Cfg:
            extended_market_data_enabled = False

        class _CfgOn:
            extended_market_data_enabled = True

        self.assertFalse(extended_market_data_enabled(_Cfg()))
        self.assertTrue(extended_market_data_enabled(_CfgOn()))
        self.assertFalse(extended_market_data_enabled(object()))


if __name__ == "__main__":
    unittest.main()
