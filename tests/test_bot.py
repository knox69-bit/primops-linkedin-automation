import asyncio
import os
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from unittest.mock import patch
from playwright.async_api import async_playwright
from bot import State, handle_thread, EXTRACT, SENDER_PROBE, digest, event_key
from replies import generate
from identity import Identity, PROFILE_PROBE

URL = 'https://www.linkedin.com/messaging/thread/mock/'
FIXTURE = '''<ul id="events">
<li class="msg-s-message-list__event" id="incoming-1">
<span><a href="/in/sender/"><span class="msg-s-message-group__name">Sender</span></a></span>
<div class="msg-s-event-listitem"><p class="msg-s-event-listitem__body">Hi</p></div></li>
</ul><div class="msg-form__contenteditable" contenteditable="true"></div>
<button class="msg-form__send-button" onclick="send()">Send</button>
<script>
function send(){
 const input=document.querySelector('[contenteditable]');
 const li=document.createElement('li');
 li.className='msg-s-message-list__event';
 li.innerHTML='<span><a href="/in/owner/"><span class="msg-s-message-group__name">Owner</span></a></span>';
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
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
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

    async def test_sender_probe_does_not_return_private_dom_values(self):
        await self.page.goto(URL)
        await self.page.evaluate('''() => {
            events.firstElementChild.innerHTML = '<div data-member-id="secret-member"><a href="/in/secret-profile/"><span class="msg-s-message-group__name" aria-label="Secret Name">Secret Name</span></a></div><p class="msg-s-event-listitem__body">Secret Message <a href="/in/body-profile/">link</a></p>';
        }''')
        result = await self.page.evaluate(SENDER_PROBE)
        self.assertTrue(result[0]['header_present'])
        self.assertTrue(result[0]['header_ancestry'][1]['profile_href'])
        self.assertTrue(result[0]['header_ancestry'][2]['has_member_id'])
        self.assertEqual(result[0]['event']['profile_descendants'], 1)
        for private in ('secret', 'Secret', '/in/', 'body-profile'):
            self.assertNotIn(private, str(result))
        matched = await self.page.evaluate(SENDER_PROBE, 'secret-profile')
        self.assertTrue(matched[0]['configured_owner_match'])
        self.assertFalse(result[0]['configured_owner_match'])

    async def test_profile_probe_reports_only_counts_and_booleans(self):
        await self.page.goto(URL)
        await self.page.evaluate('''() => {
            document.head.innerHTML = '<meta property="og:url" content="https://www.linkedin.com/in/private-owner/">';
            const data=document.createElement('script'); data.type='application/json';
            data.textContent=JSON.stringify({publicIdentifier:'private-owner', entityUrn:'urn:li:fsd_profile:ACoPrivate', firstName:'Secret Name', token:'Secret Key'});
            document.body.append(data);
        }''')
        data = await self.page.evaluate(PROFILE_PROBE, {'owner': 'private-owner', 'candidate': 'ACoPrivate'})
        self.assertFalse(data['canonical_present'])
        self.assertTrue(data['og_url_matches_owner'])
        self.assertEqual(data['owner_objects_with_requested_id'], 1)
        self.assertTrue(all(isinstance(value, (bool, int)) for value in data.values()))
        for private in ('private-owner', 'ACoPrivate', 'Secret', 'urn:li:', 'https://'):
            self.assertNotIn(private, str(data))

    async def test_identity_comparison_detects_encoding_without_assuming_identity(self):
        await self.page.goto(URL)
        await self.page.evaluate('''() => {
            events.querySelector('a').setAttribute('href', '/in/own%C3%A9r/');
        }''')
        probe = await self.page.evaluate(SENDER_PROBE, 'ownér')
        self.assertFalse(probe[0]['configured_owner_match'])
        self.assertTrue(probe[0]['normalized_owner_match'])
        extracted = await self.page.evaluate(EXTRACT, 'ownér')
        self.assertEqual(extracted[0]['role'], 'assistant')

    async def test_opaque_aliases_two_turns_and_uncertain_recovery(self):
        await self._opaque_alias_reply_flow(delayed_redirect=False)

    async def test_delayed_profile_redirect_two_turns_and_uncertain_recovery(self):
        await self._opaque_alias_reply_flow(delayed_redirect=True)

    async def _opaque_alias_reply_flow(self, delayed_redirect):
        context = self.page.context
        async def profiles(route):
            slug = 'owner' if '/in/ACoOwner/' in route.request.url else 'sender'
            body = (f'<script>setTimeout(() => history.replaceState(null, "", "/in/{slug}/"), 150);</script>'
                    if delayed_redirect else f'<link rel="canonical" href="https://www.linkedin.com/in/{slug}/">')
            await route.fulfill(body=body, content_type='text/html')
        await context.route('**/*', profiles)
        html = FIXTURE.replace('/in/sender/', '/in/ACoPeer/').replace('/in/owner/', '/in/ACoOwner/')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        identity = Identity(context, 'owner')
        with patch('bot.generate', return_value='First reply') as generate:
            await handle_thread(self.page, URL, identity, self.state)
            self.assertEqual(generate.call_count, 1)
        self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
        self.assertEqual(self.state.get(URL)[2], 'replied')
        self.assertIn('ACoOwner', identity.owners)
        self.assertIn('ACoPeer', identity.peers)
        # Recreate a crash after delivery, then add a long incoming turn.
        self.state.set(URL, self.state.get(URL)[0], digest('First reply'), 'uncertain')
        await self.page.evaluate('''() => {
            const li=document.createElement('li'); li.className='msg-s-message-list__event';
            li.innerHTML='<a href="/in/ACoPeer/"><span class="msg-s-message-group__name">Sender</span></a><p class="msg-s-event-listitem__body"></p>';
            li.querySelector('p').textContent='Looking for a job. '.repeat(100); events.append(li);
        }''')
        html = await self.page.content()
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        # A fresh resolver must verify aliases again after a restart.
        identity = Identity(context, 'owner')
        with patch('bot.generate', return_value='Second reply') as generate:
            await handle_thread(self.page, URL, identity, self.state)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
            self.assertEqual(self.state.get(URL)[2], 'replied')
            await handle_thread(self.page, URL, identity, self.state)
            self.assertEqual(generate.call_count, 1)

    async def test_unresolved_opaque_sender_never_sends(self):
        html = FIXTURE.replace('/in/sender/', '/in/ACoUnknown/')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        await self.page.context.route('**/*', lambda route: route.fulfill(body='<html></html>', content_type='text/html'))
        identity = Identity(self.page.context, 'owner')
        with patch('bot.generate') as generate:
            await handle_thread(self.page, URL, identity, self.state)
        generate.assert_not_called()
        self.assertIsNone(await self.page.evaluate('window.sendCount'))
        self.assertFalse(identity.peers)

    async def test_late_identity_baselines_old_history_without_sending(self):
        self.state.set(URL, 'old-unresolved-key', status='baseline_pending')
        with patch('bot.generate') as generate:
            await handle_thread(self.page, URL, 'owner', self.state)
        generate.assert_not_called()
        self.assertEqual(self.state.get(URL)[2], 'seen')
        self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_profile_login_redirect_never_verifies_owner(self):
        html = FIXTURE.replace('/in/sender/', '/in/ACoUnknown/')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        await self.page.context.route('**/*', lambda route: route.fulfill(body='<script>history.replaceState(null,"","/login");</script><link rel="canonical" href="https://www.linkedin.com/in/owner/">', content_type='text/html'))
        identity = Identity(self.page.context, 'owner')
        with patch('bot.generate') as generate:
            await handle_thread(self.page, URL, identity, self.state)
        generate.assert_not_called()
        self.assertEqual(identity.owners, {'owner'})
        self.assertFalse(identity.peers)

    async def test_delayed_profile_login_redirect_stays_unknown(self):
        html = FIXTURE.replace('/in/sender/', '/in/ACoUnknown/')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        await self.page.context.route('**/*', lambda route: route.fulfill(body='<script>setTimeout(() => history.replaceState(null,"","/login"),150);</script>', content_type='text/html'))
        identity = Identity(self.page.context, 'owner')
        with patch('bot.generate') as generate:
            await handle_thread(self.page, URL, identity, self.state)
        generate.assert_not_called()
        self.assertEqual(identity.owners, {'owner'})
        self.assertFalse(identity.peers)
        self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_two_successive_incoming_messages_once_each(self):
        with patch('bot.generate', return_value='Hey!'):
            await handle_thread(self.page, URL, 'owner', self.state)
            self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
            # Preserve the rendered conversation across sweeps, as the server does.
            html = await self.page.content()
            html = html.replace('incoming-1', 'incoming-2')
            html = html.replace('</ul>', '<li class="msg-s-message-list__event"><a class="msg-s-message-group__profile-link" href="/in/sender/"></a><p class="msg-s-event-listitem__body">Next</p></li></ul>')
            await self.page.unroute('**/*')
            await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
            await handle_thread(self.page, URL, 'owner', self.state)
            self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
            self.assertEqual(self.state.get(URL)[2], 'replied')
            await handle_thread(self.page, URL, 'owner', self.state)
            self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_confirmation_with_virtualized_history_then_long_incoming(self):
        # Send replaces an older rendered event: the list no longer grows.
        html = FIXTURE.replace("events.append(li);", "events.firstElementChild.remove(); events.append(li);")
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        with patch('bot.generate', return_value='First reply'):
            await handle_thread(self.page, URL, 'owner', self.state)
        self.assertEqual(self.state.get(URL)[2], 'replied')
        self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
        # Keep the acknowledged outgoing reply, then append a long incoming turn.
        await self.page.evaluate('''() => {
            const li=document.createElement('li'); li.className='msg-s-message-list__event';
            li.innerHTML='<a href="/in/sender/"><span class="msg-s-message-group__name">Sender</span></a><p class="msg-s-event-listitem__body"></p>';
            li.querySelector('p').textContent='I am looking for a job. '.repeat(100);
            events.append(li);
        }''')
        html = await self.page.content()
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        with patch('bot.generate', return_value='Second reply'):
            await handle_thread(self.page, URL, 'owner', self.state)
            self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
            self.assertEqual(self.state.get(URL)[2], 'replied')
            await handle_thread(self.page, URL, 'owner', self.state)
            self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_unrelated_outgoing_does_not_clear_uncertain_send(self):
        self.state.set(URL, '', digest('Unconfirmed reply'), 'uncertain')
        html = FIXTURE.replace('/in/sender/', '/in/owner/')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        await handle_thread(self.page, URL, 'owner', self.state)
        self.assertEqual(self.state.get(URL)[2], 'uncertain')
        self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_sender_group_boundaries_and_body_links(self):
        await self.page.goto(URL)
        await self.page.evaluate('''() => {
            events.innerHTML += '<li class="msg-s-message-list__event msg-s-message-list__event--own"><p class="msg-s-event-listitem__body">Own</p></li>' +
              '<li class="msg-s-message-list__event"><span class="msg-s-message-group__name">Hidden</span><p class="msg-s-event-listitem__body"><a href="/in/sender/">Link</a></p></li>';
        }''')
        data = await self.page.evaluate(EXTRACT, 'owner')
        self.assertEqual([x['role'] for x in data], ['user', 'assistant', 'unknown'])

    async def test_sender_header_link_scope_and_origin(self):
        await self.page.goto(URL)
        await self.page.evaluate('''() => {
            events.innerHTML = [
              '<a class="msg-s-message-group__profile-link" href="/in/sender/"></a>',
              '<a href="https://www.linkedin.com/in/owner/"><span class="msg-s-message-group__name">Owner</span></a>',
              '<span class="msg-s-message-group__name">Unlinked</span>',
              '<a href="https://example.com/in/sender/"><span class="msg-s-message-group__name">External</span></a>',
              '<span class="msg-s-message-group__name">Unlinked</span>'
            ].map(header => '<li class="msg-s-message-list__event">' + header + '<p class="msg-s-event-listitem__body">Message <a href="/in/sender/">Mention</a></p></li>').join('');
        }''')
        data = await self.page.evaluate(EXTRACT, 'owner')
        self.assertEqual([x['role'] for x in data], ['user', 'assistant', 'unknown', 'unknown', 'unknown'])

    async def test_uncertain_recovery_allows_next_incoming(self):
        await self.page.goto(URL)
        original = await self.page.evaluate(EXTRACT, 'owner')
        self.state.set(URL, event_key(original), digest('Earlier reply'), 'uncertain')
        html = FIXTURE.replace('</ul>', '<li class="msg-s-message-list__event msg-s-message-list__event--own"><p class="msg-s-event-listitem__body">Earlier reply</p></li><li class="msg-s-message-list__event"><a class="msg-s-message-group__profile-link" href="/in/sender/"></a><p class="msg-s-event-listitem__body">Next</p></li></ul>')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        with patch('bot.generate', return_value='Next reply'):
            await handle_thread(self.page, URL, 'owner', self.state)
        self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
        self.assertEqual(self.state.get(URL)[2], 'replied')

    async def test_unknown_sender_never_sends(self):
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=FIXTURE.replace('href="/in/sender/"', 'href="#"'), content_type='text/html'))
        await handle_thread(self.page, URL, 'owner', self.state)
        self.assertIsNone(await self.page.evaluate('window.sendCount'))

    async def test_delayed_send_enablement(self):
        html = FIXTURE.replace('<button class="msg-form__send-button"', '<script>document.querySelector("[contenteditable]").addEventListener("input", () => setTimeout(() => document.querySelector("button").disabled=false, 300));</script><button disabled class="msg-form__send-button"')
        await self.page.unroute('**/*')
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        with patch('bot.generate', return_value='Hey!'):
            await handle_thread(self.page, URL, 'owner', self.state)
        self.assertEqual(await self.page.evaluate('window.sendCount'), 1)
        self.assertEqual(self.state.get(URL)[2], 'replied')

    async def test_route_mismatch_rejected_without_private_logs(self):
        await self.page.unroute('**/*')
        html = '<script>history.replaceState(null, "", "/messaging/");</script>' + FIXTURE
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(ValueError):
            await handle_thread(self.page, URL, 'owner', self.state)
        self.assertIn('stage=navigation outcome=ValueError', output.getvalue())
        self.assertNotIn(URL, output.getvalue())
        self.assertIsNone(self.state.get(URL))

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
