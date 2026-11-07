"""Local reply-only LinkedIn automation. Run --check first; see README."""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv, dotenv_values
from playwright.async_api import async_playwright, expect
from replies import generate
from identity import Identity, profile_slug

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / 'private'
EXTRACT = (ROOT / 'extract.js').read_text(encoding='utf-8')
SENDER_PROBE = (ROOT / 'sender_probe.js').read_text(encoding='utf-8')
INVITES = 'https://www.linkedin.com/mynetwork/invitation-manager/'
INBOX = 'https://www.linkedin.com/messaging/'


def thread_id(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r'/messaging/thread/([^/]+)/?', parsed.path)
    return match[1] if parsed.hostname == 'www.linkedin.com' and match else None


@asynccontextmanager
async def stage(url, operation):
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        print(f'Thread={digest(url)[:16]} stage={operation} outcome={type(exc).__name__} elapsed_ms={int((time.monotonic()-started)*1000)}', flush=True)
        raise
    else:
        print(f'Thread={digest(url)[:16]} stage={operation} outcome=ok elapsed_ms={int((time.monotonic()-started)*1000)}', flush=True)


async def diagnose(page, url):
    # Fixed keys and counts only: never serialize DOM, text, attributes or URLs.
    async with stage(url, 'diagnostic'):
        counts = await page.evaluate('''() => ({
            document_ready: document.readyState === 'complete',
            events: document.querySelectorAll('.msg-s-message-list__event').length,
            list_events: document.querySelectorAll('li.msg-s-message-list__event').length,
            bodies: document.querySelectorAll('.msg-s-event-listitem__body').length,
            sender_links: document.querySelectorAll('a.msg-s-message-group__profile-link').length,
            sender_headers: document.querySelectorAll('.msg-s-message-group__name').length,
            composers: document.querySelectorAll('.msg-form__contenteditable[contenteditable=true]').length
        })''')
        print(f'Thread={digest(url)[:16]} route_matches={thread_id(page.url) == thread_id(url) and thread_id(url) is not None} structure={json.dumps(counts, sort_keys=True)}', flush=True)


async def load_thread(page, url):
    async with stage(url, 'navigation'):
        if not thread_id(url):
            raise ValueError('Invalid conversation route')
        await page.goto(url, wait_until='domcontentloaded')
        await guard(page)
        if thread_id(page.url) != thread_id(url):
            raise ValueError('Conversation route mismatch')
    async with stage(url, 'message_events'):
        try:
            await page.locator('.msg-s-message-list__event').first.wait_for(state='attached', timeout=6000)
        except Exception:
            await diagnose(page, url)
            # One bounded observation window distinguishes late rendering from
            # an absent adapter target; do not change navigation/action timeouts.
            await page.wait_for_timeout(1000)
            late = await page.locator('.msg-s-message-list__event').count()
            print(f'Thread={digest(url)[:16]} event_probe={"late_render" if late else "target_absent_after_probe"}', flush=True)
            if not late:
                raise
        await page.wait_for_timeout(500)


async def read_messages(page, own_slug, url):
    async with stage(url, 'extraction'):
        if thread_id(page.url) != thread_id(url):
            raise ValueError('Conversation route changed')
        if isinstance(own_slug, Identity):
            for outcome, elapsed in await own_slug.prepare(page):
                print(f'Thread={digest(url)[:16]} stage=profile_identity outcome={outcome} elapsed_ms={elapsed}', flush=True)
        return await messages(page, own_slug)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def event_key(messages):
    """Use the last event ID, falling back to its position plus recent context."""
    last = messages[-1]
    return digest(last['id'] or json.dumps(messages[-6:], ensure_ascii=False, sort_keys=True))


def own_reply_count(events, reply):
    return sum(e['role'] == 'assistant' and e['content'].strip() == reply.strip() for e in events)


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, seen TEXT, pending TEXT, status TEXT)')
        self.db.commit()

    def get(self, thread):
        return self.db.execute('SELECT seen,pending,status FROM threads WHERE id=?', (digest(thread),)).fetchone()

    def set(self, thread, seen, pending='', status='seen'):
        self.db.execute('INSERT OR REPLACE INTO threads VALUES (?,?,?,?)', (digest(thread), seen, pending, status))
        self.db.commit()


