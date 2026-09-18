"""全自动滑块登录模块:账密 → 阿里云滑块 → sso_token(零人工)。

架构(实测:预热 ~0.3s,回车→判定 ~1.0s):
  1. 预热(与 CLI 输账密并行):同源加载自建极简登录页(robots.txt + set_content,
     无协议框/无登录tab/无推广弹窗);拼图图到齐即真鼠标点登录键弹滑块,并在此期
     完成鼠标热身
  2. 提交账密:JS 注入到自建页输入框(自建页无表单行为评分,瞬时完成)
  3. 拖拽:白帽+Canny 双证据识别缺口 → 二次映射(left = 0.00355·d² + 0.0765·d,系数在线自校准)
     反解拖距 → 真人形态轨迹 → 闭环读 #aliyunCaptcha-puzzle 收敛 <0.7px(上限 CORR_MAX 轮)
  4. verify 回调拿 captchaVerifyParam(cvp)后,页面立即 fetch userLogin(单次使用,
     非重放)→ 拦截响应取 data.token(sso_token)
  5. 兜底:自建页流程失败(页面改版/场景漂移)→ 走真实 SSO 登录页全流程一次 →
     上层再失败转 9527 人工回调

实证铁律(踩坑换来的,勿改):
  - 行为链 > 轨迹形态:拖拽必须真鼠标 page.mouse.move,mousemove 历史不能省
    (缺失会显著推高 F001 行为风控拦截)。热身可搬到预热期,但 mouse.down 前
    仍须有 approach 的几次 move 作为紧邻历史。
  - 弹窗必须由**真鼠标**点击登录键开启:页面内 JS 合成 click 无效(实测无 -verify
    请求、弹窗不渲染),所以"等拼图到齐→立刻真点"才是开启路径,不是兜底。
  - 闭环校正不能省:2026-09-18 实测去掉后 F015 由 18% 升到 40%(它兜的是二次映射
    残差);但校正救不了识别偏差,故设轮数上限,别拿它精修一个算错的靶心。
  - 拖拽总时长不是 F001 的主导因素:实测 0.22s~0.95s 全档 15 样本零 F001。
  - captchaVerifyParam 一次性不可重放:回调内立即使用,禁止存储复用。
  - 同 IP 高频尝试会触发风控惩罚(响应延迟 5s+),失败后须退避等待。
  - 主线程内等待事件(hook/响应)必须走 Playwright 调用推进事件循环:纯 time.sleep
    会饿死 response 分发,表现为"永远等不到判定"(实测假象 e2e=21s)。
  - 映射自校准:在线重拟合仅接受通过质量/合理性校验的拟合,异常时死守经验初值。
"""

import importlib
import io
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from utils import log

# 重依赖缺失不致命:上层在输账密前先检测,可自动安装;装不上则转人工兜底
try:
    import cv2
    import numpy as np
    from PIL import Image
    from playwright.sync_api import sync_playwright
    _DEPS_MISSING = None
except ImportError as e:
    _DEPS_MISSING = e.name

# ==================== 依赖检测与自动安装 ====================
# 模块名 → pip 包名(scipy 在 gen_track 内部按需导入,也要检测)
_DEP_PACKAGES = (
    ('numpy', 'numpy'),
    ('cv2', 'opencv-python'),
    ('PIL', 'pillow'),
    ('scipy', 'scipy'),
    ('playwright.sync_api', 'playwright'),
)


def missing_packages() -> list:
    """返回缺失的 pip 包名列表(空列表 = 依赖齐全)。"""
    missing = []
    for mod, pkg in _DEP_PACKAGES:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    return missing


def deps_ready() -> bool:
    """依赖是否齐全(实时探测,不依赖模块加载时的旧状态)。"""
    return not missing_packages()


def _bind_deps() -> bool:
    """安装完成后重绑依赖全局量(本模块可能在依赖缺失时已先行导入)。"""
    global np, cv2, Image, sync_playwright, _DEPS_MISSING
    try:
        import cv2 as _cv2
        import numpy as _np
        from PIL import Image as _Image
        from playwright.sync_api import sync_playwright as _sp
    except ImportError as e:
        _DEPS_MISSING = e.name
        return False
    np, cv2, Image, sync_playwright = _np, _cv2, _Image, _sp
    _DEPS_MISSING = None
    return True


def auto_install_deps() -> bool:
    """pip 自动安装缺失依赖(装了 playwright 时顺带下载 chromium 内核)。

    仅源码运行模式有效;打包后的 exe 无 pip,直接返回 False。

    :return: True = 依赖已可用;False = 安装失败或环境不支持,应转人工登录
    """
    if getattr(sys, 'frozen', False):
        log('  [自动登录] 打包环境无 pip,无法自动安装依赖', 'WARNING')
        return False
    pkgs = missing_packages()
    if not pkgs:
        return True
    log(f'  [自动登录] 正在自动安装缺失依赖: {" ".join(pkgs)}(需联网,请耐心等待)', 'INFO')
    try:
        r = subprocess.run([sys.executable, '-m', 'pip', 'install', *pkgs])
        if r.returncode != 0:
            log('  [自动登录] pip 安装失败,请手动执行: pip install -r requirements.txt', 'WARNING')
            return False
        if 'playwright' in pkgs:
            log('  [自动登录] 正在下载 Chromium 浏览器内核(约 150MB)...', 'INFO')
            r = subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'])
            if r.returncode != 0:
                log('  [自动登录] Chromium 安装失败,请手动执行: playwright install chromium', 'WARNING')
                return False
    except Exception as e:
        log(f'  [自动登录] 自动安装异常: {e}', 'WARNING')
        return False
    return _bind_deps()

# ==================== 实证常量(照抄 bench15,勿调) ====================

LOGIN_URL = ('https://sso.icve.com.cn/sso/auth_v2?mode=simple'
             '&redirect=https%3A%2F%2Fzjy2.icve.com.cn%2Fv2%2Findex&source=15')
SSO_ORIGIN = 'https://sso.icve.com.cn'
CAPTCHA_SCENE_ID = 'e7gyz100'      # SSO 滑块场景 ID(抓包确认的业务常量)
CAPTCHA_PREFIX = '106eu7'          # 阿里云验证码实例前缀(同上)
PUZZLE_SCALE = 300.0 / 296.0       # 拼图显示尺寸(300) / 原图尺寸(296)
# 二次映射:left = A·d² + B·d(实测拟合误差 0.1%)。以下为经验初值,运行时由"映射自校准"
# 用真实(拖距,拼图位移)样本在线重拟合刷新,仅采纳通过质量校验的拟合结果
MAP_A, MAP_B = 0.00355, 0.0765

