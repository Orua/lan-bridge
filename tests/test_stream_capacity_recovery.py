import json
import unittest
from unittest.mock import patch

from code_cn_bridge.client import _iter_sse_data
from code_cn_bridge.protocol import StreamTranslator, _sanitize_tool_history


def payloads(events):
    return [json.loads(line[6:]) for event in events for line in event.splitlines() if line.startswith('data: {')]


def translate_call(name, arguments):
    translator = StreamTranslator(response_id='capacity', model='test', custom_tool_names={'apply_patch'})
    return payloads(translator.translate_chunk({'choices': [{'delta': {'tool_calls': [{
        'index': 0, 'id': 'original', 'function': {'name': name, 'arguments': arguments}
    }]}, 'finish_reason': 'tool_calls'}]}))


class StreamCapacityRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unterminated_line_is_rejected_while_reading_bounded_byte_chunks(self):
        class Response:
            consumed = 0

            async def aiter_lines(self):
                raise AssertionError('Unbounded line decoder must not be used for real byte streams')
                yield ''

            async def aiter_bytes(self, chunk_size=None):
                for _ in range(100):
                    self.consumed += 1
                    yield b'x' * 64

        response = Response()
        with patch('code_cn_bridge.client._SSE_LINE_MAX_BYTES', 128):
            with self.assertRaisesRegex(ValueError, 'SSE line exceeds'):
                _ = [event async for event in _iter_sse_data(response)]
        self.assertEqual(response.consumed, 3)

    async def test_split_utf8_and_crlf_are_preserved(self):
        class Response:
            async def aiter_bytes(self, chunk_size=None):
                for byte in 'data: {"text":"回执"}\r\n\r\ndata: [DONE]\r\n\r\n'.encode('utf-8'):
                    yield bytes([byte])

        events = [event async for event in _iter_sse_data(Response())]
        self.assertEqual(json.loads(events[0]), {'text': '回执'})
        self.assertEqual(events[1], '[DONE]')

    def test_complete_raw_patch_stays_compatible_but_cut_patch_is_not_dispatched(self):
        text = '*** Begin Patch\n*** Add File: demo.txt\n+ok\n*** End Patch\n'
        events = translate_call('apply_patch', text)
        completed = next(event for event in events if event['type'] == 'response.completed')
        self.assertEqual(completed['response']['output'][0]['input'], text)
        self.assertFalse(any(event['type'] == 'response.output_item.done' for event in translate_call('apply_patch', text[:-20])))

    def test_legal_three_mib_page_batch_is_not_rejected_by_bridge_argument_budget(self):
        events = translate_call('webcad_run', json.dumps({'data': 'x' * (3 * 1024 * 1024)}))
        self.assertTrue(any(event['type'] == 'response.completed' for event in events))

    def test_orphan_history_keeps_tool_provenance_without_inventing_reasoning(self):
        repaired = _sanitize_tool_history([{'role': 'tool', 'tool_call_id': 'prior', 'content': 'already wrote file'}])
        self.assertEqual(repaired[0]['reasoning_content'], '')
        self.assertEqual(repaired[1]['role'], 'tool')
        self.assertEqual(repaired[1]['content'], 'already wrote file')
