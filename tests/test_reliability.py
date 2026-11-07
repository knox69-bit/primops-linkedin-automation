import asyncio
import os
import threading
import unittest
from unittest.mock import patch
from playwright.async_api import Locator, TimeoutError as BrowserTimeout
import test_bot as fixtures
from bot import handle_thread, review_send, digest
from replies import generate


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BrowserTests.asyncSetUp
    asyncTearDown = fixtures.BrowserTests.asyncTearDown

    async def serve(self, html):
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))

    async def test_timed_burst_answers_both_questions_once(self):
        script = '''<script>
            ['actually two','do you work remotely?','and which time zone are you in?','thanks!'].forEach((text,index) => setTimeout(() => {
              const li=document.createElement('li'); li.className='msg-s-message-list__event';
              const p=document.createElement('p'); p.className='msg-s-event-listitem__body'; p.textContent=text; li.append(p); events.append(li);
            }, 600 + index*450));
        </script>'''
        await self.serve(fixtures.FIXTURE.replace('>Hi</p>', '>Quick question</p>') + script)
        with patch.dict(os.environ, {'LLM_URL':'','LLM_MODEL':''}), patch('bot.generate', wraps=generate) as generate_call:
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
        self.assertEqual(generate_call.call_count, 1)
        received = '\n'.join(e['content'] for e in generate_call.call_args.args[0])
        self.assertIn('do you work remotely?', received)
        self.assertIn('which time zone', received)
        self.assertIn('thanks!', received)
        self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
        outgoing = await self.page.locator('.msg-s-event-listitem__body').last.inner_text()
        self.assertIn('remote', outgoing)
        self.assertIn('time zone', outgoing)

    async def test_new_message_during_generation_is_answered_same_sweep(self):
        started, arrived = threading.Event(), threading.Event()
        async def arrive():
            self.assertTrue(await asyncio.to_thread(started.wait, 10))
            await self.page.evaluate('''() => {
            const li=document.createElement('li'); li.className='msg-s-message-list__event';
            li.innerHTML='<p class="msg-s-event-listitem__body">Another question?</p>'; events.append(li);
            }''')
            arrived.set()
        calls = 0
        def slow(history):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                if not arrived.wait(10):
                    raise RuntimeError('Synthetic arrival did not complete')
            return 'Reply to the complete burst'
        arrival = asyncio.create_task(arrive())
        with patch('bot.generate', side_effect=slow) as generator:
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
        await arrival
        self.assertEqual(generator.call_count, 2)
        self.assertEqual(generator.call_args.args[0][-1]['content'], 'Another question?')
        self.assertEqual(await self.page.evaluate('window.sendCount'), 1)

    async def test_identity_change_during_generation_does_not_reply_to_self(self):
        await self.serve(fixtures.FIXTURE.replace('id="incoming-1"', 'id="incoming-1" data-event-urn="stable-event"'))
        loop = asyncio.get_running_loop()
        def changed_identity(history):
            future = asyncio.run_coroutine_threadsafe(
                self.page.evaluate('''() => events.querySelector('a').setAttribute('href','/in/owner/')'''), loop)
            future.result(10)
            return 'Must not be sent'
        with patch('bot.generate', side_effect=changed_identity):
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
        self.assertIsNone(await self.page.evaluate('window.sendCount'))
        self.assertIsNone(self.state.get(fixtures.URL))

    async def test_long_post_then_url_then_literal_markup_keep_responding(self):
        messages = [
            'Thanks for connecting. ' + ('We build operations tools with TypeScript and Python and maintain recruiting automations.\n\n' * 85) + 'Which part of this role would you take on first, and why?',
            'Could you look at this posting? https://www.example.com/careers/engineering/automation-developer?utm_source=linkedin&utm_medium=message&position=48213&lang=en',
            'My colleague said "it\'s fine" then wrote: <b>bold</b> {name}\nWhat do you make of pneumonoultramicroscopicsilicovolcanoconiosisantidisestablis?'
        ]
        await self.page.goto(fixtures.URL)
        await self.page.evaluate('events.innerHTML=""')
        for index, message in enumerate(messages):
            await self.page.evaluate('''({message,index}) => {
                const li=document.createElement('li'); li.className='msg-s-message-list__event'; li.dataset.eventUrn='incoming-'+index;
                li.innerHTML='<a href="/in/sender/"><span class="msg-s-message-group__name">Sender</span></a><p class="msg-s-event-listitem__body"></p>';
                li.querySelector('p').textContent=message; events.append(li);
            }''', {'message':message,'index':index})
            html = await self.page.content()
            await self.serve(html)
            with patch.dict(os.environ, {'LLM_URL':'','LLM_MODEL':''}), patch('bot.generate', wraps=generate) as generator:
                await handle_thread(self.page, fixtures.URL, 'owner', self.state)
                self.assertEqual(generator.call_count, 1)
                self.assertEqual(' '.join(generator.call_args.args[0][-1]['content'].split()), ' '.join(message.split()))
            self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
            self.assertEqual(self.state.get(fixtures.URL)[2], 'replied')
            outgoing = await self.page.locator('.msg-s-event-listitem__body').last.inner_text()
            self.assertNotEqual(outgoing, "You're welcome!")
            if index == 1:
                self.assertIn("can't open", outgoing)
            await self.serve(await self.page.content())
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
            self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_timeout_before_input_recovers_and_sends_once(self):
        async def blocked(button, *args, **kwargs):
            raise BrowserTimeout('Private page content must never be logged')
        with patch('bot.generate', return_value='Generated reply'), patch.object(Locator, 'click', blocked):
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
        self.assertEqual(self.state.get(fixtures.URL)[2], 'retry')
        self.assertIsNone(await self.page.evaluate('window.sendCount'))
        await self.serve(await self.page.content())
        with patch('bot.generate', return_value='Generated reply'):
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
        self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
        self.assertEqual(self.state.get(fixtures.URL)[2], 'replied')

    async def test_timeout_after_dispatch_never_blindly_retries(self):
        original = Locator.click
        async def delivered_then_timeout(button, *args, **kwargs):
            await original(button, *args, **kwargs)
            raise BrowserTimeout('synthetic timeout after dispatch')
        with patch('bot.generate', return_value='Generated reply'), patch.object(Locator, 'click', delivered_then_timeout), self.assertRaises(BrowserTimeout):
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
        self.assertEqual(self.state.get(fixtures.URL)[2], 'uncertain')
        self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
        await self.serve(await self.page.content())
        with patch('bot.generate') as generator:
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
        generator.assert_not_called()
        self.assertNotEqual(self.state.get(fixtures.URL)[2], 'uncertain')

    async def test_lost_document_during_click_keeps_uncertain_hold(self):
        async def navigated_then_timeout(button, *args, **kwargs):
            await self.page.goto(fixtures.URL)
            raise BrowserTimeout('document replaced before observation')
        with patch('bot.generate', return_value='Generated reply'), patch.object(Locator, 'click', navigated_then_timeout), self.assertRaises(BrowserTimeout):
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
        self.assertEqual(self.state.get(fixtures.URL)[2], 'uncertain')
        with patch('bot.generate') as generator:
            await handle_thread(self.page, fixtures.URL, 'owner', self.state)
        generator.assert_not_called()
        self.assertEqual(self.state.get(fixtures.URL)[2], 'uncertain')

    async def test_review_requires_explicit_confirmation_of_non_delivery(self):
        self.state.set(fixtures.URL, 'previous-seen', digest('Pending reply'), 'uncertain')
        with patch('builtins.input', return_value=''):
            await review_send(self.page, fixtures.URL, 'owner', self.state)
        self.assertEqual(self.state.get(fixtures.URL)[2], 'uncertain')
        with patch('builtins.input', return_value='NOT SENT'):
            await review_send(self.page, fixtures.URL, 'owner', self.state)
        self.assertEqual(self.state.get(fixtures.URL), ('previous-seen', digest('Pending reply'), 'retry'))
        self.assertIsNone(await self.page.evaluate('window.sendCount'))
