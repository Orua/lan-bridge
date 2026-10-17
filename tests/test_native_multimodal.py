from unittest.mock import patch

from code_cn_bridge import server
from code_cn_bridge.adapters.deepseek import DeepSeekAdapter


def test_native_multimodal_model_bypasses_global_vision_and_observer():
    class Config:
        model_mapping = {"deepseek-flash": {"enabled": True, "is_multimodal": True}}
        model_slots = {"vision": {"enabled": True, "provider": "other", "target": "external-vision"}}
        _data = {}

    body = {"input": [{"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "https://example.com/image.png"}]}]}
    native = (DeepSeekAdapter(), "deepseek", "deepseek-flash", "test")
    with patch.object(server, "get_config", return_value=Config()), patch.object(server, "_get_adapter_for_model", return_value=native) as resolve:
        assert server._route_vision("deepseek-flash", body) == native
        resolve.assert_called_once_with("deepseek-flash")
        assert not server._should_delegate_vision_observation("deepseek-flash", body, Config())