async def guard(page):
    path = urlsplit(page.url).path
    if any(x in path for x in ('/login', '/checkpoint', '/authwall', '/challenge')):
        raise RuntimeError('Login or security check required in browser. No bypass attempted.')


async def messages(page, own_slug):
    return await page.evaluate(EXTRACT, own_slug.adapter() if isinstance(own_slug, Identity) else own_slug)


async def identity_check(page, url, own_slug):
    async with stage(url, 'identity_comparison'):
        probe = await page.evaluate(SENDER_PROBE, own_slug.owner if isinstance(own_slug, Identity) else own_slug)
        keys = ('valid_sender_profile', 'configured_owner_match', 'normalized_owner_match', 'opaque_profile_identifier')
        counts = {key: sum(bool(event[key]) for event in probe) for key in keys}
        if isinstance(own_slug, Identity):
            counts['verified_owner_aliases'] = len(own_slug.owners - {own_slug.owner})
        print(f'Thread={digest(url)[:16]} identity_comparison={json.dumps(counts, sort_keys=True)}', flush=True)
        return probe


async def accept_requests(page):
    await page.goto(INVITES, wait_until='domcontentloaded')
    await guard(page)
    # Only invitation manager Accept buttons, never Connect, Follow, or invitations sent.
    buttons = page.get_by_role('button', name=re.compile(r'^(Accept|Accept .+ invitation|Accept invitation from .+)$', re.I))
    for _ in range(30):
        if not await buttons.count():
            break
        button = buttons.first
        if not await button.is_visible():
            break
        await button.click()
        print('Accepted an incoming invitation.', flush=True)
        await page.wait_for_timeout(350)


async def thread_links(page):
    # Some LinkedIn inbox layouts use clickable rows without thread <a> links.
    await page.goto(INBOX, wait_until='domcontentloaded')
    await guard(page)
    try:
        await page.locator('a[href*="/messaging/thread/"], .msg-conversation-listitem').first.wait_for(state='visible', timeout=10000)
    except Exception:
        print('No conversation links or rows found. Check the inbox filter/login.', flush=True)
        return []
    values = await page.locator('a[href*="/messaging/thread/"]').evaluate_all('(els) => els.map(e => e.href)')
    urls = list(dict.fromkeys(v.split('?')[0] for v in values if urlsplit(v).hostname == 'www.linkedin.com'))
    if urls:
        return urls[:40]
    count = min(await page.locator('.msg-conversation-listitem').count(), 40)
    print(f'Found {count} conversation rows; discovering thread URLs by clicking.', flush=True)
    for index in range(count):
        try:
            # Reset the URL before each click so a previous thread cannot be mistaken
            # for the newly selected one. Never derive a recipient from row order.
            await page.goto(INBOX, wait_until='domcontentloaded')
            row = page.locator('.msg-conversation-listitem').nth(index)
            await row.wait_for(state='visible', timeout=10000)
            await row.click()
            await page.wait_for_url(re.compile(r'https://www\.linkedin\.com/messaging/thread/[^/?#]+'), timeout=6000)
            url = page.url.split('?')[0].split('#')[0]
            if url not in urls:
                urls.append(url)
        except Exception as exc:
            print(f'Conversation row {index + 1}: could not resolve thread URL ({type(exc).__name__}).', flush=True)
    print(f'Discovered {len(urls)} conversation URLs.', flush=True)
    return urls


