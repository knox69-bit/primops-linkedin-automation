"""Observe whether a failed Playwright action dispatched browser input."""
import uuid


async def click_once(page, button):
    token = uuid.uuid4().hex
    await page.evaluate('''token => {
        const attempt = {token, activated:false};
        const events=['pointerdown','mousedown','click','submit','keydown'];
        const mark=() => { attempt.activated=true; };
        for (const name of events) document.addEventListener(name,mark,true);
        attempt.dispose=() => { for (const name of events) document.removeEventListener(name,mark,true); };
        window.__primopsSendAttempt=attempt;
    }''', token)
    try:
        try:
            await button.click()
        except Exception as exc:
            try:
                activated = await page.evaluate('''token => {
                    const attempt=window.__primopsSendAttempt;
                    return attempt?.token === token ? attempt.activated : null;
                }''', token)
            except Exception:
                activated = None
            if activated is False:
                return False, type(exc).__name__
            # Lost document or any observed activation: outcome stays uncertain.
            raise
        return True, ''
    finally:
        try:
            await page.evaluate('''token => {
                const attempt=window.__primopsSendAttempt;
                if (attempt?.token === token) { attempt.dispose(); delete window.__primopsSendAttempt; }
            }''', token)
        except Exception:
            pass
