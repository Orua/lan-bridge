import unittest
from unittest.mock import patch

from code_cn_bridge import server


class _FakeConfig:
    model_slots = {
        "vision": {"provider": "qwen", "target": "new-vision", "enabled": True},
    }
    model_mapping = {
        "gpt-5-code": {
            "provider": "deepseek",
            "target": "text-model",
            "enabled": True,
            "vision_alias": "gpt-5",
        },
        "gpt-5": {
            "provider": "qwen",
            "target": "new-vision",
            "enabled": True,
            "is_multimodal": True,
        },
    }
    vision_routing = {"enabled": True, "provider": "qwen", "model": "old-global-vision"}

    @staticmethod
    def _find_provider_for_target(target):
        return "qwen"


class VisionSlotRoutingTests(unittest.TestCase):
    def test_image_request_uses_vision_slot_even_when_request_alias_differs(self):
        body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "data:image/png;base64,x"}],
                }
            ]
        }

        with (
            patch.object(server, "get_config", return_value=_FakeConfig()),
            patch.object(server, "_resolve_adapter", side_effect=lambda provider, target: (None, provider, target, "")),
        ):
            _, provider, target, _ = server._route_vision("gpt-5.5", body)

        self.assertEqual((provider, target), ("qwen", "new-vision"))


if __name__ == "__main__":
    unittest.main()
