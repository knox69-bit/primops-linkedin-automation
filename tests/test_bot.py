import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch
from playwright.async_api import async_playwright
from bot import State, handle_thread, EXTRACT
from replies import generate

URL = 'https://www.linkedin.com/messaging/thread/mock/'
FIXTURE = '''<ul id="events">
<li class="msg-s-message-list__event" id="incoming-1">
<a class="msg-s-message-group__profile-link" href="/in/sender/"></a>
<div class="msg-s-event-listitem"><p class="msg-s-event-listitem__body">Hi</p></div></li>
</ul><div class="msg-form__contenteditable" contenteditable="true"></div>
<button class="msg-form__send-button" onclick="send()">Send</button>
<script>
function send(){
 const input=document.querySelector('[contenteditable]');
 const li=document.createElement('li');
 li.className='msg-s-message-list__event msg-s-message-list__event--own';
 li.id='outgoing-'+Date.now();
 const body=document.createElement('p'); body.className='msg-s-event-listitem__body';
 body.textContent=input.innerText; li.append(body); events.append(li); input.innerText='';
 window.sendCount=(window.sendCount||0)+1;
}
</script>'''

class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True, executable_path=os.getenv('TEST_CHROMIUM') or None)
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda route: route.fulfill(body=FIXTURE, content_type='text/html'))
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(self.temp.name + '/state.db')

    async def asyncTearDown(self):
        self.state.db.close()
        self.temp.cleanup()
        await self.browser.close()
        await self.p.stop()

    async def test_reply_confirmed_and_restart_dedup(self):
        with patch('bot.generate', return_value='Hey!'):
            await handle_thread(self.page, URL, 'owner', self.state)
            self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
            self.assertEqual(self.state.get(URL)[2], 'replied')
            # Route reloads original incoming content; persisted state must prevent duplicate.
            await handle_thread(self.page, URL, 'owner', self.state)
            self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_check_is_read_only(self):
        await handle_thread(self.page, URL, 'owner', self.state, check=True)
        self.assertIsNone(await self.page.evaluate('window.sendCount'))
        self.assertIsNone(self.state.get(URL))

    async def test_unknown_sender_never_sends(self):
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=FIXTURE.replace('href="/in/sender/"', 'href="#"'), content_type='text/html'))
        await handle_thread(self.page, URL, 'owner', self.state)
        self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_no_reply_to_own_message(self):
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=FIXTURE.replace('/in/sender/', '/in/owner/'), content_type='text/html'))
        await handle_thread(self.page, URL, 'owner', self.state)
        self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_attachment_and_grouped_message_extraction(self):
        await self.page.goto(URL)
        await self.page.evaluate('''() => {
            const li=document.createElement('li');li.className='msg-s-message-list__event';
            li.innerHTML='<div class="msg-s-event-listitem"><div class="msg-s-event-listitem__attachment"></div></div>';
            events.append(li);
        }''')
        data = await self.page.evaluate(EXTRACT, 'owner')
        self.assertEqual([x['role'] for x in data], ['user', 'user'])
        self.assertIn('Attachment', data[-1]['content'])

    async def test_uncertain_send_not_retried(self):
        self.state.set(URL, '', 'pending', 'uncertain')
        await handle_thread(self.page, URL, 'owner', self.state)
        self.assertIsNone(await self.page.evaluate('window.sendCount'))

class ReplyTests(unittest.TestCase):
    def test_fallback_does_not_claim_to_view_file_or_link(self):
        with patch.dict(os.environ, {'LLM_URL': '', 'LLM_MODEL': ''}):
            for message in ('[Attachment: image]', 'https://example.com', '🙂', ' ', 'x' * 30000):
                reply = generate([{'role': 'user', 'content': message}])
                self.assertTrue(0 < len(reply) <= 800)
            self.assertIn("can't view", generate([{'role': 'user', 'content': '[Attachment: image]'}]))

if __name__ == '__main__':
    unittest.main()
