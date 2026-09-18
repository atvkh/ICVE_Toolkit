"""登录模块:全自动滑块登录(账密 → 自动过阿里云滑块)+ 人工回调兜底。

登录新账号的流程:
1. 终端输入账密 → slider_auto 全自动过阿里云滑块 → 拦截 userLoginV2 响应拿 sso_token
2. 用 sso_token 换 bearer_token → apply_token 拉用户信息 → 入库
3. 自动登录 2 次全失败 → 回退人工流程(原生 9527 回调,原样保留):
   启动本地 HTTPServer(127.0.0.1:9527),打印 SSO 链接由用户在浏览器手动登录,
   SSO 重定向回调带 token 参数,再走同样的换 token 流程
"""

import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse, parse_qs

from zjy_client import ZjyClient, BASE_URL, SSO_BASE, session, _extract_access_token
from accounts import save_account, load_accounts, list_accounts
from utils import log

# 回调服务端口
CALLBACK_PORT = 9527
CALLBACK_HOST = "127.0.0.1"
CALLBACK_URL = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}"

# 登录超时(秒)
LOGIN_TIMEOUT = 300

# 全局变量:回调捕获到的 token
_captured_token: Optional[str] = None


class CallbackHandler(BaseHTTPRequestHandler):
    """HTTP 回调处理器:接收 SSO 重定向的 token 参数。"""

    def do_GET(self):
        global _captured_token
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "token" in params:
            _captured_token = params["token"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML.encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_WAITING_HTML.encode("utf-8"))

    def log_message(self, format, *args):
        """静默 HTTP 访问日志。"""
        pass


def login() -> Optional[ZjyClient]:
    """登录入口,返回已认证的 ZjyClient。

    流程:
    1. 若有已保存账号,先尝试用保存的 token 复用登录
    2. 无可用保存账号时,登录新账号(自动滑块优先,人工回调兜底)

    :return: ZjyClient 实例或 None(登录失败/超时)
    """
    # 步骤1: 尝试用已保存的账号登录
    client = _try_saved_accounts()
    if client:
        return client

    # 步骤2: 启动浏览器登录
    return _browser_login()


def _try_saved_accounts() -> Optional[ZjyClient]:
    """尝试用已保存的 token 登录,成功返回 client。"""
    accounts = list_accounts()
    if not accounts:
        return None

    log("\n  已保存的账号:", "INFO")
    for i, (name, info) in enumerate(accounts):
        print(f"  [{i + 1}] {name} ({info.get('userName', '?')})", flush=True)
    print(f"  [0] 登录新账号", flush=True)

    choice = input(f"\n  选择账号 [0-{len(accounts)}]: ").strip()
    if not choice.isdigit():
        return None
    idx = int(choice)
    if idx == 0:
        return None
    if idx < 1 or idx > len(accounts):
        log("无效选择", "WARNING")
        return None

    name, info = accounts[idx - 1]
    saved_token = info.get("token", "")
    saved_sso = info.get("ssoToken", "")

    if not saved_token:
        log(f"  {name} 的 Token 为空,请重新登录", "WARNING")
        return None

    log(f"  正在验证 {name} 的 Token...", "INFO")
    client = ZjyClient(sso_token=saved_sso or None)
    if client.apply_token(saved_token):
        log(f"  登录成功!{client.user_info.get('nickName')} / 学号: {client.user_info.get('userName')}", "SUCCESS")
        return client

    # token 过期,尝试用 sso_token 刷新
    if saved_sso:
        log(f"  Token 已过期,尝试用 SSO Token 刷新...", "INFO")
        client = ZjyClient(sso_token=saved_sso)
        if client.refresh_token_from_sso():
            # 刷新成功,更新入库
            save_account(client, saved_sso)
            log(f"  刷新成功!{client.user_info.get('nickName')} / 学号: {client.user_info.get('userName')}", "SUCCESS")
            return client

    log(f"  Token 已过期,请重新登录", "WARNING")
    # 删除过期账号
    from accounts import delete_account
    delete_account(name)
    return None


def _input_password() -> str:
    """读密码:明文回显(本地 CLI 工具,输入可见便于确认)。"""
    return input("  密码: ").strip()


def _prompt_credentials() -> Optional[tuple]:
    """终端输入账密;账号留空表示跳过自动登录,直接走人工流程。

    :return: (user, pwd) 或 None
    """
    print("\n  正在后台准备自动登录环境(浏览器窗口已隐藏,无需理会),请输入账密:", flush=True)
    user = input("  账号(直接回车 = 改用浏览器人工登录): ").strip()
    if not user:
        return None
    pwd = _input_password()
    if not pwd:
        log("  密码为空,跳过自动登录", "WARNING")
        return None
    return user, pwd


