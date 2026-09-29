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
        f"🎉 HidenCloud 续期通知\n\n"
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
    if page.locator(iframe_selector).count() > 0:
        log("⚠️ 检测到 Cloudflare 验证...")
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

def js_click(page, text, desc, within=None):
    """DOM 级点击（按文本匹配）。新版站点多个 modal 背景层可能互相拦截真实鼠标点击，
    JS 点击可绕过遮挡。"""
    ok = page.evaluate(
        """(arg) => {
            const root = arg.within ? document.querySelector(arg.within) : document;
            if (!root) return 'NO_ROOT';
            const els = [...root.querySelectorAll('button, a')];
            const el = els.find(e => (e.innerText || '').trim().includes(arg.text)
                                    && e.offsetParent !== null && !e.disabled);
            if (!el) return 'NOT_FOUND';
            el.click();
            return 'CLICKED';
        }""",
        {"text": text, "within": within},
    )
    log(f"🖱️ JS点击 {desc}: {ok}")
    return ok

def isolate_modal(page, keep_id):
    """隐藏除 keep_id 外的所有 modal 容器。
    站点 bug：多个 modal 容器可能同时处于打开状态，透明背景层会拦截点击。"""
    page.evaluate(
        """(kid) => {
            document.querySelectorAll('div[data-modal-backdrop]').forEach(d => {
                if (d.id === kid) {
                    d.classList.remove('hidden'); d.classList.add('flex');
                    d.style.display = 'flex';
                } else {
                    d.classList.add('hidden'); d.classList.remove('flex');
                    d.style.display = 'none';
                }
            });
        }""", keep_id,
    )

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
            log("❌ Cookie 失效，请更换")
        except:
            pass

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