async def handle_thread(page, url, own_slug, state, check=False):
    await load_thread(page, url)
    events = await read_messages(page, own_slug, url)
    if not events:
        print('No extractable messages.', flush=True)
        return
    if check:
        await diagnose(page, url)
        async with stage(url, 'sender_probe'):
            probe = await identity_check(page, url, own_slug)
            print(f'Thread={digest(url)[:16]} sender_structure={json.dumps(probe, sort_keys=True)}', flush=True)
        counts = {r: sum(e['role'] == r for e in events) for r in ('user', 'assistant', 'unknown')}
        print(f'Adapter check: {counts}; composer={await page.locator(".msg-form__contenteditable[contenteditable=true]").count()}', flush=True)
        row = state.get(url)
        pending_visible = bool(row and row[2] == 'uncertain' and any(
            e['role'] == 'assistant' and digest(e['content'].strip()) == row[1] for e in events))
        pending_roles = {r: sum(e['role'] == r and digest(e['content'].strip()) == row[1] for e in events)
                         if row and row[2] == 'uncertain' else 0 for r in ('user', 'assistant', 'unknown')}
        print(f'Thread={digest(url)[:16]} recovery_check uncertain={bool(row and row[2] == "uncertain")} pending_own_reply_visible={pending_visible} pending_match_roles={json.dumps(pending_roles, sort_keys=True)}', flush=True)
        return
    if any(e['role'] == 'unknown' for e in events[-6:]):
        await diagnose(page, url)
        print('Skipped: sender identity unknown. Check extract.js.', flush=True)
        return
    key = event_key(events)
    row = state.get(url)
    if row and row[2] == 'baseline_pending':
        state.set(url, key)
        print('Baselined conversation after sender identity became available.', flush=True)
        return
    if row and row[2] == 'uncertain' and any(e['role'] == 'assistant' and digest(e['content'].strip()) == row[1] for e in events):
        state.set(url, row[0])
        row = state.get(url)
    if row and row[2] == 'uncertain':
        # After a crash between click and acknowledgement, avoid a duplicate send.
        # A later visible outgoing message resolves this automatically above.
        await identity_check(page, url, own_slug)
        print('Send outcome uncertain; inspect this thread manually before retrying.', flush=True)
        return
    if events[-1]['role'] != 'user':
        print('Skipped: latest visible message is outgoing.', flush=True)
        state.set(url, key)
        return
    if row and row[0] == key:
        print('Skipped: latest message already seen/baselined; send a new test message.', flush=True)
        return
    await page.wait_for_timeout(1000)  # collect rapid consecutive messages
    fresh = await read_messages(page, own_slug, url)
    if not fresh or fresh[-1]['role'] != 'user' or any(e['role'] == 'unknown' for e in fresh[-6:]):
        return
    events, key = fresh, event_key(fresh)
    reply = await asyncio.to_thread(generate, [{'role': e['role'], 'content': e['content']} for e in events if e['role'] != 'unknown'])
    # Don't reply to stale input when another message arrived during AI generation.
    current = await read_messages(page, own_slug, url)
    if not current or event_key(current) != key:
        return
    composer = page.locator('.msg-form__contenteditable[contenteditable=true]')
    if await composer.count() != 1:
        print('Skipped: expected one composer.', flush=True)
        return
    async with stage(url, 'composer'):
        if (await composer.inner_text()).strip():
            print('Skipped: composer contains an existing draft.', flush=True)
            return
        await composer.fill(reply)
    send = page.locator('button.msg-form__send-button:visible').or_(
        page.get_by_role('button', name=re.compile(r'^Send$', re.I)).and_(page.locator('button:visible'))
    )
    try:
        # Contenteditable input updates React state asynchronously. The old immediate
        # is_enabled() check could clear a valid reply before Send became enabled.
        async with stage(url, 'send_ready'):
            await expect(send).to_have_count(1, timeout=5000)
            await expect(send).to_be_enabled(timeout=5000)
    except Exception as exc:
        print(f'Send control not ready ({type(exc).__name__}); visible candidates={await send.count()}.', flush=True)
        # Only remove the draft this attempt created; never erase a human edit.
        if (await composer.inner_text()).strip() == reply.strip():
            await composer.fill('')
        return
    state.set(url, row[0] if row else '', digest(reply), 'uncertain')
    async with stage(url, 'send_click'):
        await send.click()
    # Confirm a newly visible own message. Never blindly retry a Send click.
    confirmation_started = time.monotonic()
    deadline = confirmation_started + 10
    previous_matches = own_reply_count(events, reply)
    before_ids = {e['id'] for e in events if e['id']}
    while time.monotonic() < deadline:
        after = await read_messages(page, own_slug, url)
        # Virtualized history can remove old events while appending the reply.
        # Compare matching own events, rather than slicing by the old list size.
        new_id_match = any(e['id'] and e['id'] not in before_ids and
                           e['role'] == 'assistant' and e['content'].strip() == reply.strip() for e in after)
        if own_reply_count(after, reply) > previous_matches or new_id_match:
            state.set(url, key, '', 'replied')
            print(f'Thread={digest(url)[:16]} stage=send_confirmation outcome=ok elapsed_ms={int((time.monotonic()-confirmation_started)*1000)}', flush=True)
            print('Reply confirmed.', flush=True)
            return
        await page.wait_for_timeout(400)
    print(f'Thread={digest(url)[:16]} stage=send_confirmation outcome=unconfirmed elapsed_ms={int((time.monotonic()-confirmation_started)*1000)}', flush=True)
    counts = {r: sum(e['role'] == r for e in after) for r in ('user', 'assistant', 'unknown')}
    matching_roles = {r: sum(e['role'] == r and e['content'].strip() == reply.strip() for e in after)
                      for r in ('user', 'assistant', 'unknown')}
    print(f'Thread={digest(url)[:16]} confirmation_probe before_events={len(events)} after_events={len(after)} roles={json.dumps(counts, sort_keys=True)} matching_reply_roles={json.dumps(matching_roles, sort_keys=True)} matching_own_before={previous_matches} matching_own_after={own_reply_count(after, reply)}', flush=True)
    print('Send not confirmed. Inspect browser; automatic retry is disabled for this thread.', flush=True)


