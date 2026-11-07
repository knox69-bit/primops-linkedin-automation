// Return only fixed categories, counts and booleans. Never return DOM text,
// class names, attribute values, identifiers, names, or URLs.
(ownSlug = '') => {
  ownSlug = typeof ownSlug === 'string' ? ownSlug : '';
  const normalized = value => {
    try { return decodeURIComponent(value).normalize('NFC').toLowerCase(); }
    catch { return null; }
  };
  const tags = new Set(['A', 'BUTTON', 'DIV', 'SPAN', 'LI', 'H3', 'H4', 'IMG']);
  const shape = node => ({
    tag: tags.has(node.tagName) ? node.tagName.toLowerCase() : 'other',
    profile_href: /^\/in\/|^https:\/\/www\.linkedin\.com\/in\//.test(node.getAttribute('href') || ''),
    has_href: node.hasAttribute('href'),
    has_member_id: node.hasAttribute('data-member-id'),
    has_entity_urn: node.hasAttribute('data-entity-urn'),
    has_sender_urn: node.hasAttribute('data-sender-urn'),
    has_aria_label: node.hasAttribute('aria-label'),
    has_test_id: node.hasAttribute('data-testid'),
    profile_descendants: [...node.querySelectorAll('a[href]')].filter(a =>
      !a.closest('.msg-s-event-listitem__body') &&
      /^\/in\/|^https:\/\/www\.linkedin\.com\/in\//.test(a.getAttribute('href') || '')).length
  });
  return [...document.querySelectorAll('li.msg-s-message-list__event')].slice(-6).map(event => {
    const header = event.querySelector('.msg-s-message-group__name');
    const link = header?.closest('a[href]') || event.querySelector('a.msg-s-message-group__profile-link');
    let sender = null;
    if (link && event.contains(link) && !link.closest('.msg-s-event-listitem__body')) {
      try {
        const url = new URL(link.getAttribute('href'), 'https://www.linkedin.com');
        if (url.protocol === 'https:' && url.hostname === 'www.linkedin.com')
          sender = url.pathname.match(/^\/in\/([^/]+)\/?$/)?.[1] || null;
      } catch {}
    }
    const ancestry = [];
    for (let node = header; node && event.contains(node) && ancestry.length < 4; node = node.parentElement)
      ancestry.push(shape(node));
    return {
      valid_sender_profile: sender !== null,
      configured_owner_match: sender !== null && sender.toLowerCase() === ownSlug.toLowerCase(),
      normalized_owner_match: sender !== null && normalized(sender) !== null && normalized(sender) === normalized(ownSlug),
      opaque_profile_identifier: sender !== null && /^ACo[A-Za-z0-9_-]+$/.test(sender),
      header_present: !!header,
      own_event_marker: event.matches('.msg-s-message-list__event--own'),
      own_descendant_marker: !!event.querySelector('.msg-s-message-group--is-me, .msg-s-event-listitem--self'),
      event: shape(event),
      header_ancestry: ancestry
    };
  });
}