def _auto_slider_login() -> Optional[str]:
    """全自动滑块登录(自动尝试 2 次),返回 sso_token 或 None。

    先检测依赖:缺失时询问自动安装,装不上直接转人工,不让用户白输账密。
    依赖就绪后启动后台预热(隐藏的浏览器加载自建登录页并预弹滑块),
    再让用户在 CLI 输账密——预热与输账密并行,输完回车约 1~1.5 秒完成登录。
    """
    try:
        import slider_auto
        from slider_auto import AutoSlider
    except Exception as e:
        log(f"  自动登录模块不可用({e}),转入人工登录", "WARNING")
        return None
    # 依赖关前置:缺依赖时先问是否自动安装,避免用户输完账密才被告知装不了
    if not slider_auto.deps_ready():
        missing = slider_auto.missing_packages()
        log(f"  [自动登录] 缺少依赖: {' '.join(missing)}", "WARNING")
        ans = input("  是否自动安装缺失依赖(需联网,含浏览器内核约 300MB)? [Y/n]: ").strip().lower()
        if ans not in ('', 'y', 'yes'):
            log("  跳过自动安装,转入人工登录", "INFO")
            return None
        if not slider_auto.auto_install_deps():
            log("  依赖安装失败,转入人工登录", "WARNING")
            return None
        log("  依赖安装完成", "SUCCESS")
    sess = AutoSlider()
    sess.start()
    try:
        creds = _prompt_credentials()
        if not creds:
            return None
        try:
            return sess.obtain_sso_token(*creds, max_attempts=2)
        except Exception as e:
            log(f"  自动登录异常: {e}", "ERROR")
            return None
    finally:
        sess.close()


def _browser_login() -> Optional[ZjyClient]:
    """登录新账号:先全自动滑块登录,失败回退 9527 人工回调流程。"""
    global _captured_token
    _captured_token = None

    # 步骤1:全自动滑块登录(账密 → 自动过滑块 → sso_token → bearer_token)
    sso_token = _auto_slider_login()
    if sso_token:
        client = _exchange_token(sso_token)
        if client:
            return client
        log("  自动登录换取 Token 失败,转入人工登录", "WARNING")
    else:
        log("  自动滑块登录未成功,转入人工登录(浏览器回调)", "WARNING")

    # 步骤2:人工兜底 —— 原生 9527 回调流程原样保留
    sso_url = f"{SSO_BASE}/h5/?mode=simple&source=15&redirect={CALLBACK_URL}#/login"
    log(f"\n  请在浏览器中打开以下链接登录:", "INFO")
    print(f"  {sso_url}\n", flush=True)
    log("  正在尝试自动打开浏览器...", "INFO")
    try:
        webbrowser.open(sso_url)
    except Exception:
        log("  无法自动打开浏览器,请手动复制链接到浏览器", "WARNING")

    log(f"  等待登录回调...(超时 {LOGIN_TIMEOUT} 秒)", "INFO")

    # 启动 HTTPServer 等待回调
    try:
        server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), CallbackHandler)
    except OSError as e:
        log(f"  启动回调服务失败(端口 {CALLBACK_PORT} 可能被占用): {e}", "ERROR")
        log("  请关闭占用该端口的程序后重试,或修改 auth.py 中的 CALLBACK_PORT", "ERROR")
        return None

    server.timeout = 1
    for _ in range(LOGIN_TIMEOUT):
        server.handle_request()
        if _captured_token:
            break
    server.server_close()

    if not _captured_token:
        log("  登录超时,请重新尝试", "WARNING")
        return None

    # 用捕获的 sso_token 换 bearer_token
    return _exchange_token(_captured_token)


def _exchange_token(sso_token: str) -> Optional[ZjyClient]:
    """用 sso_token 换 bearer_token 并完成认证。

    :param sso_token: SSO 登录回调获取的 token
    :return: ZjyClient 实例或 None
    """
    try:
        resp = session.get(
            f"{BASE_URL}/auth/passLogin",
            params={"token": sso_token},
            timeout=15,
        )
        if resp.status_code != 200:
            log(f"  换取 Token 失败: HTTP {resp.status_code}", "ERROR")
            return None
        bdata = resp.json()
    except Exception as e:
        log(f"  换取 Token 异常: {e}", "ERROR")
        return None

    bearer_token = _extract_access_token(bdata)
    if not bearer_token:
        log(f"  换取 Token 失败: {bdata.get('msg', '未知错误')}", "ERROR")
        return None

    # apply_token 拉用户信息 + AI 域鉴权
    client = ZjyClient(sso_token=sso_token)
    if not client.apply_token(bearer_token):
        log("  Token 无效,登录失败", "ERROR")
        return None

    # 持久化账号
    save_account(client, sso_token)
    log(f"\n  登录成功!{client.user_info.get('nickName')} / 学号: {client.user_info.get('userName')}", "SUCCESS")
    return client


# ==================== HTML 模板 ====================

_SUCCESS_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>登录成功</title></head>
<body style="text-align:center;padding-top:100px;font-family:sans-serif">
  <h2 style="color:green">登录成功!</h2>
  <p>Token已获取,请返回命令行继续</p>
  <p style="color:#999;font-size:14px">请手动关闭本窗口</p>
</body></html>"""

_WAITING_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>等待登录</title></head>
<body style="text-align:center;padding-top:100px;font-family:sans-serif;background:#f5f5f5">
  <div style="background:#fff;padding:40px 60px;border-radius:12px;display:inline-block;box-shadow:0 2px 8px rgba(0,0,0,0.1)">
    <h2 style="color:#2196F3;margin-bottom:10px">等待登录中</h2>
    <p style="color:#666">请在页面上完成登录操作</p>
  </div>
</body></html>"""
