"""Verify opaque sender aliases through profile navigation, without durable data."""
import re
import time
import unicodedata
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROFILE_PROBE = (Path(__file__).resolve().parent / 'profile_probe.js').read_text(encoding='utf-8')


def profile_slug(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'www.linkedin.com':
        return None
    match = re.fullmatch(r'/in/([^/]+)/?', parsed.path)
    if not match:
        return None
    value = unicodedata.normalize('NFC', unquote(match[1]))
    return value if value.startswith('ACo') else value.lower()


class Identity:
    def __init__(self, context, owner, diagnostics=False):
        self.context = context
        self.owner = owner
        self.owners = {owner}
        self.peers = set()
        self.retry_at = {}
        self.diagnostic_budget = 2 if diagnostics else 0

    def adapter(self):
        return {'owners': sorted(self.owners), 'peers': sorted(self.peers)}

    async def prepare(self, thread_page):
        # Same header structure confirmed by the private structural probe.
        links = await thread_page.evaluate('''() => [...document.querySelectorAll('li.msg-s-message-list__event')].map(event => {
            const name=event.querySelector('.msg-s-message-group__name');
            const link=name?.closest('a[href]') || event.querySelector('a.msg-s-message-group__profile-link');
            return link && event.contains(link) && !link.closest('.msg-s-event-listitem__body') ? link.href : null;
        }).filter(Boolean)''')
        outcomes = []
        for link in dict.fromkeys(links):
            candidate = profile_slug(link)
            if not candidate or not candidate.startswith('ACo') or candidate in self.owners or candidate in self.peers:
                continue
            if time.monotonic() < self.retry_at.get(candidate, 0):
                continue
            # A fresh tab prevents previous profile metadata from being reused.
            page = await self.context.new_page()
            started = time.monotonic()
            outcome = 'unresolved'
            try:
                response = await page.goto(link, wait_until='domcontentloaded', timeout=10000)
                final = profile_slug(page.url)
                outcome = 'profile_unavailable'
                if response and response.ok and final:
                    try:
                        await page.locator('link[rel="canonical"]').first.wait_for(state='attached', timeout=2500)
                    except Exception:
                        pass
                    canonical = profile_slug(await page.evaluate('''() => document.querySelector('link[rel="canonical"]')?.href || '' '''))
                    if not canonical and self.diagnostic_budget:
                        self.diagnostic_budget -= 1
                        probe = await page.evaluate(PROFILE_PROBE, {'owner': self.owner, 'candidate': candidate})
                        thread_hash = hashlib.sha256(thread_page.url.encode()).hexdigest()[:16]
                        print(f'Thread={thread_hash} profile_structure={json.dumps(probe, sort_keys=True)}', flush=True)
                    # LinkedIn can change the route after DOMContentLoaded,
                    # while the metadata wait is in progress. Never use the
                    # earlier URL to resolve identity or accept a security gate.
                    final = profile_slug(page.url)
                    outcome = 'profile_unavailable' if not final else (
                        'canonical_missing' if not canonical and final == candidate else 'alias_unresolved')
                    # Trust a profile redirect, or canonical metadata on the
                    # requested profile only. Never accept login/home redirects.
                    resolved = final if final and final != candidate else canonical if final == candidate else None
                    if resolved == self.owner:
                        self.owners.add(candidate)
                        outcome = 'owner_verified'
                    elif resolved and not resolved.startswith('ACo'):
                        self.peers.add(candidate)
                        outcome = 'peer_verified'
            except Exception as exc:
                # Exception text can include profile URLs; return only its class.
                outcome = type(exc).__name__
            finally:
                await page.close()
            if candidate not in self.owners and candidate not in self.peers:
                self.retry_at[candidate] = time.monotonic() + 60
            outcomes.append((outcome, int((time.monotonic() - started) * 1000)))
        return outcomes