def get_due_date(page):
    """新版页面同时存在 'Due date' 信息卡与 'Current Due Date' 弹窗文本，两种都兼容"""
    try:
        if SERVICE_URL not in page.url:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        if not wait_text(page, "Due date", 90):
            log("⚠️ 等待页面渲染 'Due date' 超时")
        body_text = page.locator("body").inner_text()
        patterns = [
            r"Due date\s*\n\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Current Due Date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"expires on the\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
            if match:
                due_date = match.group(1).strip()
                log(f"📅 获取到Due Date: {due_date}")
                return due_date
    except Exception as e:
        log(f"❌ 获取Due Date失败: {e}")
    return "未知"

def renew_service(page, server_id):
    """新版续期流程: Renew -> (弹窗) Create Invoice -> 发票页 Pay。
    站点弹窗状态管理有 bug（多个 modal 同时打开/透明背景层拦截点击/JS 事件未挂载），
    因此采用: 多轮重试 + 等待真实渲染 + isolate_modal 清理遮挡 + JS 级点击。"""
    try:
        log("➡ 进入续期流程...")

        for rnd in range(1, 5):
            log(f"🔄 续期尝试 第 {rnd}/4 轮")
            if page.url != SERVICE_URL:
                page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            if not wait_text(page, "Due date", 90):
                log("⚠️ 服务页渲染超时，重新加载")
                continue
            time.sleep(3)

            log("🖱️ 准备点击 'Renew' 按钮...")
            js_click(page, "Renew", "Renew")

            # 未到续期时间检测（新版站点点击后若受限会显示 Renewal Restricted 提示）
            time.sleep(3)
            try:
                page_text = page.locator("body").inner_text()
                if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
                    log("⚠️ 未到续期时间，无法续期。")
                    page.screenshot(path="renew_not_allowed.png")
                    return "NOT_TIME"
            except Exception:
                pass

            # 等待弹窗内 Create Invoice 可见
            modal_ok = False
            for _w in range(12):
                time.sleep(2.5)
                state = page.evaluate(
                    """(sid) => {
                        const cont = document.getElementById('renewService-' + sid);
                        const ci = [...document.querySelectorAll('button')]
                            .find(b => (b.innerText||'').includes('Create Invoice'));
                        return { ciVisible: ci ? (ci.offsetParent !== null) : false,
                                 contHidden: cont ? cont.classList.contains('hidden') : true,
                                 contStyle: cont ? (cont.style.display || '') : '' };
                    }""", server_id)
                if state.get("ciVisible"):
                    modal_ok = True
                    break
                # 兜底: 站点认为弹窗已开但容器被藏住 -> 强制显示
                if state.get("contHidden") or state.get("contStyle") == "none":
                    page.evaluate(
                        """(sid) => {
                            const c = document.getElementById('renewService-' + sid);
                            if (c) { c.classList.remove('hidden'); c.classList.add('flex');
                                     c.style.display = 'flex'; }
                        }""", server_id)

            if not modal_ok:
                log(f"⚠️ 第 {rnd} 轮弹窗未出现，重试...")
                try:
                    page.screenshot(path=f"renew_modal_failed_r{rnd}.png")
                except Exception:
                    pass
                continue

            log("✅ 续期弹窗已成功弹出！")
            time.sleep(1)
            # 隐藏其他 modal 容器（deleteService 等），保留 renewService，消除遮挡
            isolate_modal(page, f"renewService-{server_id}")
            try:
                page.screenshot(path="renew_modal_open.png")
            except Exception:
                pass

            log("🖱️ 点击 'Create Invoice'...")
            ok = js_click(page, "Create Invoice", "Create Invoice",
                          within=f"#renewService-{server_id}")
            if ok != "CLICKED":
                js_click(page, "Create Invoice", "Create Invoice(全局)")

            # 等待跳转发票页 或 弹窗内出现 Pay Now
            pay_clicked = False
            start_wait = time.time()
            while time.time() - start_wait < 120:
                if "/payment/invoice/" in page.url:
                    log(f"🎉 页面已跳转: {page.url}")
                    break
                try:
                    pn = page.locator('button:has-text("Pay Now")')
                    if pn.count() and pn.first.is_visible():
                        log("💳 弹窗内出现 'Pay Now'，点击...")
                        js_click(page, "Pay Now", "Pay Now")
                        pay_clicked = True
                        time.sleep(6)
                        break
                except Exception:
                    pass
                time.sleep(2)

            # 发票页点击 Pay
            if "/payment/invoice/" in page.url:
                handle_cloudflare(page)
                time.sleep(3)
                try:
                    page.screenshot(path="invoice_page.png")
                except Exception:
                    pass
                if wait_text(page, "Pay", 60):
                    log("🔎 查找并点击 'Pay' 按钮...")
                    js_click(page, "Pay", "发票页 Pay")
                    pay_clicked = True
                    time.sleep(8)
                    try:
                        page.screenshot(path="after_pay.png")
                    except Exception:
                        pass
                else:
                    log("⚠️ 发票页未出现 Pay 按钮")

            if pay_clicked:
                return True
            log(f"⚠️ 第 {rnd} 轮未完成支付，重新尝试...")

        log("❌ 错误：多轮尝试后，续费流程仍未完成。")
        page.screenshot(path="renew_failed.png")
        return False

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        page.screenshot(path="renew_error.png")
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
                sys.exit(1)

            # 登录成功后，自动获取 Server ID
            server_id = get_server_id(page)
            if not server_id:
                log("❌ 无法获取 Server ID，退出。")
                sys.exit(1)
            SERVICE_URL = f"{BASE_URL}/service/{server_id}/manage"

            # 获取旧到期时间
            old_due = get_due_date(page)
            log(f"📆 续费前到期时间：{old_due}")

            # 执行续费
            renew_result = renew_service(page, server_id)

            new_due = old_due
            if renew_result == "NOT_TIME":
                log("⏳ 未到续期时间，目前无法续期")
                status = "⏳ 未到续期时间"
            elif renew_result is False:
                log("❌ 续费失败，脚本退出。")
                status = "❌ 续期失败"
            else:  # renew_result is True
                new_due = get_due_date(page)
                log(f"📆 续费后到期时间：{new_due}")
                if new_due != old_due and new_due != "未知":
                    status = "✅ 续期成功"
                else:
                    log("⚠️ 支付动作已执行但到期日未变化，请人工核对")
                    status = "⚠️ 支付已执行，请核对"

            # 发送 Telegram 通知
            send_telegram_notification(status, old_due, new_due)

            if renew_result == "NOT_TIME":
                sys.exit(0)
            elif renew_result is False:
                sys.exit(1)
            else:
                sys.exit(0)
        except Exception as e:
            log(f"❌ 浏览器启动出错: {e}")
            sys.exit(1)
        finally:
            if 'browser' in locals() and browser:
                browser.close()

if __name__ == "__main__":
    main()
