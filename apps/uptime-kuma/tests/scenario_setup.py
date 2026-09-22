"""Real Kuma 2.5.3 onboarding + the socket.io client already used by its UI."""
from urllib.parse import urlsplit

USERNAME = "corenova"
PASSWORD = "CoreNova-Verify-2026!"
ROOT = 'document.querySelector("#app")._vnode.component.proxy'


def owner_page(page, base_url):
    page.goto(base_url + "/", wait_until="networkidle")
    if "/setup-database" in page.url:
        # The real 2.x database wizard must be exercised, not bypassed by seeding SQL.
        page.get_by_text("MariaDB/MySQL", exact=True).wait_for()
        page.get_by_text("SQLite", exact=True).click()
        page.get_by_role("button", name="Next", exact=True).click()
    page.wait_for_function("""() => document.body.innerText.includes('Add New Monitor') ||
        document.body.innerText.includes('Create your admin account') ||
        document.body.innerText.includes('Remember me')""", timeout=90_000)
    if page.get_by_text("Create your admin account", exact=True).is_visible():
        page.get_by_label("Username", exact=True).fill(USERNAME)
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_label("Repeat Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Create", exact=True).click()
    elif page.get_by_role("button", name="Log in", exact=True).is_visible():
        page.get_by_label("Username", exact=True).fill(USERNAME)
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Log in", exact=True).click()
    page.get_by_text("Add New Monitor", exact=False).first.wait_for(timeout=60_000)
    page.wait_for_function(f"{ROOT}.loggedIn === true")


def socket_call(page, event, *args):
    result = page.evaluate("""({event,args}) => new Promise((resolve,reject) => {
        const timer = setTimeout(()=>reject(new Error('socket acknowledgement timeout: '+event)),20000);
        document.querySelector('#app')._vnode.component.proxy.getSocket().emit(event,...args,result=>{
            clearTimeout(timer); resolve(result);
        });
    })""", {"event": event, "args": list(args)})
    assert result.get("ok") is True, f"{event}: {result}"
    return result


def prepare(page, slug):
    parts = urlsplit(page.url)
    base_url = f"{parts.scheme}://{parts.netloc}"
    owner_page(page, base_url)
    page.wait_for_function(f"Object.values({ROOT}.monitorList).some(m=>m.name.startsWith('P03 '))")
    monitor_id = page.evaluate(f"Object.values({ROOT}.monitorList).find(m=>m.name.startsWith('P03 ')).id")
    page.goto(base_url + "/dashboard/" + str(monitor_id), wait_until="networkidle")
    page.get_by_text("Edit", exact=True).first.wait_for()
