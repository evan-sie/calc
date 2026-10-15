"""Offline regression checks. Run with python3 -m unittest discover -s tests -v."""

import base64
import collections
import curses
import io
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import casio_ai as app


def event(kind, **kwargs):
    return NS(type=kind, **kwargs)


class ChatTests(unittest.TestCase):
    def setUp(self):
        app.chat = app.ChatState()
        app.chat_history = app.chat.history
        app.pending_capture = app.PendingCapture()
        app.header_lines.clear()
        app.STYLES = collections.defaultdict(int)
        app.throbber_frame = app.throbber_tick = 0
        self.calls = []
        self.failures = 0
        self.bad_stream = None
        self.client = NS(responses=NS(create=self.create_response))
        patches = [
            patch.object(app, 'OpenAI', return_value=self.client),
            patch.object(app, 'OPENAI_API_KEY', 'offline-test-placeholder'),
            patch.object(app, 'check_viewfinder_alive', return_value=False),
            patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden in tests')),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def create_response(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            self.failures -= 1
            raise ConnectionError('simulated connection failure')
        if self.bad_stream is not None:
            return iter(self.bad_stream)
        return iter([
            event('response.reasoning_summary_text.delta', delta='**Checking units** prose'),
            event('response.output_text.delta', delta='Context or answer'),
            event('response.completed', response=NS(id=f'resp_{len(self.calls)}', usage=NS(input_tokens=10))),
        ])

    def pump(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            worker = app.chat.thread or app.pending_capture.thread
            if worker:
                worker.join(0.02)
            app.tick_request(70)
            if app.chat.status == 'idle' or app.chat.awaiting_choice:
                return
        self.fail('worker did not finish')

    def test_notes_photo_and_followup_share_history(self):
        app.send_class_context('MECH4310', 'Use velocity VX.', 70)
        self.pump()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'photo.jpg'
            image.write_bytes(b'offline-image-payload')

            def capture(cap):
                cap.path = str(image)

            with patch.object(app, '_run_capture', side_effect=capture):
                self.assertTrue(app.start_capture(70))
                self.pump()
        self.assertTrue(app.send_message('Explain the previous answer.', 70))
        self.pump()
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn('previous_response_id', self.calls[0])
        self.assertEqual(self.calls[1]['previous_response_id'], 'resp_1')
        self.assertEqual(self.calls[2]['previous_response_id'], 'resp_2')
        notes = self.calls[0]['input'][0]['content'][0]['text']
        self.assertIn('MECH4310', notes)
        self.assertIn('Use velocity VX.', notes)
        photo = self.calls[1]['input'][0]['content'][0]
        self.assertEqual(photo['type'], 'input_image')
        self.assertEqual(base64.b64decode(photo['image_url'].split(',')[1]), b'offline-image-payload')
        for call in self.calls:
            self.assertEqual(call['model'], 'gpt-6.1-sol')
            self.assertEqual(call['reasoning'], {'effort': 'high', 'summary': 'detailed'})
            self.assertEqual(call['instructions'], app.F1_PROMPT)
            self.assertTrue(call['stream'])
        self.assertEqual(app.chat.prev_response_id, 'resp_3')
        self.assertIsNotNone(app.chat.last_usage)
        self.assertFalse(any('Checking units' in text for line in app.chat_history for text, _ in line))

    def test_retry_dismiss_keeps_history_and_allows_next_question(self):
        app.send_message('First question', 70)
        self.pump()
        self.failures = 2
        app.send_message('Fails twice', 70)
        self.pump()
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(app.chat.awaiting_choice)
        self.assertEqual(app.chat.prev_response_id, 'resp_1')
        self.assertTrue(app.handle_retry_choice(curses.KEY_F3, 70))
        self.assertFalse(app.chat.awaiting_choice)
        self.assertEqual(app.chat.prev_response_id, 'resp_1')
        self.assertTrue(app.send_message('New question', 70))
        self.pump()
        self.assertEqual(self.calls[-1]['previous_response_id'], 'resp_1')
        self.assertEqual(app.chat.status, 'idle')

    def test_manual_retry_uses_failed_turn(self):
        self.failures = 2
        app.send_message('Keep this question', 70)
        self.pump()
        self.assertTrue(app.handle_retry_choice(10, 70))
        self.pump()
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(c['input'][0]['content'][0]['text'] == 'Keep this question' for c in self.calls))
        self.assertEqual(app.chat.status, 'idle')

    def test_incomplete_or_broken_stream_does_not_advance_history(self):
        for ending in (None, 'response.failed', 'response.incomplete', 'error'):
            with self.subTest(ending=ending):
                app.chat.prev_response_id = 'last_success'
                self.bad_stream = [event('response.output_text.delta', delta='Partial answer')]
                if ending:
                    self.bad_stream.append(event(ending))
                with self.assertRaises(RuntimeError):
                    app._openai_request(app.chat, False, None, 'Question')
                self.assertEqual(app.chat.prev_response_id, 'last_success')

    def test_thinking_is_one_line_replaces_header_and_reserves_dots(self):
        app.chat.step = 'WAITING'
        app.chat.wait_start = time.time()
        app.note_thought(app.chat, 'Prose without a heading')
        self.assertEqual(app.chat.thought_label, '')
        app.note_thought(app.chat, '**Reading the dia')
        self.assertEqual(app.chat.thought_label, '')
        app.note_thought(app.chat, 'gram** more prose')
        self.assertEqual(app.chat.thought_label, 'Reading the diagram')
        app.note_thought(app.chat, '**Checking units** more prose')
        app.throbber_frame = 3
        block = app.render_live_block(app.chat, 30)
        self.assertEqual(len(block), 1)
        text, _ = block[0][0]
        self.assertIn('Checking units', text)
        self.assertTrue(text.endswith('...'))
        self.assertLessEqual(len(text), 29)
        self.assertNotIn('prose', text)

    def test_camera_failure_does_not_send_api_request(self):
        app.chat.prev_response_id = 'notes_context'

        def capture(cap):
            cap.error = 'camera timeout'

        with patch.object(app, '_run_capture', side_effect=capture):
            app.start_capture(70)
            self.pump()
        self.assertEqual(self.calls, [])
        self.assertEqual(app.chat.status, 'idle')
        self.assertEqual(app.chat.prev_response_id, 'notes_context')
        self.assertTrue(any('Cam Fail:' in text for line in app.chat_history for text, _ in line))

    def test_busy_chat_rejects_another_capture_or_message(self):
        app.chat.status = 'working'
        self.assertFalse(app.start_capture(70))
        self.assertFalse(app.send_message('Another question', 70))
        self.assertFalse(app.send_class_context('MECH3381', 'Notes', 70))
        self.assertEqual(self.calls, [])

    def test_finished_worker_must_be_reaped_before_next_turn(self):
        app.chat.status = 'done'
        app.chat.thread = NS(is_alive=lambda: False)
        self.assertFalse(app.send_message('Too early', 70))
        self.assertFalse(app.start_capture(70))

    def test_toolbar_has_one_gpt_health_indicator_and_clock(self):
        def read_stat(path, *args, **kwargs):
            if path == '/sys/class/thermal/thermal_zone0/temp':
                return io.StringIO('40000')
            if path == '/proc/meminfo':
                return io.StringIO('MemTotal: 360000 kB\nMemFree: 100000 kB\nMemAvailable: 200000 kB\n')
            raise AssertionError(path)

        def wifi(command, **kwargs):
            return NS(stdout='CampusWiFi' if command[0] == 'iwgetid' else 'signal: -60 dBm')

        app.STYLES.update(normal=1, diag_ok=2, diag_warn=4, diag_crit=8)
        for status, expected in [('idle', 2), ('working', 4), ('error', 8)]:
            with self.subTest(status=status), patch('builtins.open', side_effect=read_stat), \
                    patch.object(app.subprocess, 'run', side_effect=wifi):
                app.chat.status = status
                segments = app.get_diagnostics_styled()
                text = ''.join(t for t, _ in segments)
                self.assertEqual(text.count('GPT'), 1)
                self.assertNotIn('GEMINI', text)
                self.assertIn('CampusWiFi -60', text)
                self.assertRegex(text, r'\d{2}:\d{2}$')
                style = next(s for t, s in segments if t == 'GPT')
                self.assertEqual(style, expected | curses.A_BOLD)


if __name__ == '__main__':
    unittest.main()
