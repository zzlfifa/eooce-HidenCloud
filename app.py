#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os,re,sys,time,random,requests
from playwright.sync_api import sync_playwright

# --- 环境变量 ---
COOKIE_VALUE = os.environ.get('COOKIE_VALUE') or ""    # remember_web cookie 值，必填
EMAIL        = os.environ.get('EMAIL') or ""           # 登录邮箱,可选，作为备用,TG通知需要填写
PASSWORD     = os.environ.get('PASSWORD') or ""        # 登录密码,可选，作为备用
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""    # Telegram Bot Token,可选
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""      # Telegram Chat ID,可选

BASE_URL = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

# --- 代理配置（由工作流 shell 脚本写入 $GITHUB_ENV）---
IS_PROXY      = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER  = os.environ.get('PROXY_SERVER') or "socks5://127.0.0.1:1080"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

# 日志
def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""

def get_current_ip(proxy_server=None):
    """获取当前出口IP"""
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"

def send_telegram_notification(status, old_due, new_due):
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False

    # 获取运行时间
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****'

    text = (
        f"HidenCloud 续期通知\n\n"
        f"{status}\n"
        f"👤 账号: {masked_email}\n"
        f"📅 续期前到期：{old_due}\n"
        f"📅 续期后到期：{new_due}\n"
        f"🕒 续期时间：{now}"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        else:
            log(f"❌ Telegram 通知失败: {resp.text}")
            return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False

def handle_cloudflare(page):
    """等待 Cloudflare / HidenCloud 安全验证通过。
    新版站点会在页面加载前插入自定义安全验证页（标题含 Security Verification / 请稍候）。"""
    iframe_selector = 'iframe[src*="challenges.cloudflare.com"]'
    start_time = time.time()
    while time.time() - start_time < 90:
        try:
            title = page.title().lower()
            url = page.url
            iframe_count = page.locator(iframe_selector).count()
        except Exception:
            time.sleep(1)
            continue
        if (iframe_count == 0 and "__cf_chl_rt_tk" not in url
                and "security verification" not in title and "请稍候" not in title):
            return True
        if page.locator(iframe_selector).count() > 0:
            log("⚠️ 检测到 Cloudflare 验证...")
        try:
            frame = page.frame_locator(iframe_selector)
            checkbox = frame.locator('input[type="checkbox"]')
            if checkbox.count() and checkbox.is_visible():
                log("🖱️ 点击验证复选框...")
                time.sleep(random.uniform(0.5, 1.5))
                checkbox.click()
                log("⏳ 已点击，等待验证结果...")
                time.sleep(5)
        except Exception:
            pass
        time.sleep(2)
    log("❌ 验证超时。")
    return False

def wait_turnstile_token(page, timeout_s=90):
    """等待登录表单中 Turnstile 隐藏字段拿到 token，否则提交会报
    'The cf-turnstile-response field is required.'"""
    try:
        page.wait_for_function(
            "() => { const i = document.querySelector('input[name=\"cf-turnstile-response\"]');"
            " return i && i.value && i.value.length > 10; }",
            timeout=timeout_s * 1000,
        )
        log("✅ Turnstile token 已生成")
        return True
    except Exception:
        log("⚠️ 未获取到 Turnstile token，继续尝试提交...")
        return False

def wait_modal_turnstile(page, server_id, timeout_s=90):
    """等待 Renew 弹窗内的 Turnstile token 生成。
    站点实现: 弹窗处于 display:none 时 Turnstile 不运行,弹窗打开后才开始;
    没有该 token,后端会拒绝 Create Invoice 请求(发票静默创建失败)。"""
    try:
        page.wait_for_function(
            """(sid) => {
                const c = document.getElementById('renewService-' + sid);
                if (!c) return false;
                const i = c.querySelector('input[name="cf-turnstile-response"]');
                return i && i.value && i.value.length > 10;
            }""",
            arg=server_id, timeout=timeout_s * 1000,
        )
        log("✅ 弹窗内 Turnstile token 已生成")
        return True
    except Exception:
        log("⚠️ 弹窗内未获取到 Turnstile token，继续尝试...")
        return False

def wait_text(page, text, max_s=60):
    """等待页面真正渲染出指定文本（站点加载慢且会插入安全验证页，不能只靠固定 sleep）"""
    try:
        page.wait_for_function(
            "(t) => document.body && document.body.innerText.includes(t)",
            arg=text, timeout=max_s * 1000,
        )
        return True
    except Exception:
        return False

def js_click_exact(page, text, desc, within=None):
    """DOM 级点击,按钮文本 trim 后**完全相等**才命中。
    站点页面常驻一个 Add Balance 弹窗,其中有 'Pay Now' 按钮;
    旧版用 includes('Pay') 模糊匹配会误点到它,导致续期假成功。"""
    ok = page.evaluate(
        """(arg) => {
            const root = arg.within ? document.querySelector(arg.within) : document;
            if (!root) return 'NO_ROOT';
            const els = [...root.querySelectorAll('button, a')];
            const el = els.find(e => (e.innerText || '').trim() === arg.text
                                    && e.offsetParent !== null && !e.disabled);
            if (!el) return 'NOT_FOUND';
            el.click();
            return 'CLICKED';
        }""",
        {"text": text, "within": within},
    )
    log(f"🖱️ JS点击 {desc}: {ok}")
    return ok

def neutralize_traps(page):
    """中和页面干扰元素:
    1. 禁用并隐藏 Add Balance 弹窗的 'Pay Now' 充值按钮(与续期支付无关,历史上被误点过)
    2. 隐藏 'Ad blocker detected' 全屏遮罩(会拦截真实鼠标点击)"""
    try:
        page.evaluate(
            """() => {
                document.querySelectorAll('button').forEach(b => {
                    if ((b.innerText || '').trim() === 'Pay Now') {
                        b.disabled = true; b.style.display = 'none';
                    }
                });
                document.querySelectorAll('div').forEach(d => {
                    const t = (d.innerText || '');
                    if (t.includes('Ad blocker detected')) {
                        const s = getComputedStyle(d);
                        if (s.position === 'fixed') d.style.display = 'none';
                    }
                });
            }"""
        )
    except Exception:
        pass

def login(page):
    # 1. Cookie 登录尝试
    if COOKIE_VALUE:
        log("📇 尝试 Cookie 登录...")
        try:
            page.context.add_cookies([{
                'name': 'remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d',
                'value': COOKIE_VALUE,
                'domain': 'dash.hidencloud.com',
                'path': '/',
                'expires': int(time.time()) + 3600 * 24 * 365,
                'httpOnly': True,
                'secure': True,
                'sameSite': 'Lax'
            }])
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            page_title = page.title()
            log(f"📝 当前Title: {page_title}")
            if "auth/login" not in page.url:
                log(f"✅ Cookie 登录成功！当前已到达dashboard页面")
                return True
            log("❌ Cookie 失效，改用账号密码登录")
        except Exception as e:
            log(f"⚠️ Cookie 登录异常: {e}")

    # 2. 账号密码登录（新版表单字段为 username，支持邮箱或用户名）
    if not EMAIL or not PASSWORD:
        return False
    for attempt in range(1, 4):
        log(f"💣 尝试账号密码登录（第 {attempt} 次）...")
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            page.wait_for_selector('input[name="username"]', state="visible", timeout=120000)
            log("🔑 登录表单已出现，填写账号...")
            page.fill('input[name="username"]', EMAIL)
            page.fill('input[name="password"]', PASSWORD)
            try:
                page.check('input[name="remember"]')
            except Exception:
                pass
            time.sleep(0.5)
            handle_cloudflare(page)
            # 必须等 Turnstile token 生成后再提交，否则报 cf-turnstile-response required
            wait_turnstile_token(page, 90)
            page.click('button[type="submit"]')
            time.sleep(3)
            handle_cloudflare(page)
            for _ in range(30):
                if "auth/login" not in page.url:
                    break
                time.sleep(2)
            if "auth/login" not in page.url:
                log(f"✅ 账号密码登录成功！")
                return True
            log(f"⚠️ 第 {attempt} 次登录失败，仍在登录页")
            try:
                page.screenshot(path="login_fail.png")
            except Exception:
                pass
            time.sleep(10)
        except Exception as e:
            log(f"❌ 登录异常: {e}")
            try:
                page.screenshot(path="login_fail.png")
            except Exception:
                pass
            time.sleep(5)
    return False

def get_server_id(page):
    try:
        handle_cloudflare(page)
        if not wait_text(page, "Due date", 60):
            # 当前页可能不是 dashboard,主动跳过去再取一次
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            wait_text(page, "Due date", 60)
        time.sleep(3)
        html = page.content()
        log(f"📝 页面长度: {len(html)}, URL: {page.url}")

        # 方案1: 从 href 链接中提取 /service/数字/manage
        matches = re.findall(r'/service/(\d+)/manage', html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从链接中获取到 Server ID: {server_id}")
            return server_id

        # 方案2: 从 span 标签中提取 #数字 (如 "Free Server #218079")
        matches = re.findall(r'#(\d{4,})', html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从文本 #号中获取到 Server ID: {server_id}")
            return server_id

        log("❌ 所有 URL 均未找到 Server ID")
        return None
    except Exception as e:
        log(f"❌ 获取 Server ID 失败: {e}")
        page.screenshot(path="server_id_error.png")
        return None

def goto_service_page(page):
    """(重新)加载服务管理页并等待渲染完成。
    每次都强制 goto: 一是为了拿到最新到期数据,二是让重试轮次从干净页面开始
    (上一轮可能留下了打开的弹窗)。"""
    page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
    handle_cloudflare(page)
    neutralize_traps(page)
    if not wait_text(page, "Due date", 90):
        log("⚠️ 等待页面渲染 'Due date' 超时")
        return False
    return True

def parse_due_date(page):
    """从服务管理页解析 Due date(页面同时存在 Due date / Last renewal date / Next Invoice,
    正则以 'Due date' 为锚点紧跟的日期,不会误取其它两个)"""
    body_text = page.locator("body").inner_text()
    patterns = [
        r"Due date\s*\n\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        r"Due date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        r"Current Due Date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        r"expires on the\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return "未知"

def get_due_date(page):
    try:
        if not goto_service_page(page):
            return "未知"
        due = parse_due_date(page)
        log(f"📅 获取到Due Date: {due}")
        return due
    except Exception as e:
        log(f"❌ 获取Due Date失败: {e}")
        return "未知"

def create_invoice(page, server_id, max_rounds=4):
    """打开 Renew 弹窗并创建续期发票。
    成功的唯一标志: URL 跳转到 /payment/invoice/<uuid>(发票真正创建)。
    返回发票 URL;未到续期时间返回 'NOT_TIME';失败返回 None。"""
    for rnd in range(1, max_rounds + 1):
        log(f"🔄 创建发票 第 {rnd}/{max_rounds} 轮")
        try:
            if not goto_service_page(page):
                continue
            time.sleep(2)

            # 未到续期时间检测（站点会在到期前 N 天外限制续期）
            page_text = page.locator("body").inner_text()
            if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
                log("⏳ 未到续期时间，无法续期。")
                page.screenshot(path="renew_not_allowed.png")
                return "NOT_TIME"

            # 1. 打开 Renew 弹窗
            ok = js_click_exact(page, "Renew", "Renew")
            if ok != "CLICKED":
                log(f"⚠️ 未找到 Renew 按钮，重试...")
                continue

            # 2. 等待弹窗真正打开
            modal_open = False
            for _w in range(15):
                time.sleep(2)
                opened = page.evaluate(
                    """(sid) => {
                        const c = document.getElementById('renewService-' + sid);
                        return c ? !c.classList.contains('hidden') : false;
                    }""", server_id)
                if opened:
                    modal_open = True
                    break
            if not modal_open:
                log(f"⚠️ 第 {rnd} 轮 Renew 弹窗未打开，重试...")
                try:
                    page.screenshot(path=f"renew_modal_failed_r{rnd}.png")
                except Exception:
                    pass
                continue

            log("✅ Renew 弹窗已打开！")
            time.sleep(2)
            try:
                page.screenshot(path="renew_modal_open.png")
            except Exception:
                pass

            # 3. 等待弹窗内 Turnstile token（弹窗打开后 Turnstile 才开始运行）
            wait_modal_turnstile(page, server_id, 90)

            # 4. 点击 Create Invoice（精确匹配，限定在弹窗内）
            ok = js_click_exact(page, "Create Invoice", "Create Invoice",
                                within=f"#renewService-{server_id}")
            if ok != "CLICKED":
                ok = js_click_exact(page, "Create Invoice", "Create Invoice(全局)")
            if ok != "CLICKED":
                log(f"⚠️ 第 {rnd} 轮未找到 Create Invoice 按钮，重试...")
                continue

            # 5. 等待跳转发票页 —— 发票创建成功的唯一可信标志
            try:
                page.wait_for_url("**/payment/invoice/**", timeout=60000)
                log(f"🎉 发票创建成功，已跳转: {page.url}")
                return page.url
            except Exception:
                log(f"⚠️ 第 {rnd} 轮点击 Create Invoice 后 60s 未跳转发票页")
                try:
                    page.screenshot(path=f"invoice_failed_r{rnd}.png")
                except Exception:
                    pass
        except Exception as e:
            log(f"❌ 创建发票异常: {e}")
            try:
                page.screenshot(path=f"invoice_error_r{rnd}.png")
            except Exception:
                pass
    return None

def find_unpaid_invoice(page, server_id):
    """兜底: 在未支付发票列表中查找本服务的续期发票链接。
    用于上一轮运行创建发票成功但支付失败的情况,直接支付旧发票即可完成续期。"""
    try:
        page.goto(f"{BASE_URL}/invoices?where=unpaid", wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        neutralize_traps(page)
        wait_text(page, "Invoice", 60)
        time.sleep(3)
        links = page.evaluate(
            """(sid) => {
                const out = [];
                document.querySelectorAll('a[href*="/payment/invoice/"]').forEach(a => {
                    const row = a.closest('tr, li, div');
                    const ctx = row ? (row.innerText || '') : '';
                    if (!ctx || ctx.includes('#' + sid)) out.push(a.href);
                });
                return out;
            }""", server_id)
        if links:
            log(f"🔎 发现未支付发票: {links[0]}")
            return links[0]
        log("ℹ️ 没有本服务的未支付发票")
    except Exception as e:
        log(f"⚠️ 查找未支付发票失败: {e}")
    return None

def pay_invoice(page, invoice_url, timeout_s=120):
    """在发票页点击 'Pay' 完成支付(€0.00 续期发票无需支付方式)。
    只精确匹配文本恰为 'Pay' 的按钮;'Pay Now' 是充值按钮,已在 neutralize_traps 中禁用。
    支付成功的标志: 跳回 dashboard 并显示 'Your payment has been completed'。"""
    try:
        page.goto(invoice_url, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        neutralize_traps(page)

        start = time.time()
        last_click = 0
        while time.time() - start < timeout_s:
            try:
                body = page.locator("body").inner_text()
                if "Your payment has been completed" in body:
                    log("✅ 支付成功提示已出现！")
                    return True
            except Exception:
                pass
            # 仍在发票页说明支付尚未完成: 每 15s 允许补点一次 Pay
            # (按钮可能还没渲染出来,或首次点击被弹窗拦截)
            if "/payment/invoice/" in page.url and time.time() - last_click > 15:
                ok = js_click_exact(page, "Pay", "发票页 Pay")
                if ok == "CLICKED":
                    last_click = time.time()
                    log("⏳ 已点击 Pay，等待支付结果...")
            time.sleep(3)

        body = page.locator("body").inner_text()
        if "Your payment has been completed" in body:
            log("✅ 支付成功提示已出现！")
            return True
        log("❌ 等待支付结果超时")
        page.screenshot(path="pay_failed.png")
        return False
    except Exception as e:
        log(f"❌ 支付异常: {e}")
        try:
            page.screenshot(path="pay_error.png")
        except Exception:
            pass
        return False

def main():
    # 检查必要环境变量
    if not COOKIE_VALUE and not (EMAIL and PASSWORD):
        log("❌ 缺少登录凭证")
        sys.exit(1)

    global SERVICE_URL

    with sync_playwright() as p:
        try:
            if IS_PROXY:
                log(f"⚙️ 代理已启用: {PROXY_SERVER}")
            else:
                log("🌐 直连模式（未使用代理）")

            # 获取当前出口ip
            current_ip = get_current_ip(PROXY_SERVER)
            log(f"🎯 当前出口IP: {current_ip}")

            log("🚀 启动浏览器...")
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars']
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                proxy={"server": PROXY_SERVER} if IS_PROXY else None
            )
            page = context.new_page()
            page.add_init_script(STEALTH_JS)

            if not login(page):
                send_telegram_notification("❌ 续期失败(登录失败)", "未知", "未知")
                sys.exit(1)

            # 登录成功后，自动获取 Server ID
            server_id = get_server_id(page)
            if not server_id:
                log("❌ 无法获取 Server ID，退出。")
                send_telegram_notification("❌ 续期失败(未获取到 Server ID)", "未知", "未知")
                sys.exit(1)
            SERVICE_URL = f"{BASE_URL}/service/{server_id}/manage"

            # 获取旧到期时间
            old_due = get_due_date(page)
            log(f"📆 续费前到期时间：{old_due}")

            # 执行续期: 创建发票
            invoice_url = create_invoice(page, server_id)

            if invoice_url == "NOT_TIME":
                log("⏳ 未到续期时间，目前无法续期")
                send_telegram_notification("⏳ 未到续期时间", old_due, old_due)
                sys.exit(0)

            if not invoice_url:
                # 兜底: 查找上一轮运行可能遗留的未支付发票,直接支付
                log("ℹ️ 创建发票未成功，尝试查找遗留的未支付发票...")
                invoice_url = find_unpaid_invoice(page, server_id)

            if not invoice_url:
                log("❌ 未能获得可支付的发票，续期失败。")
                send_telegram_notification("❌ 续期失败(发票创建失败)", old_due, old_due)
                sys.exit(1)

            # 支付发票
            pay_ok = pay_invoice(page, invoice_url)

            # 支付后回到服务页核对到期日是否真的延长 —— 这是判断成功的最终标准
            time.sleep(5)
            new_due = get_due_date(page)
            log(f"📆 续费后到期时间：{new_due}")

            if (pay_ok and new_due != "未知" and old_due != "未知"
                    and new_due != old_due):
                status = "✅ 续期成功"
            elif new_due != "未知" and old_due != "未知" and new_due != old_due:
                status = "✅ 续期成功"
            elif old_due == "未知" or new_due == "未知":
                log("⚠️ 到期日读取失败，无法确认续期结果")
                status = "⚠️ 无法确认(到期日读取失败)"
            else:
                log("❌ 支付动作已执行但到期日未变化，续期失败")
                status = "❌ 续期失败(到期日未变化)"

            # 发送 Telegram 通知
            send_telegram_notification(status, old_due, new_due)

            if status == "✅ 续期成功":
                sys.exit(0)
            elif status.startswith("⚠️"):
                # 无法确认结果时以失败退出,让 Actions 红灯提醒人工核对
                sys.exit(1)
            else:
                sys.exit(1)
        except Exception as e:
            log(f"❌ 浏览器启动出错: {e}")
            sys.exit(1)
        finally:
            if 'browser' in locals() and browser:
                browser.close()

if __name__ == "__main__":
    main()