# 拖拽节奏(2026-09-18 自建页实测标定)。拖拽墙钟 ≈ pre_roll + dur + pause,
# dur 只改"距离沿时间怎么摊",不改帧数;真正决定耗时的是帧间隔与前置/中段停顿。
DRAG_DUR_S = (0.28, 0.38)       # 标称时长(秒);实测 0.22s 起零 F001
DRAG_STEP_MS = (13.0, 18.0)     # 帧间隔;单次 move 派发往返 ~6ms,再压会被派发耗时兜住
DRAG_PRE_MS = (20.0, 45.0)      # 起手前置(按下后到第一帧的距离曲线起点)
DRAG_PAUSE_S = (0.05, 0.10)     # 中段犹豫时长
CORR_MAX = 4                    # 闭环校正轮数上限:超过即认定靶心算错,再精修只是白等
DRAG_WATCHDOG_S = 2.0           # 拖拽墙钟硬超时(相对标称):单次 move 被页面卡顿拖住时快速弃轮

VIEWPORT = {'width': 1280, 'height': 850}
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
# 有头窗口挪到屏幕外:保持实证过的有头指纹,又不干扰用户;
# 后两个开关禁用 Chromium 对离屏/遮挡窗口的渲染节流(避免滑块动画被降频)。
# 注:离屏窗口创建时仍会被系统激活抢走终端焦点,由 _restore_console_focus 抢回
LAUNCH_ARGS = [
    '--window-position=-32000,-32000',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-features=CalculateNativeWinOcclusion',
]

# playwright 反检测:掩蔽 webdriver 指纹
_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = window.chrome || {runtime: {}};
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

# ==================== 自建极简登录页(主路径) ====================
# 同源加载:滑块几何与真实页一致(拼图图 300×296),二次映射/闭环常数直接复用。
# 弹窗不在页面内自动开(JS 合成 click 打不开 Aliyun 弹窗),由 _setup_mypage 在拼图
# 图到齐后用真鼠标点开——整段都在预热期完成,与用户输账密并行。

_LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { height: 100vh; display: flex; align-items: center; justify-content: center;
         background: linear-gradient(135deg, #e8f0fe 0%, #f5f7fa 100%);
         font-family: "Microsoft YaHei", sans-serif; }
  .card { width: 340px; background: #fff; border-radius: 14px; padding: 36px 32px 30px;
          box-shadow: 0 8px 30px rgba(0,60,120,.12); }
  .title { text-align: center; font-size: 20px; color: #1f2d3d; font-weight: 600; margin-bottom: 6px; }
  .subtitle { text-align: center; font-size: 12px; color: #909399; margin-bottom: 26px; }
  .field { margin-bottom: 16px; }
  .field input { width: 100%; height: 42px; border: 1px solid #dcdfe6; border-radius: 8px;
                 padding: 0 14px; font-size: 14px; outline: none; transition: border .2s; }
  .field input:focus { border-color: #409eff; }
  #captcha-button { width: 100%; height: 44px; border: none; border-radius: 8px;
                    background: #409eff; color: #fff; font-size: 16px; cursor: pointer; margin-top: 6px; }
  #captcha-button:hover { background: #337ecc; }
  .foot { text-align: center; font-size: 11px; color: #c0c4cc; margin-top: 18px; }
</style></head>
<body>
  <div class="card">
    <div class="title">智慧职教</div>
    <div class="subtitle">账号密码登录</div>
    <div class="field"><input id="acc" placeholder="请输入账号" autocomplete="off"></div>
    <div class="field"><input id="pwd" type="password" placeholder="请输入密码"></div>
    <button id="captcha-button">登 录</button>
    <div class="foot">ICVE_Toolkit · 全自动登录</div>
  </div>
  <div id="captcha-element"></div>
  <script>
    window.__cvp = null; window.__login = null;
    async function myVerify(cvp) {
      window.__cvp = cvp;
      const u = document.getElementById('acc').value;
      const w = document.getElementById('pwd').value;
      const r = await fetch('%ORIGIN%/prod-api/v2/user/userLogin', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({type: 1, userName: u, password: w, webPageSource: 1,
                              captchaVerifyParam: cvp, sceneId: '%SCENE%', isNationalLogin: false})
      });
      window.__login = await r.json();
      return {captchaResult: true};
    }
    window.initAliyunCaptcha({
      SceneId: '%SCENE%', mode: 'popup', prefix: '%PREFIX%',
      element: '#captcha-element', button: '#captcha-button',
      captchaVerifyCallback: myVerify,
      onBizValidateCallback: function() {},
    });
    // 合成 .click() 打不开 Aliyun 弹窗(需真实用户激活),弹窗由 Python 侧真鼠标开启
  </script>
</body></html>""".replace('%ORIGIN%', SSO_ORIGIN).replace('%SCENE%', CAPTCHA_SCENE_ID) \
    .replace('%PREFIX%', CAPTCHA_PREFIX)

# 按可见文本精确定位元素中心(JS 合成点击会被忽略,定位后必须走真鼠标)
_JS_FIND = """(sel) => { const e = document.querySelector(sel);
    if (!e) return null; const b = e.getBoundingClientRect();
    return {x: b.x + b.width/2, y: b.y + b.height/2}; }"""

_JS_FIND_INPUT = """(kw) => {
    const e = [...document.querySelectorAll('input')].find(i => (i.placeholder||'').includes(kw));
    if (!e) return null;
    const b = e.getBoundingClientRect();
    return {x: b.x + b.width/2, y: b.y + b.height/2};
}"""

_JS_FIND_BUTTON = """(kw) => {
    const els = [...document.querySelectorAll('button,div,a,span')]
        .filter(e => (e.offsetWidth||e.offsetHeight) && (e.textContent||'').trim()===kw);
    if (!els.length) return null;
    const b = els[els.length-1].getBoundingClientRect();
    return {x: b.x + b.width/2, y: b.y + b.height/2};
}"""

# 微信绑定等推广弹窗的真点击关闭按钮(并非每个账号都弹,机会式处理)
_JS_FIND_DISMISS = """() => {
    for (const kw of ['下次绑定', '不再提醒', '我已知晓']) {
        const els = [...document.querySelectorAll('button,div,a,span')]
            .filter(e => (e.offsetWidth||e.offsetHeight) && (e.textContent||'').trim()===kw);
        if (els.length) {
            const b = els[els.length-1].getBoundingClientRect();
            return {x: b.x + b.width/2, y: b.y + b.height/2};
        }
    }
    return null;
}"""

# verify 错误码语义(实证)
_VERIFY_HINT = {
    'F015': '缺口未对齐(识别偏差)',
    'F001': '位置对但行为风控拦截',
}


# ==================== 缺口识别(双证据融合,照抄 bench15) ====================

def identify_gap(back_bytes: bytes, shadow_bytes: bytes):
    """双证据融合识别缺口 x:白帽亮标记 + Canny 暗洞边缘(F015 主因是单法低 conf 误匹配)。

    只在 shadow alpha 包围盒给出的 y 横带内匹配;缺口 y 由 alpha 直接给出,只识别 x。

    :return: (缺口左缘 x1 原图系, 拼图形状左缘偏移, 置信度 0~1)
    """
    back = np.array(Image.open(io.BytesIO(back_bytes)).convert('RGBA'))
    shadow = np.array(Image.open(io.BytesIO(shadow_bytes)).convert('RGBA'))
    a = shadow[:, :, 3]
    rows = np.where(a.max(axis=1) > 10)[0]
    cols = np.where(a.max(axis=0) > 10)[0]
    shape_x0, y_top, y_bot = float(cols.min()), int(rows.min()), int(rows.max()) + 1
    y0, y1 = max(0, y_top - 8), y_bot + 8
    gray = cv2.cvtColor(back[:, :, :3], cv2.COLOR_RGB2GRAY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (61, 61))
    th = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, k).astype(np.float32) / 255.0
    tmpl = (a[y_top:y_bot, cols.min():cols.max()+1] > 10).astype(np.float32)
    r1 = cv2.matchTemplate(th[y0:y1, :], tmpl, cv2.TM_CCOEFF_NORMED)
    _, c1, _, l1 = cv2.minMaxLoc(r1)
    edges = cv2.Canny(gray[y0:y1, :], 40, 140).astype(np.float32) / 255.0
    a_patch = (a[y_top:y_bot, cols.min():cols.max()+1] > 10).astype(np.uint8)
    tmpl_edge = cv2.Canny(a_patch * 255, 40, 140).astype(np.float32) / 255.0
    if tmpl_edge.sum() < 5:
        tmpl_edge = tmpl
    r2 = cv2.matchTemplate(edges, tmpl_edge, cv2.TM_CCORR_NORMED)
    _, c2, _, l2 = cv2.minMaxLoc(r2)
    if abs(l1[0] - l2[0]) < 10:            # 两法同意:取中点
        gap_x1, conf = (l1[0] + l2[0]) / 2, (c1 + c2) / 2
    else:                                   # 分歧:取白帽,罚置信
        gap_x1, conf = (l1[0], c1) if c1 >= c2 else (l2[0], c2 * 0.8)
    return float(gap_x1), shape_x0, float(conf)


# ==================== 映射自校准(自适应滑块参数漂移) ====================
# 每次拖拽完成即得一个真实观测(拖距 d, 拼图位移 left),与验证通过与否无关——
# 失败尝试同样是有效样本。样本足够且拖距分布充分时,对 left = A·d² + B·d 做最小二乘重拟合,
# 持久化到 slider_calib.json(运行时数据,勿提交);任一质量闸不过则保持经验初值。
# 目的:阿里云调整拖距→位移曲线时自愈,无需重新手工标定。

_CALIB_MIN_SAMPLES = 4      # 起拟合的最小样本数(原 5:真实登录频率低时长期攒不满,映射一直裸奔)
_CALIB_MAX_SAMPLES = 24     # 滑动窗口:只保留最近样本(跟踪最新曲线)
_CALIB_MIN_SPREAD = 15.0    # 拖距分布标准差下限,低于此则二次项不可辨识(原 20)
_CALIB_MAX_RESID = 3.0      # 拟合后平均偏差上限(px)
_CALIB_MIN_GAIN = 2.0       # 相对现用系数至少要改善这么多(px),否则视为拟合噪声
_calib_samples = []         # [(d, left), ...]
_calib_loaded = False
CAL_A, CAL_B = MAP_A, MAP_B


def _calib_path():
    """校准数据文件:打包后放 exe 旁(临时目录重启即失),源码运行放脚本旁。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent / 'slider_calib.json'
    return Path(__file__).resolve().parent / 'slider_calib.json'


def _sane_curve(a: float, b: float) -> bool:
    """校准结果合理性:曲线单调为正,且典型拖距处的位移相对经验值漂移不过分。"""
    if not (1e-5 < a < 1.0 and 0.0 <= b < 2.0):
        return False
    base = MAP_A * 150.0 ** 2 + MAP_B * 150.0    # 典型拖距 150px 处的经验位移
    cur = a * 150.0 ** 2 + b * 150.0
    return 0.4 * base <= cur <= 2.5 * base


def _calib_load():
    """加载历史样本与校准结果(幂等;文件损坏不致命,回退经验初值)。"""
    global _calib_samples, CAL_A, CAL_B, _calib_loaded
    if _calib_loaded:
        return
    _calib_loaded = True
    try:
        data = json.loads(_calib_path().read_text(encoding='utf-8'))
        _calib_samples = [(float(d), float(l)) for d, l in data.get('samples', [])][-_CALIB_MAX_SAMPLES:]
        a, b = float(data.get('a', MAP_A)), float(data.get('b', MAP_B))
        if _sane_curve(a, b):
            CAL_A, CAL_B = a, b
    except Exception:
        pass


def _calib_save():
    try:
        _calib_path().write_text(
            json.dumps({'a': CAL_A, 'b': CAL_B, 'samples': _calib_samples}, ensure_ascii=False),
            encoding='utf-8')
    except Exception:
        pass


def _calib_refit():
    """最小二乘重拟合;样本不足/拖距过集中/未显著优于现值/违反合理性 → 保持当前值。

    实测教训(2026-09-18):d∈[195,255] 这类窄区间上 d² 与 d 高度共线(设计矩阵条件数
    ~3e3),a、b 可互相补偿,拟合会把噪声当真信号——系数 b 曾因此漂 +45% 而平均偏差只
    改善 0.09px。所以"比现状好"必须是显著好,不只是数值上略低。
    """
    global CAL_A, CAL_B
    if len(_calib_samples) < _CALIB_MIN_SAMPLES:
        return
    try:
        ds = np.array([s[0] for s in _calib_samples], dtype=np.float64)
        ls = np.array([s[1] for s in _calib_samples], dtype=np.float64)
        if ds.std() < _CALIB_MIN_SPREAD:            # 拖距分布过集中,二次拟合病态
            return
        coef, *_ = np.linalg.lstsq(np.column_stack([ds ** 2, ds]), ls, rcond=None)
        a, b = float(coef[0]), float(coef[1])
        resid = np.abs((a * ds ** 2 + b * ds) - ls)
        cur = np.abs((CAL_A * ds ** 2 + CAL_B * ds) - ls)   # 现值在同一批样本上的表现
        if (resid.mean() > _CALIB_MAX_RESID                      # 绝对精度不过关
                or resid.mean() > 0.7 * cur.mean()               # 相对现值无实质改善
                or cur.mean() - resid.mean() < _CALIB_MIN_GAIN   # 改善量淹没在噪声里
                or not _sane_curve(a, b)):                       # 违反合理性
            return
        if abs(a - CAL_A) > 1e-6 or abs(b - CAL_B) > 1e-6:
            log(f'  [自动登录] 映射自校准更新: A={a:.5f} B={b:.4f}'
                f'(n={len(ds)}, 平均偏差 {resid.mean():.2f}px, 原 {cur.mean():.2f}px)', 'DEBUG')
        CAL_A, CAL_B = a, b
    except Exception:
        pass


def calib_observe(d: float, left: float):
    """记录一次拖拽观测(闭环收敛后、mouse.up 前调用),自动重拟合 + 持久化。"""
    _calib_load()
    if not (10.0 <= d <= 400.0 and 0.0 <= left <= 300.0):   # 拒绝明显异常样本(如动画未落定)
        return
    _calib_samples.append((round(float(d), 2), round(float(left), 2)))
    del _calib_samples[:-_CALIB_MAX_SAMPLES]
    _calib_refit()
    _calib_save()


def _invert_map(left: float) -> float:
    """位移 → 拖距反解(用当前生效的校准系数)。"""
    _calib_load()
    return (-CAL_B + (CAL_B ** 2 + 4 * CAL_A * left) ** 0.5) / (2 * CAL_A)


# ==================== 真人形态轨迹(形态照抄 bench15;节奏见 DRAG_* 常量) ====================

def gen_track(distance: float, seed=None, dur=None, step_ms=None,
              pre_ms=None, pause_s=None):
    """PCHIP 早峰长尾轨迹:前 25% 时间走 50% 距离,微调期一处停顿,
    末端过冲/欠冲回拉,y 随机游走 clamp ±6px。

    墙钟 = pre_ms 起手 + dur + pause,帧间隔由 step_ms 决定(dur 只改距离分布,
    不改帧数),所以想压缩耗时得同时调 step_ms/pre_ms/pause_s。

    :param distance: 按钮拖动总距离(px)
    :param dur: 主体时长(秒),None 则取 DRAG_DUR_S 区间随机
    :param step_ms: 帧间隔区间(ms),None 取 DRAG_STEP_MS
    :param pre_ms: 起手前置区间(ms),None 取 DRAG_PRE_MS
    :param pause_s: 中段犹豫时长区间(秒),None 取 DRAG_PAUSE_S
    :return: [(t_ms, x, y), ...]
    """
    from scipy.interpolate import PchipInterpolator
    step_lo, step_hi = step_ms or DRAG_STEP_MS
    pre_lo, pre_hi = pre_ms or DRAG_PRE_MS
    pause_lo, pause_hi = pause_s or DRAG_PAUSE_S
    rng = random.Random(seed)
    pts = []
    t = 0.0
    for _ in range(rng.randint(1, 2)):
        pts.append((t, rng.uniform(-0.3, 0.4), rng.uniform(-0.4, 0.4)))
        t += rng.uniform(pre_lo, pre_hi)
    T = dur if dur else rng.uniform(*DRAG_DUR_S)
    over = rng.uniform(0.005, 0.02) * rng.choice([1, -1])
    at = [0.0, rng.uniform(0.18, 0.26), rng.uniform(0.45, 0.58), rng.uniform(0.76, 0.88), 1.0]
    ax = [0.0, rng.uniform(0.48, 0.58), rng.uniform(0.89, 0.94), 1.0 + over, 1.0]
    pch = PchipInterpolator(at, ax)
    tt, pause_done = 0.0, False
    pause_at = rng.uniform(0.60, 0.85)
    while tt < T:
        xx = distance * float(pch(min(tt / T, 1.0))) + rng.uniform(-0.25, 0.25)
        pts.append((t + tt * 1000, xx, 0.0))
        step = rng.uniform(step_lo, step_hi) / 1000.0
        if rng.random() < 0.07:
            step += rng.uniform(0.010, 0.020)
        tt += step
        if not pause_done and tt / T >= pause_at:
            ps, n = rng.uniform(pause_lo, pause_hi), 0.0
            while n < ps:
                pts.append((t + tt * 1000, distance * float(pch(min(tt / T, 1.0))) + rng.uniform(-0.2, 0.2), 0.0))
                tt += rng.uniform(0.012, 0.02)
                n += 0.015
            pause_done = True
    yv = rng.uniform(-1.5, 1.5)
    out = []
    for (pt, xx, _) in pts:
        yv = max(-6.0, min(6.0, yv + rng.uniform(-0.55, 0.55)))
        out.append((pt, xx, yv))
    out.append((out[-1][0] + rng.uniform(40, 100), out[-1][1], out[-1][2]))
    return out


# ==================== 页面基础设施 ====================

def _top_level_chrome_windows() -> dict:
    """枚举顶层 Chrome 窗口 → {hwnd: (pid, title)}。仅 win32;失败返回空。"""
    import ctypes
    user32 = ctypes.windll.user32
    found = {}
    CBTYPE = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        if cls.value == 'Chrome_WidgetWin_1':
            tt = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, tt, 256)
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            found[h] = (pid.value, tt.value or '')
        return True

    try:
        user32.EnumWindows(CBTYPE(cb), 0)
    except Exception:
        return {}
    return found


def _silence_taskbar(pre_existing: set) -> int:
    """把本次新建的浏览器窗口从任务栏/Alt-Tab 摘掉(改判 WS_EX_TOOLWINDOW)。

    窗口本身保持离屏正常渲染——不用 SW_HIDE:被隐藏的窗口会被合成器降频,滑块动画与
    布局可能停摆,而过滑块依赖真实渲染。扩展样式只在 Win32 层,页面 JS 读不到,
    因此不影响风控指纹。只处理"这次新建且标题属于测试浏览器"的窗口,绝不动用户
    自己开着的 Chrome。

    :return: 实际改掉的窗口数
    """
    if sys.platform != 'win32':
        return 0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = \
            0x1, 0x2, 0x4, 0x10, 0x20
        n = 0
        for h, (_, title) in _top_level_chrome_windows().items():
            if h in pre_existing:
                continue
            if 'Chrome' not in title:
                continue
            st = user32.GetWindowLongW(h, GWL_EXSTYLE) or 0
            if st & WS_EX_TOOLWINDOW:
                continue
            user32.SetWindowLongW(h, GWL_EXSTYLE,
                                  (st & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW)
            # 任务栏归属变化要让 shell 重读一次框架
            user32.SetWindowPos(h, 0, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                                | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            n += 1
        return n
    except Exception:
        return 0


def _restore_console_focus(retry_s: float = 0.0):
    """把前台焦点抢回控制台(仅 Windows,失败静默)。

    实测抢焦点的不是 chromium 启动,而是 ctx.new_page() 创建首个窗口的那一刻;
    所以调用点必须在建页之后。系统激活还可能比我们的调用晚零点几秒 → 允许在 retry_s
    内轮询重试;重试只在"抢了我们焦点的那个窗口仍占前台"时进行,一旦前台易主
    (说明用户自己切走了)立刻停手,不去跟他抢。SetForegroundWindow 返回值不可信,
    一律以 GetForegroundWindow 为准。用"前台锁超时临时清零"绕过 Windows 前台保护。
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
        con = kernel32.GetConsoleWindow()
        if not con:
            return
        # GetConsoleWindow 给的是 conhost 的伪控制台句柄(PseudoConsoleWindow),真正持前台的
        # 是宿主终端窗口(经典 conhost 窗口 / Windows Terminal 的 CASCADIA_HOSTING_WINDOW_CLASS)。
        # 判据必须用这个可见窗口,否则在 Windows Terminal 下永远判不中、白等满重试还误判失败。
        hwnd = user32.GetAncestor(con, 3) or con     # GA_ROOTOWNER
        GET_LOCK, SET_LOCK = 0x2000, 0x2001   # SPI_GET/SETFOREGROUNDLOCKTIMEOUT
        old = ctypes.c_uint()
        user32.SystemParametersInfoW(GET_LOCK, 0, ctypes.byref(old), 0)
        zero = ctypes.c_uint(0)
        user32.SystemParametersInfoW(SET_LOCK, 0, ctypes.byref(zero), 0)
        try:
            thief = user32.GetForegroundWindow()
            deadline = time.time() + max(0.0, retry_s)
            while True:
                fg = user32.GetForegroundWindow()
                if fg == hwnd or fg == con or (thief and fg != thief):
                    break                      # 已抢回,或前台已被第三方合法接管
                user32.SetForegroundWindow(hwnd)
                if time.time() >= deadline:
                    break
                time.sleep(0.05)
        finally:
            user32.SystemParametersInfoW(SET_LOCK, 0, ctypes.byref(old), 0)
    except Exception:
        pass


def _new_page(browser):
    """独立干净 context(隔离 cookie/存储)+ 反检测 init script。"""
    ctx = browser.new_context(viewport=VIEWPORT, user_agent=USER_AGENT)
    ctx.add_init_script(_STEALTH_JS)
    return ctx, ctx.new_page()


def _install_hooks(page, captured: dict, results: dict):
    """拦截滑块图片字节、verify 结果(legacy 用)、页面自身发出的 userLogin 响应。

    登录响应只存 Response 对象:登录成功后页面会跳转,在事件回调里读 body
    可能抛异常被吞,由调用方在主线程重试解析。
    滑块图片在弹窗打开时才会拉取,hook 常驻页面统一捕获。
    """
    def hook_resp(r):
        try:
            url = r.url
            if '/qst/PUZZLE/' in url and url.endswith('back.png'):
                captured['back'] = r.body()
                captured['t_img'] = time.time()
            elif '/qst/PUZZLE/' in url and url.endswith('shadow.png'):
                captured['shadow'] = r.body()
            elif '106eu7-verify' in url:
                results['verify'] = r.json()
                results['t_verify'] = time.time()
            elif 'userLogin' in url and r.request.method == 'POST':
                results.setdefault('login_resps', []).append(r)
        except Exception:
            pass
    page.on('response', hook_resp)


def _warmup_mouse(page, points=((320, 400), (660, 300), (950, 215))):
    """鼠标热身:给风控攒 mousemove 历史(缺失会显著推高 F001)。

    在预热期执行,与用户输账密并行,不占回车后的关键路径;拖拽前 approach 的几次
    move 仍提供紧邻历史。
    """
    for wx, wy in points:
        page.mouse.move(wx + random.uniform(-30, 30), wy + random.uniform(-20, 20),
                        steps=random.randint(5, 9))
        time.sleep(random.uniform(0.03, 0.07))


def _click_open_popup(page) -> bool:
    """真鼠标点击登录键开启弹窗(页面内 JS 合成 click 开不了,实测无 verify 请求)。"""
    try:
        r = page.evaluate(_JS_FIND, '#captcha-button')
        if r:
            page.mouse.click(r['x'], r['y'])
            return True
    except Exception:
        pass
    return False


def _setup_mypage(page, captured: dict, abort=None, warmup: bool = True) -> bool:
    """加载自建极简登录页 → 真点开弹窗 → 等滑块渲染 → 鼠标热身。

    就绪信号不可颠倒:拼图 back/shadow 到齐说明 AliyunCaptcha 实例已建好并绑上
    按钮监听(此时真点才有用);但弹窗未开时这些图也已预加载,故"图到齐"只是
    "可以点"的信号,弹窗到底开没开要看滑块按钮有无尺寸。

    预热期调用时与用户输账密并行;重试轮调用会顺带重置滑块会话。

    :param abort: 可选 callables,返回 True 时提前放弃(如账密已提交,避免阻塞)
    :param warmup: 是否在本函数内完成鼠标热身(默认搬到预热期,省关键路径 ~0.3s)
    :return: True 就绪 / False 超时或放弃
    """
    captured.pop('back', None)
    captured.pop('shadow', None)
    try:
        page.goto(SSO_ORIGIN + '/robots.txt', wait_until='domcontentloaded', timeout=15000)
        page.set_content(_LOGIN_PAGE_HTML)
    except Exception as e:
        log(f'  [自动登录] 自建页加载失败: {str(e)[:60]}', 'WARNING')
        return False

    def _give_up():
        return abort is not None and abort()

    # 1) 等 SDK 就绪(拼图字节到齐)→ 立刻真点开弹窗
    #    轮询里的 page.evaluate('1') 是必须的:它推进 Playwright 事件分发,hook 才收得到图
    imgs = False
    for _ in range(120):
        if 'back' in captured and 'shadow' in captured:
            imgs = True
            break
        if _give_up():
            return False
        page.evaluate('1')
        time.sleep(0.02)
    if not imgs:
        log('  [自动登录] 预热未就绪(拼图图未到,SDK 可能改版)', 'WARNING')
        return False
    _click_open_popup(page)

    # 2) 等弹窗内滑块渲染出尺寸;组件异步重初始化会让首点落空,按节奏补点
    slider, clicks = False, 1
    t0 = time.time()
    while time.time() - t0 < 12:
        try:
            if page.locator('#aliyunCaptcha-sliding-slider').bounding_box():
                slider = True
                break
        except Exception:
            pass
        if _give_up():
            return False
        if clicks < 4 and time.time() - t0 > clicks * 1.2:
            _click_open_popup(page)
            clicks += 1
        time.sleep(0.04)
    if not slider:
        log(f'  [自动登录] 预热未就绪(弹窗未渲染,已点 {clicks} 次)', 'WARNING')
        return False
    if warmup:
        _warmup_mouse(page)
    return True


def _solve_slider(page, captured: dict):
    """识别缺口 → 反解拖距 → 真人轨迹拖拽 → 闭环校正 → mouse.up。

    小粒度等待一律 time.sleep:page.wait_for_timeout 被浏览器 rAF 量化到 16ms 一档
    (请求 0ms 也花 15.6ms),而本机 time.sleep 精度 1ms→1.07ms。
    注意:等 hook/响应的那类循环不能这么写,那边必须留 Playwright 调用推进事件分发。
    """
    gap_x1, shape_x0, conf = identify_gap(captured['back'], captured['shadow'])
    left_target = (gap_x1 - shape_x0) * PUZZLE_SCALE
    d_est = _invert_map(left_target)
    log(f"  [自动登录] 缺口 x={gap_x1:.1f} conf={conf:.2f} 拖距≈{d_est:.1f}px", "DEBUG")

    slider = page.locator('#aliyunCaptcha-sliding-slider')
    box = slider.bounding_box()
    if not box:
        raise RuntimeError('滑块按钮未渲染')
    sx, sy = box['x'] + box['width']/2, box['y'] + box['height']/2
    get_left = lambda: page.evaluate(
        "() => parseFloat(document.querySelector('#aliyunCaptcha-puzzle').style.left) || 0")

    dur = random.uniform(*DRAG_DUR_S)
    pts = gen_track(d_est, seed=random.randrange(2**32), dur=dur)
    ox, oy = random.uniform(-9, 7), random.uniform(-4, 4)
    page.mouse.move(sx + ox - random.uniform(20, 50), sy + oy + random.uniform(-12, 12))
    for _ in range(2):
        page.mouse.move(sx + ox + random.uniform(-3, 3), sy + oy + random.uniform(-2, 2))
        time.sleep(random.uniform(0.004, 0.009))
    page.mouse.down()
    time.sleep(random.uniform(0.025, 0.050))     # 按下到起步的犹豫(实测 40-75ms 无必要)
    t_drag0 = time.time()
    for tt, xx, yy in pts:
        page.mouse.move(sx + ox + xx, sy + oy + yy)
        el = time.time() - t_drag0
        delay = tt/1000.0 - el
        if delay > 0:
            time.sleep(min(delay, 0.04))
        elif el > tt/1000.0 + DRAG_WATCHDOG_S:   # 单次 move 被页面卡顿拖住:弃本轮,别耗着
            raise RuntimeError(f'拖拽超时(落后 {el - tt/1000.0:.1f}s)')
    # 闭环兜底:实时读拼图 left,误差收敛 <0.7px 才松手。它兜的是二次映射残差;
    # 轮数打满仍未收敛,基本说明缺口靶心算错了,继续精修只是把落点更准地送错地方。
    cur_x = sx + ox + d_est
    rounds = 0
    for _ in range(CORR_MAX):
        err = left_target - get_left()
        if abs(err) < 0.7:
            break
        rounds += 1
        step = max(min(err * 0.85, 16), -16)
        cur_x += step
        page.mouse.move(cur_x, sy + oy + random.uniform(-0.4, 0.4))
        time.sleep(random.uniform(0.010, 0.024))
    if rounds >= CORR_MAX:
        log(f'  [自动登录] 校正 {rounds} 轮未收敛,疑识别偏差(conf={conf:.2f})', 'DEBUG')
    time.sleep(random.uniform(0.018, 0.035))
    try:   # 自校准采样:(实际拖距, 实际拼图位移),拖拽已落定、松手前读取最稳定
        calib_observe(cur_x - (sx + ox), get_left())
    except Exception:
        pass
    page.mouse.up()


def _extract_sso_token(login_json) -> Optional[str]:
    """从 userLogin 响应提取 sso_token(兼容 data.token 等字段布局)。"""
    if not isinstance(login_json, dict):
        return None
    data = login_json.get('data')
    if isinstance(data, dict):
        for k in ('token', 'ssoToken', 'access_token'):
            v = data.get(k)
            if isinstance(v, str) and v:
                return v
    for k in ('token', 'access_token'):
        v = login_json.get(k)
        if isinstance(v, str) and v:
            return v
    return None


# ==================== 主路径:自建极简登录页 ====================

def _one_attempt_mypage(page, user: str, pwd: str, captured: dict, results: dict,
                        warmed: bool = True):
    """主路径单次尝试:JS 注入账密 → 拖拽 → 页面自动登录拦响应。

    鼠标热身默认已在 _setup_mypage(预热期)完成;只有从未经过预热时才在此补做。

    :return: (sso_token, stop_reason)。token 非 None 即成功;
             stop_reason 非 None 表示不值得重试(如账密被拒)。
    """
    t0 = time.time()
    try:
        # JS 注入账密:自建页为纯静态表单(无 Vue 绑定),风控不评分表单行为
        fields = page.evaluate("""([u, w]) => {
            const a = document.getElementById('acc'), p = document.getElementById('pwd');
            if (!a || !p) return false;
            a.value = u; p.value = w; return true;
        }""", [user, pwd])
        if not fields:
            raise RuntimeError('自建页输入框缺失(页面可能已改版)')
        if not warmed:
            _warmup_mouse(page)
        if not ('back' in captured and 'shadow' in captured):
            # 等图必须留 Playwright 调用:response hook 靠它推进
            for _ in range(60):
                if 'back' in captured and 'shadow' in captured:
                    break
                page.evaluate('1')
                time.sleep(0.02)
            if not ('back' in captured and 'shadow' in captured):
                log('  [自动登录] 滑块图片拦截超时', 'WARNING')
                return None, None

        _solve_slider(page, captured)
        log(f'  [自动登录] 拖拽完成({time.time()-t0:.1f}s)', "DEBUG")
        # 页面在 verify 回调里自动发 userLogin,等响应。每轮 evaluate 既读结果也推进事件循环,
        # 所以这里用 time.sleep 细颗粒轮询是安全的(旧写法 wait_for_timeout(20) 实为 31ms 一档)
        login, deadline = None, time.time() + 8.0
        while time.time() < deadline:
            login = page.evaluate("() => window.__login")
            if login:
                break
            time.sleep(0.004)
        log(f'  [自动登录] 登录响应({time.time()-t0:.1f}s)', "DEBUG")
        if not isinstance(login, dict):
            log('  [自动登录] 未捕获登录响应', 'WARNING')
            return None, None

        token = _extract_sso_token(login)
        if token:
            log(f'  [自动登录] 滑块通过,已获取 SSO Token(本次 {time.time()-t0:.1f}s)', 'SUCCESS')
            results['vcode'] = 'PASS'
            return token, None
        msg = str(login.get('msg') or login.get('message') or '')
        if 'F015' in msg or 'F001' in msg or '验证码' in msg:
            vcode = 'F015' if 'F015' in msg else ('F001' if 'F001' in msg else '?')
            results['vcode'] = vcode
            log(f'  [自动登录] 滑块未通过({vcode} {_VERIFY_HINT.get(vcode, "滑块校验失败")})', 'WARNING')
            return None, None   # 可重试
        # 账密类拒绝(不存在/密码错误/冻结等),重试无意义
        results['vcode'] = 'BIZ'
        log(f'  [自动登录] 滑块通过但登录被拒: {msg[:40]}', 'ERROR')
        return None, msg[:40] or '登录被拒'
    except Exception as e:
        log(f'  [自动登录] 本次尝试异常: {str(e)[:60]}', 'WARNING')
        return None, None


# ==================== 兜底路径:真实 SSO 登录页全流程 ====================

def _open_to_captcha(page, user: str, pwd: str, results: dict):
    """真实页兜底:填账密走到滑块弹出或登录直出(真键盘 + 真鼠标行为链)。"""
    page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=30000)
    page.wait_for_timeout(400)
    # 鼠标热身
    for wx, wy in ((320, 400), (700, 300), (950, 215)):
        page.mouse.move(wx + random.uniform(-30, 30), wy + random.uniform(-20, 20),
                        steps=random.randint(5, 9))
        page.wait_for_timeout(random.uniform(25, 55))
    # 切账密 tab:点到为止可能早于 Vue 挂载,校验账密输入框出现,未出现则补点
    for _ in range(3):
        try:
            page.click('text="账号密码登录"', timeout=4000)
        except Exception:
            pass
        for _ in range(20):
            if page.evaluate(_JS_FIND_INPUT, '账号'):
                break
            page.wait_for_timeout(100)
        if page.evaluate(_JS_FIND_INPUT, '账号'):
            break
    # 真键盘输入(风控看 keydown/input 序列)
    for ph, text in (('账号', user), ('密码', pwd)):
        r = page.evaluate(_JS_FIND_INPUT, ph)
        if not r:
            raise RuntimeError(f'找不到输入框({ph})')
        page.mouse.click(r['x'], r['y'])
        page.wait_for_timeout(random.uniform(40, 100))
        page.keyboard.type(text, delay=random.uniform(12, 28))
        page.wait_for_timeout(random.uniform(50, 120))
    r = page.evaluate(_JS_FIND_BUTTON, '登录')
    if not r:
        raise RuntimeError('找不到登录按钮')
    page.mouse.click(r['x'], r['y'])   # 真鼠标点击(JS 合成点击被 Vue 忽略)
    page.wait_for_timeout(120)
    # 弹窗循环:滑块可见 / 登录直出即走;否则点「同意并登录」;否则关推广弹窗
    box = page.locator('#aliyunCaptcha-img-box')
    deadline = time.time() + 15
    while time.time() < deadline:
        if results.get('login_resps'):
            log('  [自动登录] 风控白名单:本次跳过滑块直接登录', 'DEBUG')
            return 'direct'
        if box.count() and box.is_visible():
            return 'captcha'
        r = page.evaluate(_JS_FIND_BUTTON, '同意并登录')
        if r:
            page.mouse.click(r['x'], r['y'])
            page.wait_for_timeout(random.uniform(250, 400))
            continue
        d = page.evaluate(_JS_FIND_DISMISS)
        if d:
            page.mouse.click(d['x'], d['y'])
            page.wait_for_timeout(random.uniform(200, 350))
            continue
        page.wait_for_timeout(100)
    raise RuntimeError('滑块未弹出且未直接登录(超时 15s)')


def _wait_sso_token(page, results: dict, timeout: float = 6.0) -> Optional[str]:
    """legacy 兜底路径:等页面自动登录拿 sso_token(响应解析 + 跳转 URL 双通道)。

    通道 A(响应解析):解析页面自己发出的 userLogin 响应取 data.token;
    通道 B(跳转 URL):页面登录成功后重定向,从目标 URL 的 token 参数取。
    任一通道命中即返回,应对真实页改版后跳转不再带 token 的情况。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 通道 A:页面自身登录响应的解析(跳转后 body 可能不可读,逐条容错)
        for r in list(results.get('login_resps') or []):
            try:
                tok = _extract_sso_token(r.json())
            except Exception:
                continue
            if tok:
                return tok
        # 通道 B:跳转 URL 的 token 参数
        m = re.search(r'[?&]token=([^&]+)', page.url)
        if m:
            return unquote(m.group(1))
        try:
            d = page.evaluate(_JS_FIND_DISMISS)
            if d:
                page.mouse.click(d['x'], d['y'])
        except Exception:
            pass
        time.sleep(0.05)
    return None


def _one_attempt_legacy(page, user: str, pwd: str, captured: dict, results: dict):
    """legacy 兜底单次尝试(真实 SSO 登录页全流程)。返回 (sso_token, stop_reason)。"""
    t0 = time.time()
    try:
        _open_to_captcha(page, user, pwd, results)
        if results.get('login_resps'):
            # 风控白名单直登:解析页面自己的 userLogin 响应拿 token
            log('  [自动登录] 风控白名单:跳过滑块直接登录', 'DEBUG')
            for _ in range(100):
                for r in list(results.get('login_resps') or []):
                    try:
                        tok = _extract_sso_token(r.json())
                    except Exception:
                        continue
                    if tok:
                        log(f'  [自动登录] 已获取 SSO Token(本次 {time.time()-t0:.1f}s)', 'SUCCESS')
                        return tok, None
                page.wait_for_timeout(50)
            log('  [自动登录] 兜底流程未获取到 Token', 'WARNING')
            return None, None
        for _ in range(300):
            if 'back' in captured and 'shadow' in captured:
                break
            page.wait_for_timeout(10)
        if not ('back' in captured and 'shadow' in captured):
            log('  [自动登录] 滑块图片拦截超时', 'WARNING')
            return None, None
        _solve_slider(page, captured)
        for _ in range(250):
            if 'verify' in results:
                break
            page.wait_for_timeout(10)
        vres = (results.get('verify') or {}).get('Result') or {}
        if not vres.get('VerifyResult'):
            vcode = vres.get('VerifyCode', '?')
            log(f'  [自动登录] 滑块未通过({vcode} {_VERIFY_HINT.get(vcode, "滑块校验失败")})', 'WARNING')
            return None, None
        token = _wait_sso_token(page, results)
        if token:
            log(f'  [自动登录] 已获取 SSO Token(本次 {time.time()-t0:.1f}s)', 'SUCCESS')
            return token, None
        log('  [自动登录] 兜底流程未获取到 Token', 'WARNING')
        return None, None
    except Exception as e:
        log(f'  [自动登录] 兜底尝试异常: {str(e)[:60]}', 'WARNING')
        return None, None


# ==================== 会话(后台预热 + 提交账密) ====================

class AutoSlider:
    """全自动滑块登录会话:后台线程跑浏览器,与 CLI 输账密并行。

    用法:
        s = AutoSlider(); s.start()            # 立即后台开浏览器+预热自建页+弹滑块
        ...(主线程让用户输账密,预热被完全藏掉)...
        token = s.obtain_sso_token(u, p)       # JS 注入账密 → 拖拽 → 拦 token
        s.close()

    playwright 对象绑定创建线程,所有页面操作都在后台线程内完成,
    主线程只通过队列提交账密/取结果。
    """

    _SUBMIT_TIMEOUT = 300.0   # 等账密提交的超时(秒),防挂死
    _RESULT_TIMEOUT = 240.0   # 等登录结果的超时(秒)

    def __init__(self, headless: bool = False):
        self._headless = headless
        self._job_q: "queue.Queue" = queue.Queue()
        self._result_q: "queue.Queue" = queue.Queue()
        self._closed = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """启动后台预热线程(幂等)。依赖缺失时静默跳过,由 obtain 提示。"""
        if _DEPS_MISSING or self._thread:
            return
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def obtain_sso_token(self, user: str, pwd: str, max_attempts: int = 2) -> Optional[str]:
        """提交账密,等待全自动滑块登录结果。

        :return: sso_token;失败返回 None(内部已打日志)
        """
        if not user or not pwd:
            return None
        if _DEPS_MISSING:
            log(f'  [自动登录] 缺少依赖 {_DEPS_MISSING},'
                f'请先 pip install -r requirements.txt', 'WARNING')
            return None
        if not self._thread:
            self.start()
        try:
            self._job_q.put((user, pwd, max_attempts), timeout=5)
        except queue.Full:
            return None
        try:
            kind, payload = self._result_q.get(timeout=self._RESULT_TIMEOUT)
        except queue.Empty:
            log('  [自动登录] 等待登录结果超时', 'WARNING')
            return None
        if kind == 'token':
            return payload
        if kind == 'error':
            log(f'  [自动登录] {payload}', 'ERROR')
        return None

    def close(self):
        """关闭会话(通知后台线程自行清理,幂等)。"""
        self._closed.set()
        self._job_q.put(None)
        if self._thread:
            self._thread.join(timeout=15)

    # -------------------- 后台线程侧(全部 playwright 操作在此) --------------------

    @staticmethod
    def _prefer_system_browsers():
        """冻结打包后 playwright 会强制使用包内 .local-browsers
        (_transport.py: frozen 时 env.setdefault("PLAYWRIGHT_BROWSERS_PATH","0")),
        而本包不含浏览器、依赖目标机 `playwright install chromium` 装的系统级
        浏览器,故检测到系统级浏览器时显式把 env 指回去。"""
        import sys
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ms-playwright"
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Caches" / "ms-playwright"
        else:
            base = Path.home() / ".cache" / "ms-playwright"
        try:
            if base.exists() and any(base.glob("chromium-*")):
                os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(base))
        except Exception:
            pass

    def _worker(self):
        browser = None
        self._prefer_system_browsers()
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=self._headless, args=LAUNCH_ARGS)
                except Exception as e:
                    self._result_q.put(('error', f'浏览器启动失败: {str(e)[:300]}'))
                    return
                ctx = page = None
                captured, results = {}, {}
                try:
                    wins0 = set(_top_level_chrome_windows())
                    ctx, page = _new_page(browser)
                    # 先摘掉任务栏/Alt-Tab 里的浏览器图标(窗口本身离屏,照常渲染),
                    # 再把前台抢回终端——顺序反了会被 SetWindowPos 之后的激活干扰
                    _silence_taskbar(wins0)
                    # 实测抢终端前台的是"创建首个页面窗口"这一步(launch 本身不抢),
                    # 所以必须在建页后抢回;系统激活可能稍晚,给 1.2s 轮询重试窗口。
                    _restore_console_focus(retry_s=1.2)
                    _install_hooks(page, captured, results)
                except Exception as e:
                    self._result_q.put(('error', f'页面创建失败: {str(e)[:60]}'))
                    return
                # 预热:趁用户输账密加载自建页并弹好滑块(并行提速关键);
                # 账密先到则提前放弃,尝试路径会用热缓存自行快速加载
                self._setup_ready = _setup_mypage(
                    page, captured, abort=lambda: not self._job_q.empty() or self._closed.is_set())
                job = self._wait_job()
                if job is None:
                    return
                user, pwd, max_attempts = job
                self._run_attempts(page, captured, results, user, pwd, max_attempts)
        except Exception as e:
            self._result_q.put(('error', f'自动登录异常: {str(e)[:60]}'))
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

    def _wait_job(self):
        """等账密提交;close() 或超时返回 None。"""
        deadline = time.time() + self._SUBMIT_TIMEOUT
        while not self._closed.is_set():
            try:
                job = self._job_q.get(timeout=0.2)
            except queue.Empty:
                if time.time() > deadline:
                    return None
                continue
            return job   # None(close 哨兵)或 (user, pwd, max_attempts)
        return None

    def _run_attempts(self, page, captured: dict, results: dict,
                      user: str, pwd: str, max_attempts: int):
        """主路径自建页尝试 max_attempts 次;全失败再走一次真实页兜底。"""
        for i in range(1, max_attempts + 1):
            log(f'  [自动登录] 第 {i}/{max_attempts} 次尝试...', 'INFO')
            if i > 1 or not self._setup_ready:
                if i > 1:
                    # F015 属识别/映射偏差,不是行为风控,短退避即可;F001 才值得长退避
                    back = 2.5 if results.get('vcode') == 'F001' else 0.8
                    log(f'  [自动登录] 退避 {back}s 后重试(防高频风控)...', 'INFO')
                    time.sleep(back)
                captured.clear()   # 先清空,再由 _setup_mypage 重新捕获新图片
                results.clear()
                if not _setup_mypage(page, captured):
                    continue
            results.clear()
            token, stop = _one_attempt_mypage(page, user, pwd, captured, results)
            if token:
                self._result_q.put(('token', token))
                return
            if stop:   # 账密被拒等,重试无意义
                self._result_q.put(('none', None))
                return
        # 自建页全失败 → 真实 SSO 页全流程兜底一次(应对页面/场景改版)
        try:
            captured.clear()
            results.clear()
            results.setdefault('login_resps', [])
            token, _ = _one_attempt_legacy(page, user, pwd, captured, results)
            if token:
                self._result_q.put(('token', token))
                return
        except Exception as e:
            log(f'  [自动登录] 兜底流程异常: {str(e)[:60]}', 'DEBUG')
        self._result_q.put(('none', None))


def obtain_sso_token(user: str, pwd: str, max_attempts: int = 2,
                     headless: bool = False) -> Optional[str]:
    """全自动滑块登录:仅凭账密过阿里云滑块,返回 sso_token(无预热同步入口)。

    常规 CLI 场景建议用 AutoSlider(输账密期间并行预热,省 ~2s)。

    :param user: 账号(学号/手机号)
    :param pwd: 密码
    :param max_attempts: 最大尝试次数(默认 2:首次 + 重试 1 次)
    :param headless: 无头模式(默认有头,与实证环境一致)
    :return: sso_token;全部尝试失败返回 None
    """
    s = AutoSlider(headless=headless)
    s.start()
    try:
        return s.obtain_sso_token(user, pwd, max_attempts=max_attempts)
    finally:
        s.close()
