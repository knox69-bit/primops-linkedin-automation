// Diagnostic only. Return fixed keys, counts and booleans, never identity data.
({owner, candidate}) => {
  const identifier = value => {
    try {
      const url = new URL(value, location.origin);
      if (url.protocol !== 'https:' || url.hostname !== 'www.linkedin.com') return null;
      return decodeURIComponent(url.pathname.match(/^\/in\/([^/]+)\/?$/)?.[1] || '').normalize('NFC').toLowerCase() || null;
    } catch { return null; }
  };
  const canonical = document.querySelector('link[rel="canonical"]')?.href;
  const og = document.querySelector('meta[property="og:url"]')?.content;
  const stats = {
    document_complete: document.readyState === 'complete',
    main_present: !!document.querySelector('main'),
    route_is_requested_profile: identifier(location.href) === candidate.toLowerCase(),
    canonical_present: !!canonical,
    canonical_matches_owner: !!canonical && identifier(canonical) === owner.toLowerCase(),
    og_url_present: !!og,
    og_url_matches_owner: !!og && identifier(og) === owner.toLowerCase(),
    json_scripts: document.querySelectorAll('script[type="application/ld+json"], script[type="application/json"]').length,
    code_blocks: document.querySelectorAll('code').length,
    parsed_blocks: 0, unparsed_blocks: 0, oversized_blocks: 0,
    public_identifier_fields: 0, owner_identifier_fields: 0,
    owner_objects_with_entity_urn: 0, owner_objects_with_requested_id: 0,
    entity_urn_fields: 0, profile_id_fields: 0,
    public_identifier_tokens: 0, entity_urn_tokens: 0
  };
  const seen = new Set();
  const visit = (obj, depth = 0) => {
    if (!obj || typeof obj !== 'object' || depth > 30 || seen.has(obj)) return;
    seen.add(obj);
    if (typeof obj.publicIdentifier === 'string') {
      stats.public_identifier_fields++;
      if (obj.publicIdentifier.normalize('NFC').toLowerCase() === owner.toLowerCase()) {
        stats.owner_identifier_fields++;
        if (typeof obj.entityUrn === 'string') {
          stats.owner_objects_with_entity_urn++;
          if (obj.entityUrn.split(':').pop() === candidate) stats.owner_objects_with_requested_id++;
        }
      }
    }
    if (typeof obj.entityUrn === 'string') stats.entity_urn_fields++;
    if (obj.profileId !== undefined) stats.profile_id_fields++;
    for (const value of Object.values(obj)) visit(value, depth + 1);
  };
  // Inspect inert serialized data only; never execute page scripts or fetch APIs.
  for (const block of [...document.querySelectorAll('script[type="application/ld+json"], script[type="application/json"], code')].slice(0, 40)) {
    const content = block.textContent || '';
    if (content.length > 2000000) { stats.oversized_blocks++; continue; }
    stats.public_identifier_tokens += (content.match(/"publicIdentifier"/g) || []).length;
    stats.entity_urn_tokens += (content.match(/"entityUrn"/g) || []).length;
    try { visit(JSON.parse(content)); stats.parsed_blocks++; }
    catch { stats.unparsed_blocks++; }
  }
  return stats;
}
