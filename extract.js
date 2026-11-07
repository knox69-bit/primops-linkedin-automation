// DOM adapter: LinkedIn changes its UI. Keep selectors centralized here.
// Refuse to guess message direction if sender identity cannot be established.
(ownSlug) => {
  const slug = href => (href || '').match(/\/in\/([^/?#]+)/)?.[1]?.toLowerCase();
  const events = [...document.querySelectorAll('li.msg-s-message-list__event')];
  let sender = null;
  const result = [];
  for (const event of events) {
    const profile = event.querySelector('a.msg-s-message-group__profile-link, a[href*="/in/"]');
    if (profile) sender = slug(profile.getAttribute('href')) || null;
    const name = event.querySelector('.msg-s-message-group__name');
    // System events are not user messages.
    const bodies = [...event.querySelectorAll('.msg-s-event-listitem__body')];
    const attachment = event.querySelector('.msg-s-event-listitem__attachment, .msg-s-attachment, .msg-s-event-listitem__image, .msg-s-event-listitem__file, a[download], img.msg-s-event-listitem__image');
    const item = event.querySelector('.msg-s-event-listitem');
    if (!bodies.length && !attachment && !item) continue;
    const own = event.matches('.msg-s-message-list__event--own') ||
      !!event.querySelector('.msg-s-message-group--is-me, .msg-s-event-listitem--self');
    const role = own || sender === ownSlug ? 'assistant' : sender ? 'user' : 'unknown';
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
