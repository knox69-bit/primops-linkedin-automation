import os
import tempfile
import unittest
from unittest.mock import patch
from bot import State, event_key
from replies import generate

class LogicTests(unittest.TestCase):
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
