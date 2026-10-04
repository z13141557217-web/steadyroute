from playwright.sync_api import sync_playwright
errs = []
def say(*a): print(*a, flush=True)
def confirm(pg, tag, cancel=False):
    pg.wait_for_selector("dialog[open]"); pg.wait_for_timeout(200)
    say(tag, "::", pg.inner_text("#dlg-title"), "|", pg.inner_text("#dlg-body .plain") if pg.locator("#dlg-body .plain").count() else "", "|", pg.inner_text(".compare").replace("\n", " ") if pg.locator(".compare").count() else "")
    pg.click("#dlg-cancel" if cancel else "#dlg-ok"); pg.wait_for_timeout(900); say("   toast:", pg.inner_text("#toast"))
with sync_playwright() as p:
    b = p.chromium.launch()
    for scheme in ("light", "dark"):
        pg = b.new_page(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:8765/settings.html"); pg.evaluate("sessionStorage.clear()"); pg.reload(); pg.wait_for_timeout(1000)
        if scheme == "dark":
            pg.screenshot(path="n-before-dark.png", full_page=True); pg.close(); continue
        pg.screenshot(path="n-before.png", full_page=True)
        say("state:", pg.inner_text("#ai-state"))
        pg.click("#migration-accept"); pg.wait_for_selector("dialog[open]"); pg.wait_for_timeout(300); pg.screenshot(path="n-dialog.png"); confirm(pg, "migrate")
        pg.wait_for_timeout(800); say("result:", pg.inner_text("#ai-result"))
        pg.screenshot(path="n-after.png", full_page=True)
        pg.click("button.choice[data-country=JP]"); confirm(pg, "JP", cancel=True)
        say("   checked after cancel:", pg.get_attribute("button.choice[data-country=TW]", "aria-checked"))
        pg.click("button.choice[data-country=off]"); confirm(pg, "off")
        say("   sites hidden:", pg.is_hidden("#sites"))
        pg.click("button.choice[data-country=TW]"); confirm(pg, "on")
        pg.fill("#manual-input", "https://www.poe.com/chat"); pg.click("#manual-form button"); confirm(pg, "add")
        pg.click("input[data-group='家宽出口']", force=True); pg.wait_for_timeout(800); say("switch off:", pg.inner_text("#toast"))
        say("groups:", pg.inner_text("#groups").replace("\n", " / "))
        pg.click("#advanced summary"); pg.click("#sync-now"); pg.wait_for_timeout(800); say("sync:", pg.inner_text("#toast"), "|", pg.inner_text("#source").replace("\n", " "))
        pg.screenshot(path="n-final.png", full_page=True)
        mp = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        mp.goto("http://127.0.0.1:8765/settings.html"); mp.wait_for_timeout(1200); mp.screenshot(path="n-mobile.png", full_page=True)
        say("mobile overflow:", mp.evaluate("document.documentElement.scrollWidth"))
    b.close()
say("errors:", errs)
