# LinkedIn reply-only bot

Local Python + Playwright automation for accepting incoming connection invitations
and replying to newly incoming messages. Runs in a visible browser with a dedicated
persistent profile. Login and any verification are done manually by the owner.

**Status:** synthetic browser and logic regression tests run locally. Not yet
validated against the live LinkedIn DOM. Run the check and a real smoke test before submitting.
LinkedIn may change its DOM or restrict automation; this project does not bypass
security checks. Use LinkedIn's English interface for invitation button matching.

## Windows setup (PowerShell, Python 3.10+)

Extract this project, open its folder in VS Code, and open the terminal there:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item .env.example .env
```

Edit `.env` with your own LinkedIn profile URL. Configure `LLM_URL`, `LLM_MODEL`,
and `LLM_API_KEY` using an OpenAI-compatible provider or local server you choose.
`LLM_URL` must be the complete chat-completions endpoint. The model receives the
last 20 visible messages (up to 12,000 characters per message), so choose a provider
you trust. No message content is saved by this application or printed to the console.
The browser's own local cache may retain content. Without AI, replies use basic
English templates and will perform poorly on unusual or non-English messages.

```powershell
.\.venv\Scripts\python.exe bot.py --check
```

Log into LinkedIn in the opened browser. Open the inbox, then press Enter **in the
terminal**. The check reports incoming/outgoing/unknown message counts and composer
availability without accepting invitations or sending messages. For an empty inbox,
ask a friend to send a test message first. A valid thread should have `unknown: 0`
and `composer=1`. This only verifies extraction, not the Send/Accept buttons.

```powershell
.\.venv\Scripts\python.exe bot.py
```

Press Enter in the terminal after confirming login. Wait for `Running.` before
asking someone to send a **new** message and connection request. Existing inbox
messages are baselined on first startup so it does not reply to old conversations.
Verify actual acceptance and reply delivery from the sender's account. Then submit.
Do not operate the bot's browser while it runs; use a separate regular browser.
Keep the PC awake, connected, and the terminal/browser open until **Test finished**.
Ctrl+C stops it. No Docker or virtualization is needed.

## Behavior

- Checks invitations every 5 seconds, plus page load/action time, independently of AI replies.
- Sweeps up to 40 visible recent inbox threads. Full sweep time depends on thread count.
- Replies only to verified incoming messages; never clicks Connect or starts conversations.
- Waits briefly for consecutive messages, then checks again after generating a reply.
- Uses visible conversation context. Doesn't open links, execute message instructions,
  or download/view attachments. Asks for details where needed.
- Stores hashed thread/event identifiers in `private/state.sqlite3` for restart deduplication.
- Confirms a visible outgoing reply after clicking Send. An uncertain send is held
  rather than blindly retried. Inspect that thread manually. Once the outgoing
  message appears, the hold clears. If no message was sent, stop the bot and remove
  that thread's row from the local SQLite database before restarting.
- Send confirmation compares matching outgoing events before and after Send,
  including event IDs when available, so removal of older rendered history does
  not hide a newly sent reply. An unrelated outgoing message does not clear an
  uncertain send. `--check` reports `recovery_check` and `pending_match_roles`
  without changing the hold: `assistant` means the stored reply is recognized
  as outgoing; `user` means its direction was misidentified; all zeros means
  its exact text was not found in the extracted history. Do not clear a hold
  for an already delivered reply just to force another Send.
- Logs counts/status and exception class names, never message text, names or URLs.
- Never modifies a composer containing an existing draft.

## Limitations and troubleshooting

For the current timeout/unknown-sender failure, run just the read-only diagnostic:
` .\.venv\Scripts\python.exe bot.py --check ` (stop the regular bot first so
the persistent browser profile is not opened twice). Share only its console
diagnostics, never a DOM dump or screenshot containing conversations.
Each thread operation reports a hashed thread identifier, stage, outcome and
elapsed milliseconds. `route_matches=False` means the discovered route did not
remain selected; matching routes alone do not prove the rendered recipient.
The structural probe reports counts only. `late_render` means events appeared
in the bounded extra observation window; `target_absent_after_probe` means the
adapter target remained absent, which may be loading or a different DOM and
needs live inspection before changing selectors. No global timeouts were raised.
Unknown sender checks report sender-link/header counts without identities.
`--check` also emits `sender_structure=` for up to six recent events per thread:
fixed tag categories, sender-header ancestry, profile-link presence, and presence
of identity attributes. It never emits attribute values, text, names or URLs.
Both `--check` and held uncertain threads print `identity_comparison` counts
for up to six recent events: exact configured-owner matches, matches after
percent decoding and Unicode normalization, and opaque profile identifiers.
These are diagnostic comparisons only; they do not infer identity or clear a
hold. Startup also reports whether a pre-existing environment variable overrides
the profile configured in `.env`, without printing either value.
When profile alias resolution reports `canonical_missing`, `--check` emits
`profile_structure=` for the first two failed profile lookups. This inspects
Open Graph URL metadata and inert structured data, reporting only availability,
counts and comparisons. It never prints the profile URL, IDs, names or serialized
page data and does not use these diagnostic observations to infer direction.
When sender links are missing, share these lines to inspect the actual header
structure before changing the extraction adapter.
No live browser debugging connection was available during this fix, so the
live DOM and recipient rendering still require this check.

Selectors target LinkedIn's `msg-s-*` messaging UI. A different UI may require changes
to `extract.js`, `thread_links`, the composer/Send selector, or the Accept button
pattern in `bot.py`. `--check` cannot prove live Send/Accept functionality. The bot
requires sender profile links to distinguish incoming/outgoing messages; it skips
unknown identities. Group chats are not intentionally supported. Only the first
40 rendered inbox threads are scanned; archived, filtered, paginated, message
requests, and other hidden inboxes may be missed. Newly discovered threads at the
very first startup are baselined, including any test messages already waiting.
Messages arriving during a later sweep are handled on that sweep or the next one.

The sender adapter supports the observed layout where a
`.msg-s-message-group__name` span sits inside a profile anchor without the old
profile-link class. It reads that header's LinkedIn profile URL locally and
compares it with your configured profile; names and links in message text are
never used to guess identity. Unlinked sender headers remain unknown.
LinkedIn may use opaque `ACo...` profile IDs in message headers instead of your
configured public profile slug. The bot now verifies those IDs by loading each
distinct profile in a temporary tab and checking its redirect or canonical
profile URL. Owner and peer aliases are cached only in memory. Unresolved opaque
IDs remain unknown; no direction is inferred from a name or pending reply text.
The console reports `stage=profile_identity outcome=owner_verified` or
`peer_verified`; an unresolved lookup stays blocked and can retry after a minute.
These read-only profile visits can add time on the first sweep. `.env` and the
saved send hold are preserved. Once the delivered outgoing reply is recognized,
the existing recovery code clears the hold and can reply to the newer incoming
message. `--check` verifies identities but never changes the hold or sends.
Profile verification re-reads the browser URL after waiting for metadata, since
LinkedIn may redirect to the public profile after DOMContentLoaded without
providing a canonical link. A later login/security redirect remains unverified.

Login expiry, checkpoints, network failure or UI changes need owner attention.
Errors retry on the next sweep; login/security checks are never bypassed. A crash
between Send and confirmation may require the manual inspection described above.
Local duplicate detection is conservative, not a distributed exactly-once guarantee.
AI quality depends on the model. Fallback replies are limited and English-only.
Very long input is truncated for the model; attachment contents are unavailable.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Synthetic Playwright tests cover confirmed send, restart deduplication, read-only
check, no reply to self, unknown sender refusal, attachment/group extraction,
uncertain send recovery, two successive incoming turns, delayed Send enablement,
route mismatch refusal, privacy-safe timeout diagnostics, and fallback output.
They don't verify LinkedIn's live UI.

## Public repository

Commit only source, tests, README and `.env.example`. `.gitignore` excludes `.env`,
`private/` (cookies/browser profile/database), virtual environments and logs.
Never upload the whole working folder through GitHub's web uploader: it can bypass
`.gitignore`. Review `git status` and `git diff --cached` before pushing.

## Short submission note (use after live verification)

Runs locally using Python and Playwright with manual login and a persistent browser
session. Accepts incoming invitations and replies only to new incoming DMs using a
configurable AI model, recent conversation context, message-burst grouping, and
local duplicate prevention. Install requirements and Chromium, configure `.env`,
then run `python bot.py`. Must remain running on my PC. Does not open links or read
attachments; cannot know personal facts beyond configured context. Hidden inboxes,
group chats, security checkpoints and future LinkedIn UI changes are limitations.