async def run(args):
    file_profile = dotenv_values(ROOT / '.env').get('LINKEDIN_PROFILE_URL')
    load_dotenv(ROOT / '.env')
    effective_profile = os.getenv('LINKEDIN_PROFILE_URL', '')
    print(f'Identity configuration: env_file_profile_present={bool(file_profile)} environment_overrides_file={bool(file_profile and effective_profile != file_profile)}', flush=True)
    own = profile_slug(effective_profile)
    if not own:
        raise SystemExit('Set LINKEDIN_PROFILE_URL to your own profile URL in .env first.')
    PRIVATE.mkdir(exist_ok=True, mode=0o700)
    state = State(PRIVATE / 'state.sqlite3')
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(PRIVATE / 'browser'), headless=False, viewport={'width': 1440, 'height': 1000}, locale='en-US')
        context.set_default_timeout(6000)
        # Reuse the initial/restored tab and discard redundant saved-session tabs.
        inbox_page = context.pages[0] if context.pages else await context.new_page()
        for extra in context.pages[1:]:
            await extra.close()
        invite_page, thread_page = await context.new_page(), await context.new_page()
        await inbox_page.goto(INBOX, wait_until='domcontentloaded')
        await asyncio.to_thread(input, 'Log in manually. Use LinkedIn in English. When your inbox is visible, press Enter here: ')
        await guard(inbox_page)
        if args.login:
            await context.close()
            return
        identity = Identity(context, own, diagnostics=args.check)
        if not os.getenv('LLM_URL') or not os.getenv('LLM_MODEL'):
            print('WARNING: AI not configured; replies use limited English fallbacks.', flush=True)
        # Baseline existing threads to avoid replying to old inbox history.
        urls = await thread_links(inbox_page)
        for url in urls:
            try:
                if args.check:
                    await handle_thread(thread_page, url, identity, state, check=True)
                elif not state.get(url):
                    await load_thread(thread_page, url)
                    events = await read_messages(thread_page, identity, url)
                    if events:
                        state.set(url, event_key(events), status='baseline_pending' if any(e['role'] == 'unknown' for e in events[-6:]) else 'seen')
                        print('Baselined existing conversation; waiting for a new message.', flush=True)
            except Exception as exc:
                print(f'Initial thread check failed: {type(exc).__name__}', flush=True)
        if args.check:
            print('Read-only check finished; no invitations accepted or messages sent.')
            await context.close()
            return
        print('Running. New incoming messages only. Ctrl+C to stop.', flush=True)

        async def invitation_loop():
            while True:
                try:
                    await accept_requests(invite_page)
                except Exception as exc:
                    print(f'Invitation check needs attention: {type(exc).__name__}', flush=True)
                await asyncio.sleep(max(3, float(os.getenv('POLL_SECONDS', '5'))))

        async def message_loop():
            while True:
                try:
                    for url in await thread_links(inbox_page):
                        try:
                            await handle_thread(thread_page, url, identity, state)
                        except Exception as exc:
                            print(f'Thread needs attention: {type(exc).__name__}', flush=True)
                    print('Inbox sweep complete.', flush=True)
                except Exception as exc:
                    print(f'Inbox check needs attention: {type(exc).__name__}', flush=True)
                await asyncio.sleep(max(3, float(os.getenv('POLL_SECONDS', '5'))))
        try:
            await asyncio.gather(invitation_loop(), message_loop())
        finally:
            await context.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true', help='Read-only DOM adapter check; sends nothing')
    group.add_argument('--login', action='store_true', help='Only save manual login')
    try:
        asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        print('\nStopped.')
