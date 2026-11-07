// DOM adapter: LinkedIn changes its UI. Keep selectors centralized here.
// Refuse to guess message direction if sender identity cannot be established.
(ownSlug) => {
  const owners = new Set(typeof ownSlug === 'string' ? [ownSlug] : ownSlug.owners);
  const peers = new Set(typeof ownSlug === 'string' ? [] : ownSlug.peers);
  const slug = href => {
    if (!href) return null;
    try {
      const url = new URL(href, 'https://www.linkedin.com');
      if (url.protocol !== 'https:' || url.hostname !== 'www.linkedin.com') return null;
      const raw = url.pathname.match(/^\/in\/([^/]+)\/?$/)?.[1];
      if (!raw) return null;
      const value = decodeURIComponent(raw).normalize('NFC');
      return value.startsWith('ACo') ? value : value.toLowerCase();
    } catch { return null; }
  };
  const events = [...document.querySelectorAll('li.msg-s-message-list__event')];
  let sender = null;
  const result = [];
  for (const event of events) {
    const name = event.querySelector('.msg-s-message-group__name');
    // Observed live DOM: the name span is inside a profile anchor without
    // msg-s-message-group__profile-link. Scope identity to this header only.
    const headerLink = name?.closest('a[href]');
    const candidate = headerLink || event.querySelector('a.msg-s-message-group__profile-link');
    const profile = candidate && event.contains(candidate) &&
      !candidate.closest('.msg-s-event-listitem__body') ? candidate : null;
    // System events are not user messages.
    const bodies = [...event.querySelectorAll('.msg-s-event-listitem__body')];
    const attachment = event.querySelector('.msg-s-event-listitem__attachment, .msg-s-attachment, .msg-s-event-listitem__image, .msg-s-event-listitem__file, a[download], img.msg-s-event-listitem__image');
    const item = event.querySelector('.msg-s-event-listitem');
    if (!bodies.length && !attachment && !item) continue;
    const own = event.matches('.msg-s-message-list__event--own') ||
      !!event.querySelector('.msg-s-message-group--is-me, .msg-s-event-listitem--self');
    // A header starts a new group even when its profile is unavailable. Never
    // infer a sender from profile links in message text or inherit across an
    // outgoing event. Headerless adjacent events continue the preceding group.
    if (own) sender = null;
    else if (profile || name) sender = profile ? slug(profile.getAttribute('href')) || null : null;
    const role = own || owners.has(sender) ? 'assistant' :
      sender && (!sender.startsWith('ACo') || peers.has(sender)) ? 'user' : 'unknown';
    let content = bodies.map(b => b.innerText || b.textContent || '').join('\n').trim();
    if (attachment) content += '\n[Attachment: file or image; contents unavailable]';
    // Some attachment-only messages use a different element. A real event still gets a reply.
    if (!content && item) content = '[Empty message or unsupported attachment]';
    if (!content) continue;
    const id = event.getAttribute('data-event-urn') || item?.getAttribute('data-event-urn') || '';
    result.push({id, role, content});
  }
  return result;
}
