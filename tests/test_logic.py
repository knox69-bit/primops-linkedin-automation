import os
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from unittest.mock import patch, AsyncMock, MagicMock
from bot import State, event_key, load_thread
from replies import generate

class LogicTests(unittest.TestCase):
    def test_provider_request_identifies_client_without_exposing_key(self):
        response = MagicMock()
        response.__enter__.return_value = io.StringIO('{"choices":[{"message":{"content":"Test reply"}}]}')
        with patch.dict(os.environ, {'LLM_URL':'https://provider.invalid/chat/completions', 'LLM_MODEL':'test', 'LLM_API_KEY':'secret-key'}):
            with patch('urllib.request.urlopen', return_value=response) as request:
                self.assertEqual(generate([{'role':'user','content':'Synthetic hello'}]), 'Test reply')
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_header('User-agent'), 'primops-reply-bot/1.0')
        self.assertEqual(sent.get_header('Accept'), 'application/json')

    def test_restart_state_preserved_and_content_not_stored(self):
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/state.db'
            state=State(path)
            state.set('private-thread-url', 'event-digest', 'reply-digest', 'uncertain')
            state.db.close()
            state=State(path)
            self.assertEqual(state.get('private-thread-url'), ('event-digest', 'reply-digest', 'uncertain'))
            self.assertNotIn('private-thread-url', str(state.db.execute('SELECT * FROM threads').fetchall()))
            state.db.close()

    def test_new_event_ids_distinguish_repeated_text(self):
        a=[{'id':'event-1','role':'user','content':'Hi'}]
        b=[{'id':'event-2','role':'user','content':'Hi'}]
        self.assertNotEqual(event_key(a),event_key(b))
        self.assertEqual(event_key(a),event_key(a))

    def test_fallback_attachment_link_empty_emoji_long(self):
        with patch.dict(os.environ, {'LLM_URL':'','LLM_MODEL':''}):
            for text in ('[Attachment: image]', 'https://example.com', '', '🙂', 'x'*50000):
                reply=generate([{'role':'user','content':text}])
                self.assertTrue(0<len(reply)<=800)

    def test_provider_failure_uses_fallback(self):
        with patch.dict(os.environ, {'LLM_URL':'https://provider.invalid/chat/completions','LLM_MODEL':'test'}):
            with patch('urllib.request.urlopen', side_effect=TimeoutError):
                self.assertTrue(generate([{'role':'user','content':'hello'}]))


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_render_continues_after_bounded_probe(self):
        page = MagicMock()
        page.url = 'https://www.linkedin.com/messaging/thread/test/'
        page.goto = AsyncMock()
        page.locator.return_value.first.wait_for = AsyncMock(side_effect=TimeoutError())
        page.locator.return_value.count = AsyncMock(return_value=1)
        page.wait_for_timeout = AsyncMock()
        with patch('bot.diagnose', new_callable=AsyncMock), redirect_stdout(io.StringIO()):
            await load_thread(page, page.url)

    async def test_event_timeout_logs_stage_not_exception_body(self):
        url = 'https://www.linkedin.com/messaging/thread/private-token/'
        page = MagicMock()
        page.url = url
        page.goto = AsyncMock()
        page.locator.return_value.first.wait_for = AsyncMock(side_effect=TimeoutError('secret message and name'))
        page.locator.return_value.count = AsyncMock(return_value=0)
        page.wait_for_timeout = AsyncMock()
        output = io.StringIO()
        with patch('bot.diagnose', new_callable=AsyncMock), redirect_stdout(output), self.assertRaises(TimeoutError):
            await load_thread(page, url)
        log = output.getvalue()
        self.assertIn('stage=message_events outcome=TimeoutError elapsed_ms=', log)
        self.assertIn('event_probe=target_absent_after_probe', log)
        for private in (url, 'private-token', 'secret message', 'name'):
            self.assertNotIn(private, log)
