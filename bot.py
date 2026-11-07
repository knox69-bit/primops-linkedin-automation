"""Local reply-only LinkedIn automation. Run --check first; see README."""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from playwright.async_api import async_playwright, expect
from replies import generate

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / 'private'
EXTRACT = (ROOT / 'extract.js').read_text(encoding='utf-8')
INVITES = 'https://www.linkedin.com/mynetwork/invitation-manager/'
INBOX = 'https://www.linkedin.com/messaging/'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def event_key(messages):
    """Use the last event ID, falling back to its position plus recent context."""
    last = messages[-1]
    return digest(last['id'] or json.dumps(messages[-6:], ensure_ascii=False, sort_keys=True))


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
    return await page.evaluate(EXTRACT, own_slug)


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
    await page.goto(url, wait_until='domcontentloaded')
    await guard(page)
    await page.locator('.msg-s-message-list__event').first.wait_for(state='attached', timeout=6000)
    # Let React render the conversation, then read again before generating/sending.
    await page.wait_for_timeout(500)
    events = await messages(page, own_slug)
    if not events:
        print('No extractable messages.', flush=True)
        return
    if check:
        counts = {r: sum(e['role'] == r for e in events) for r in ('user', 'assistant', 'unknown')}
        print(f'Adapter check: {counts}; composer={await page.locator(".msg-form__contenteditable[contenteditable=true]").count()}', flush=True)
        return
    if any(e['role'] == 'unknown' for e in events[-6:]):
        print('Skipped: sender identity unknown. Check extract.js.', flush=True)
        return
    key = event_key(events)
    row = state.get(url)
    if events[-1]['role'] != 'user':
        print('Skipped: latest visible message is outgoing.', flush=True)
        state.set(url, key)
        return
    if row and row[2] == 'uncertain' and any(e['role'] == 'assistant' and digest(e['content'].strip()) == row[1] for e in events):
        state.set(url, row[0])
        row = state.get(url)
    if row and row[2] == 'uncertain':
        # After a crash between click and acknowledgement, avoid a duplicate send.
        # A later visible outgoing message resolves this automatically above.
        print('Send outcome uncertain; inspect this thread manually before retrying.', flush=True)
        return
    if row and row[0] == key:
        print('Skipped: latest message already seen/baselined; send a new test message.', flush=True)
        return
    await page.wait_for_timeout(1000)  # collect rapid consecutive messages
    fresh = await messages(page, own_slug)
    if not fresh or fresh[-1]['role'] != 'user' or any(e['role'] == 'unknown' for e in fresh[-6:]):
        return
    events, key = fresh, event_key(fresh)
    reply = await asyncio.to_thread(generate, [{'role': e['role'], 'content': e['content']} for e in events if e['role'] != 'unknown'])
    # Don't reply to stale input when another message arrived during AI generation.
    current = await messages(page, own_slug)
    if not current or event_key(current) != key:
        return
    composer = page.locator('.msg-form__contenteditable[contenteditable=true]')
    if await composer.count() != 1:
        print('Skipped: expected one composer.', flush=True)
        return
    if (await composer.inner_text()).strip():
        print('Skipped: composer contains an existing draft.', flush=True)
        return
    # This path is reached only with a verified incoming message in this thread.
    await composer.fill(reply)
    send = page.locator('button.msg-form__send-button:visible').or_(
        page.get_by_role('button', name=re.compile(r'^Send$', re.I)).and_(page.locator('button:visible'))
    )
    try:
        # Contenteditable input updates React state asynchronously. The old immediate
        # is_enabled() check could clear a valid reply before Send became enabled.
        await expect(send).to_have_count(1, timeout=5000)
        await expect(send).to_be_enabled(timeout=5000)
    except Exception as exc:
        print(f'Send control not ready ({type(exc).__name__}); visible candidates={await send.count()}.', flush=True)
        # Only remove the draft this attempt created; never erase a human edit.
        if (await composer.inner_text()).strip() == reply.strip():
            await composer.fill('')
        return
    state.set(url, row[0] if row else '', digest(reply), 'uncertain')
    await send.click()
    # Confirm a newly visible own message. Never blindly retry a Send click.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        after = await messages(page, own_slug)
        if any(e['role'] == 'assistant' and e['content'].strip() == reply.strip() for e in after[len(events):]):
            state.set(url, key, '', 'replied')
            print('Reply confirmed.', flush=True)
            return
        await page.wait_for_timeout(400)
    print('Send not confirmed. Inspect browser; automatic retry is disabled for this thread.', flush=True)


async def run(args):
    load_dotenv(ROOT / '.env')
    own = re.search(r'/in/([^/?#]+)', os.getenv('LINKEDIN_PROFILE_URL', ''))
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
        if not os.getenv('LLM_URL') or not os.getenv('LLM_MODEL'):
            print('WARNING: AI not configured; replies use limited English fallbacks.', flush=True)
        # Baseline existing threads to avoid replying to old inbox history.
        urls = await thread_links(inbox_page)
        for url in urls:
            try:
                if args.check:
                    await handle_thread(thread_page, url, own[1].lower(), state, check=True)
                elif not state.get(url):
                    await thread_page.goto(url, wait_until='domcontentloaded')
                    await thread_page.locator('.msg-s-message-list__event').first.wait_for(state='attached')
                    await thread_page.wait_for_timeout(500)
                    events = await messages(thread_page, own[1].lower())
                    if events:
                        state.set(url, event_key(events))
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
                            await handle_thread(thread_page, url, own[1].lower(), state)
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
