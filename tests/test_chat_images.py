import asyncio
import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from code_cn_bridge import server
from code_cn_bridge.generated_images import image_output_dir
from tests.test_workbuddy_responses_bridge import _NativeConfig


# A real, decodable 1x1 PNG; deliberately never sent to an upstream provider.
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1EAAAAASUVORK5CYII="


class ChatImageTests(unittest.TestCase):
    def setUp(self):
        from code_cn_bridge.access_control import ANONYMOUS_PRINCIPAL
        auth = patch('code_cn_bridge.middleware.authenticate_bridge_headers', return_value=ANONYMOUS_PRINCIPAL)
        auth.start()
        self.addCleanup(auth.stop)

    def test_native_chat_delivers_saved_image_for_stream_and_json(self):
        for stream in (True, False):
            for final_only in (False, True):
                with self.subTest(stream=stream, final_only=final_only), tempfile.TemporaryDirectory() as output:
                    image = {"type": "image_generation_call", "id": "ig_test", "status": "completed", "result": PNG}
                    observed = {}

                    async def fake_native(request, payload, *args, **kwargs):
                        observed.update(payload)

                        async def events():
                            if not final_only:
                                yield 'data: ' + json.dumps({"type": "response.output_item.done", "output_index": 0, "item": image}) + '\n\n'
                            yield 'data: ' + json.dumps({"type": "response.completed", "response": {"status": "completed", "output": [image]}}) + '\n\n'
                        return StreamingResponse(events(), media_type="text/event-stream")

                    with patch.dict(os.environ, {"LAN_BRIDGE_IMAGE_OUTPUT_DIR": output}), patch.object(server, "get_config", return_value=_NativeConfig()), patch("code_cn_bridge.provider_proxy.proxy_native_responses", side_effect=fake_native):
                        client = TestClient(server.create_app())
                        result = client.post('/v1/chat/completions', json={"model": "gpt-5.6-terra", "stream": stream, "messages": [{"role": "user", "content": "生成一个苹果的图片，用我的自定义模型！"}]})
                        self.assertEqual(result.status_code, 200)
                        self.assertEqual(observed['tool_choice'], {"type": "image_generation"})
                        self.assertIn({"type": "image_generation"}, observed['tools'])
                        if stream:
                            frames = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: {')]
                            content = ''.join(f.get('choices', [{}])[0].get('delta', {}).get('content', '') for f in frames)
                        else:
                            content = result.json()['choices'][0]['message']['content']
                        self.assertEqual(content.count('![生成的图片]'), 1)
                        files = list(image_output_dir().iterdir())
                        self.assertEqual(len(files), 1)
                        self.assertIn(files[0].name, content)
                        download = client.get('/generated-images/' + files[0].name)
                        self.assertEqual(download.content, base64.b64decode(PNG))
                        self.assertEqual(download.headers['content-type'], 'image/png')
                        self.assertEqual(client.get('/generated-images/not-an-image.png').status_code, 404)

    def test_media_preparation_respects_none_and_other_tool_selection(self):
        route = server.resolve_route(_NativeConfig(), 'gpt-5.6-terra')
        for choice in ('none', {'type': 'function', 'name': 'Read'}):
            payload = {"input": [{"type": "message", "role": "user", "content": "生成苹果图片"}], "tool_choice": choice}
            server._prepare_native_chat_media(payload, route)
            self.assertEqual(payload['tool_choice'], choice)
        payload = {"input": [{"type": "message", "role": "user", "content": "生成苹果图片"}, {"type": "function_call_output", "call_id": "1", "output": "done"}]}
        server._prepare_native_chat_media(payload, route)
        self.assertNotIn('tool_choice', payload)

    def test_failed_image_is_an_error_not_an_empty_success(self):
        from code_cn_bridge.protocol_adapters import ResponsesStreamToChat
        async def events():
            yield 'data: ' + json.dumps({"type": "response.completed", "response": {"output": [{"type": "image_generation_call", "status": "failed", "result": None}]}}) + '\n\n'
        async def run():
            from code_cn_bridge.generated_images import render_generated_image
            translator = ResponsesStreamToChat('test', 'test', image_renderer=lambda item: render_generated_image(item, 'http://test'))
            chunks = [chunk async for chunk in translator.convert(events())]
            self.assertTrue(translator.failed)
            self.assertFalse(translator.completed)
            self.assertIn('"error"', ''.join(chunks))
        asyncio.run(run())
