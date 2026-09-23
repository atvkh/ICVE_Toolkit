"""智慧职教 API 客户端核心模块。

封装三域鉴权(主域/AI域/资源库域)、课程获取、刷课心跳、签到改签等全部能力。
基于抓包验证的接口实现,保留完整的业务参数构造逻辑。
"""

import base64
import hashlib
import json
import os
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Union
from urllib.parse import quote

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from requests.adapters import HTTPAdapter

from utils import log

# 连接池上限：requests 默认只缓存 10 条连接，刷课车道数一超过 10 就开始
# "建连→用完即弃"，每条请求重付一次 TLS 握手（实测单条 RTT 从 ~0.07s 涨到 ~0.20s，
# 12 车道反而比旧的格内并发更慢）。故显式放大到能容纳车道并发的量级。
POOL_CONNECTIONS = 10
POOL_MAXSIZE = 24

# ==================== 域名常量 ====================
BASE_URL = "https://zjy2.icve.com.cn/prod-api"        # 主域(SPOC/MOOC 课程)
AI_BASE_URL = "https://ai.icve.com.cn/prod-api"        # AI 域(MOOC 课程设计/讨论)
ZYK_BASE_URL = "https://zyk.icve.com.cn/prod-api"      # 资源库域
SSO_BASE = "https://sso.icve.com.cn"                   # SSO 单点登录域

# AI 域换票锁:passLogin 一次一张票,并发线程各自换票会把票数放大成"风暴"
# (实测并发 3/8/16 → passLogin 3/8/16 次,扫描起头白等 0.75/2.0/4.0s),
# 故冷会话补鉴权走 ensure_ai_token 的双检锁。
_AI_AUTH_LOCK = threading.Lock()

# 视频防盗链 Referer(反编译确认官方 H5 带此 header)
_VIDEO_REFERER = {"Referer": "https://zjy2.icve.com.cn/prod-api/"}

# 图片类型集合(刷课时长豁免)
IMAGE_TYPES = {"image", "图片", "图文", "picture", "photo", "png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"}
VIDEO_TYPES = {"video", "audio", "mp4", "flv", "视频", "音频", "m3u8", "avi", "mov"}
# 音频课件类型(时长解析分流:先 MP3 解析,失败回落 MP4——对齐生产 2026-09-16 消噪修复)
AUDIO_TYPES = {"audio", "音频"}

# MP3 帧头解析常量(生产 backend/zjy_client.py 同源移植)
_MP3_BR_V1L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_MP3_BR_V2L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _mp3_skip_id3v2(data: bytes) -> int:
    """ID3v2 标签 → 返回其后偏移；无标签返回 0"""
    if len(data) >= 10 and data[:3] == b"ID3":
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
               ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        return 10 + size
    return 0


def _mp3_frame_header(data: bytes, p: int):
    """解析 p 处 MPEG1/2 Layer III 帧头，非法返回 None"""
    if p + 4 > len(data) or data[p] != 0xFF or (data[p + 1] & 0xE0) != 0xE0:
        return None
    b1, b2 = data[p + 1], data[p + 2]
    version, layer = (b1 >> 3) & 0x03, (b1 >> 1) & 0x03
    if version == 1 or layer != 1:          # 只支持 Layer III（网络音频绝对主流）
        return None
    br_idx, sr_idx = (b2 >> 4) & 0x0F, (b2 >> 2) & 0x03
    if sr_idx == 3 or br_idx in (0, 15):
        return None
    bitrate = (_MP3_BR_V1L3 if version == 3 else _MP3_BR_V2L3)[br_idx]
    if bitrate <= 0:
        return None
    return {"version": version, "bitrate": bitrate,
            "samplerate": _MP3_SR[version][sr_idx],
            "spf": 1152 if version == 3 else 576,
            "channel": (data[p + 3] >> 6) & 0x03}


def _parse_mp3_duration_bytes(data: bytes) -> Optional[int]:
    """从字节流解析 MP3 时长（秒）；非 MP3/无精确帧数头返回 None。纯函数。"""
    if len(data) < 4:
        return None
    if data[4:8] == b"ftyp" or data[:3] == b"\x00\x00\x01":
        return None                          # MP4/MPEG-PS 容器 → 交回 MP4 解析
    off = _mp3_skip_id3v2(data)
    h = None
    frame_at = -1
    for p in range(off, min(off + 8, len(data) - 4)):
        h = _mp3_frame_header(data, p)
        if h:
            frame_at = p
            break
    if not h:
        return None
    side = (32 if h["channel"] != 3 else 17) if h["version"] == 3 \
        else (17 if h["channel"] != 3 else 9)
    scan = data[frame_at + 4: frame_at + 4 + side + 96]
    for tag in (b"Xing", b"Info", b"VBRI"):
        i = scan.find(tag)
        if i == -1:
            continue
        if tag == b"VBRI":
            # 标准 VBRI 头：Frame Count 在 tag+16（实测边界需 i+20）
            if i + 20 > len(scan):
                continue
            frames = struct.unpack_from(">I", scan, i + 16)[0]
        else:
            if i + 12 > len(scan):
                continue
            if not (struct.unpack_from(">I", scan, i + 4)[0] & 1):
                continue
            frames = struct.unpack_from(">I", scan, i + 8)[0]
        if frames:
            return int(round(frames * h["spf"] / h["samplerate"]))
    return None


def get_mp3_duration(url: str) -> Optional[int]:
    """Range 请求解析 MP3 时长（秒），失败返回 None。1 次 GET 即命中（实测 50ms，
    比 MP4 三策略全失败的 300ms 快 6 倍）；大 ID3v2 标签时二次 Range 精读。"""
    try:
        r = requests.get(url, headers={**{"Range": "bytes=0-65535"}, **_VIDEO_REFERER}, timeout=15)
        if r.status_code not in (200, 206):
            return None
        data = r.content
        off = _mp3_skip_id3v2(data)
        if off >= len(data) - 4:             # ID3v2 标签超出首窗 → 二次精读
            r2 = requests.get(url, headers={**{"Range": f"bytes={off}-{off + 65535}"},
                                            **_VIDEO_REFERER}, timeout=15)
            if r2.status_code not in (200, 206) or len(r2.content) < 4:
                return None
            data = r2.content
        return _parse_mp3_duration_bytes(data)
    except Exception:
        pass
    return None


class ZjyClient:
    """智慧职教 API 客户端。

    通过 token 或 sso_token 初始化,自动完成三域鉴权。
    一个实例对应一个登录会话,线程安全由 requests.Session 保证。
    """

    def __init__(self, token: Optional[str] = None, sso_token: Optional[str] = None,
                 question_bank_dir: Optional[str] = None, accounts: Optional[dict] = None):
        self.session = requests.Session()
        _adapter = HTTPAdapter(pool_connections=POOL_CONNECTIONS, pool_maxsize=POOL_MAXSIZE)
        self.session.mount("https://", _adapter)
        self.session.mount("http://", _adapter)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36",
            "log-equipment-app-version": "2.5.6",
            "log-equipment-model": "google Pixel 8",
            "log-equipment-api-version": "35",
            "log-equipment": "1",
            "platform-type": "android",
        })
        self.token: Optional[str] = token
        self.sso_token: Optional[str] = sso_token
        self.user_info: Optional[dict] = None
        self.stu_id: Optional[str] = None
        self.ai_token: Optional[str] = None
        self.zyk_token: Optional[str] = None
        # 题库目录(可选):无则跳过题库兜底
        self.question_bank_dir: Optional[str] = question_bank_dir
        # 同学账号 token 字典(可选):{nickname: {"token":..., "stuId":...}}
        # 用于 _get_classmate_correct_answers 扫包;为空则跳过同学答案扫描
        self.accounts: dict = accounts or {}
        # 题库缓存(带 mtime 检测,文件变动才重新读取)
        self._question_bank_cache: Optional[list] = None
        self._question_bank_mtime: float = 0

        if token:
            self.apply_token(token)

    # ==================== 鉴权 ====================

    def apply_token(self, t: str) -> bool:
        """应用 bearer token:设 Header → 拉用户信息 → 解析 JWT stu_id → 鉴权 AI 域。

        :return: True 表示 token 有效并已拉取到用户信息
        """
        self.token = t
        self.session.headers["Authorization"] = f"Bearer {t}"
        try:
            resp = self.session.get(f"{BASE_URL}/system/user/getInfo", timeout=10)
            if resp.status_code == 200 and resp.json().get("code") == 200:
                self.user_info = resp.json().get("user", {})

                # 从 JWT payload 解析 user_id 作为 stu_id
                try:
                    payload = t.split('.')[1]
                    payload += '=' * (4 - len(payload) % 4)
                    jwt_data = json.loads(base64.b64decode(payload))
                    self.stu_id = str(jwt_data.get("user_id", ""))
                except Exception:
                    self.stu_id = None

                if not self.stu_id:
                    self.stu_id = str(self.user_info.get("userId", ""))

                # AI 域鉴权延迟到首次访问 AI 接口时(api_*_ai 内自动补鉴权):
                # 登录链路少一个串行 RTT,而只有 MOOC 讨论/考试才用得到 AI 域
                return True
            else:
                log(f"apply_token 失败: HTTP {resp.status_code}", "WARNING")
        except Exception as e:
            log(f"apply_token 异常: {e}", "ERROR")
        return False

    def refresh_token_from_sso(self) -> bool:
        """用 sso_token 换取新的 bearer token 并应用(无感刷新)。

        复用 passLogin 逻辑:GET /auth/passLogin?token=<ssoToken>
        """
        if not self.sso_token:
            return False
        try:
            resp = self.session.get(
                f"{BASE_URL}/auth/passLogin",
                params={"token": self.sso_token},
                timeout=10,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            new_token = _extract_access_token(data)
            if not new_token:
                return False
            return self.apply_token(new_token)
        except Exception as e:
            log(f"refresh_token_from_sso 异常: {e}", "ERROR")
            return False

    def auth_ai_domain(self) -> bool:
        """AI 域鉴权:用 sso_token(优先)或 token 换 ai_token。"""
        try:
            auth_token = self.sso_token or self.token
            if not auth_token:
                return False
            clean_headers = {k: v for k, v in self.session.headers.items()
                             if k.lower() not in ("authorization", "x-ai-token")}
            resp = self.session.get(
                f"{AI_BASE_URL}/auth/passLogin",
                params={"token": auth_token},
                headers=clean_headers,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 200:
                    ai_token = _extract_access_token(data)
                    if ai_token:
                        self.ai_token = ai_token
                        self.session.headers["X-AI-Token"] = f"Bearer {ai_token}"
                        return True
        except Exception as e:
            log(f"auth_ai_domain 异常: {e}", "ERROR")
        return False

    def auth_zyk_domain(self) -> bool:
        """资源库域鉴权:用 sso_token 或 token 换 zyk_token。

        抓包确认:GET https://zyk.icve.com.cn/prod-api/auth/passLogin?token=<ssoToken>
        返回 {code:200, data:{access_token, expires_in:1440}}
        """
        try:
            auth_token = self.sso_token or self.token
            if not auth_token:
                return False
            clean_headers = {k: v for k, v in self.session.headers.items()
                             if k.lower() not in ("authorization", "x-ai-token")}
            clean_headers["Referer"] = "https://zyk.icve.com.cn/icve-study/courseDetailed"
            resp = self.session.get(
                f"{ZYK_BASE_URL}/auth/passLogin",
                params={"token": auth_token},
                headers=clean_headers,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 200:
                    zyk_token = _extract_access_token(data)
                    if zyk_token:
                        self.zyk_token = zyk_token
                        return True
        except Exception as e:
            log(f"auth_zyk_domain 异常: {e}", "ERROR")
        return False

    # ==================== AES 加密 ====================

    def generate_aes_key(self) -> Optional[str]:
        """生成 AES-128-ECB 密钥:md5(token)[:16]。"""
        if not self.token:
            return None
        return hashlib.md5(self.token.encode()).hexdigest()[:16]

    def aes_encrypt(self, plaintext: str, key: str) -> Optional[str]:
        """AES-128-ECB 加密,返回 Base64 字符串。"""
        try:
            cipher = AES.new(key.encode(), AES.MODE_ECB)
            padded = pad(plaintext.encode(), AES.block_size)
            encrypted = cipher.encrypt(padded)
            return base64.b64encode(encrypted).decode()
        except Exception as e:
            log(f"aes_encrypt 异常: {e}", "ERROR")
            return None

    # ==================== 主域 API ====================

    def api_get(self, path: str, params: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """主域 GET 请求。"""
        try:
            resp = self.session.get(f"{BASE_URL}/{path}", params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"GET {path} 异常: {e}", "ERROR")
        return None

    def api_post(self, path: str, body: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """主域 POST 请求。

        spoc/ 路径需特殊编码:JSON 序列化后对 % 做 %25 转义(抓包确认服务端要求)。
        """
        try:
            if path.startswith("spoc/") and isinstance(body, dict):
                payload_str = json.dumps(body, ensure_ascii=False)
                escaped_payload_str = payload_str.replace("%", "%25")
                headers = {"Content-Type": "application/json;charset=UTF-8"}
                resp = self.session.post(
                    f"{BASE_URL}/{path}",
                    data=escaped_payload_str.encode("utf-8"),
                    headers=headers,
                    timeout=timeout,
                )
            else:
                resp = self.session.post(f"{BASE_URL}/{path}", json=body, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            # 非 200 也尝试读取响应体,便于调试
            try:
                return resp.json()
            except Exception:
                pass
        except Exception as e:
            log(f"POST {path} 异常: {e}", "ERROR")
        return None

    def api_put(self, path: str, body: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """主域 PUT 请求。"""
        try:
            resp = self.session.put(f"{BASE_URL}/{path}", json=body, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"PUT {path} 异常: {e}", "ERROR")
        return None

    def api_delete(self, path: str, params: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """主域 DELETE 请求。"""
        try:
            resp = self.session.delete(f"{BASE_URL}/{path}", params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"DELETE {path} 异常: {e}", "ERROR")
        return None

    # ==================== AI 域 API(401 自动重新鉴权) ====================

    def ensure_ai_token(self) -> bool:
        """无票时补一次 AI 域鉴权;并发下只让第一个线程真打 passLogin,其余复用已换到的票。

        只作用于"起手无票"这一种情况;401 懒重鉴权路径保持原样(不在此复用他人新票)。
        """
        if self.ai_token:
            return True
        with _AI_AUTH_LOCK:
            if self.ai_token:
                return True
            return self.auth_ai_domain()

    def _ai_headers(self) -> dict:
        """AI 域请求头;token 缺失(登录后未鉴权)时先补一次 AI 域鉴权。

        apply_token 不再主动鉴权 AI 域,故冷会话的第一次 AI 调用在这里补齐。
        """
        if not self.ai_token:
            self.ensure_ai_token()
        return {"Authorization": f"Bearer {self.ai_token}"} if self.ai_token else {}

    def api_get_ai(self, path: str, params: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """AI 域 GET,401 时自动重新鉴权重试一次。"""
        try:
            headers = self._ai_headers()
            resp = self.session.get(f"{AI_BASE_URL}/{path}", params=params, headers=headers, timeout=timeout)
            if resp.status_code == 401:
                self.auth_ai_domain()
                if self.ai_token:
                    headers["Authorization"] = f"Bearer {self.ai_token}"
                resp = self.session.get(f"{AI_BASE_URL}/{path}", params=params, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"GET(AI) {path} 异常: {e}", "ERROR")
        return None

    def api_post_ai(self, path: str, body: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """AI 域 POST,401 时自动重新鉴权重试一次。"""
        try:
            headers = self._ai_headers()
            resp = self.session.post(f"{AI_BASE_URL}/{path}", json=body, headers=headers, timeout=timeout)
            if resp.status_code == 401:
                self.auth_ai_domain()
                if self.ai_token:
                    headers["Authorization"] = f"Bearer {self.ai_token}"
                resp = self.session.post(f"{AI_BASE_URL}/{path}", json=body, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"POST(AI) {path} 异常: {e}", "ERROR")
        return None

    def api_put_ai(self, path: str, body: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """AI 域 PUT。"""
        try:
            headers = self._ai_headers()
            resp = self.session.put(f"{AI_BASE_URL}/{path}", json=body, headers=headers, timeout=timeout)
            if resp.status_code == 401:
                self.auth_ai_domain()
                if self.ai_token:
                    headers["Authorization"] = f"Bearer {self.ai_token}"
                resp = self.session.put(f"{AI_BASE_URL}/{path}", json=body, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"PUT(AI) {path} 异常: {e}", "ERROR")
        return None

    # ==================== 资源库域 API ====================

    def api_get_zyk(self, path: str, params: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """资源库域 GET,需带 Referer。"""
        try:
            headers = {"Referer": "https://zyk.icve.com.cn/icve-study/courseDetailed"}
            if self.zyk_token:
                headers["Authorization"] = f"Bearer {self.zyk_token}"
            resp = self.session.get(f"{ZYK_BASE_URL}/{path}", params=params, headers=headers, timeout=timeout)
            try:
                return resp.json()
            except Exception:
                return None
        except Exception as e:
            log(f"GET(ZYK) {path} 异常: {e}", "ERROR")
        return None

    def api_post_zyk(self, path: str, body: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """资源库域 POST。"""
        try:
            headers = {"Content-Type": "application/json", "Referer": "https://zyk.icve.com.cn/icve-study/courseDetailed"}
            if self.zyk_token:
                headers["Authorization"] = f"Bearer {self.zyk_token}"
            resp = self.session.post(f"{ZYK_BASE_URL}/{path}", json=body, headers=headers, timeout=timeout)
            try:
                return resp.json()
            except Exception:
                return None
        except Exception as e:
            log(f"POST(ZYK) {path} 异常: {e}", "ERROR")
        return None

    def api_put_zyk(self, path: str, body: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        """资源库域 PUT。"""
        try:
            headers = {"Content-Type": "application/json", "Referer": "https://zyk.icve.com.cn/icve-study/courseDetailed"}
            if self.zyk_token:
                headers["Authorization"] = f"Bearer {self.zyk_token}"
            resp = self.session.put(f"{ZYK_BASE_URL}/{path}", json=body, headers=headers, timeout=timeout)
            try:
                return resp.json()
            except Exception:
                return None
        except Exception as e:
            log(f"PUT(ZYK) {path} 异常: {e}", "ERROR")
        return None

    # ==================== 响应解析 ====================

    def _extract_rows_loose(self, data) -> list:
        """宽松提取列表数据,兼容 data/rows/list 多种结构。"""
        if not data:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, str):
            return []
        if not isinstance(data, dict):
            return []
        d = data.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            if d.get("rows"):
                return d["rows"]
            if d.get("list"):
                return d["list"]
            if any(k for k in d.keys() if "course" in k.lower() or "id" in k.lower()):
                return [d]
        if data.get("rows"):
            return data["rows"]
        if data.get("list"):
            return data["list"]
        return []

    def extract_rows(self, data) -> list:
        """严格提取列表数据,要求 code==200。"""
        if not data:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, str):
            return []
        if data.get("code") != 200:
            return []
        d = data.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict) and d.get("rows"):
            return d["rows"]
        if isinstance(d, dict) and d.get("list"):
            return d["list"]
        if data.get("rows"):
            return data["rows"]
        if isinstance(d, dict) and not d.get("rows") and not d.get("list"):
            return [d] if d else []
        return []

    # ==================== 课程获取 ====================

    def get_my_courses(self) -> list:
        """聚合 SPOC / MOOC / 资源库三类课程,去重后返回。

        每个 course dict 含:courseId / courseInfoId / classId / courseName / _courseType
        """
        all_courses = []
        course_map = {}

        def _add_course(c: dict, ctype: str):
            key = (c.get("courseId", ""), c.get("courseInfoId", ""), c.get("classId", ""))
            if not c.get("courseId"):
                return
            if key not in course_map:
                c["_courseType"] = ctype
                course_map[key] = c

        # 1. SPOC 课程
        spoc_data = self.api_get("spoc/courseInfoStudent/myCourseList", {"pageNum": 1, "pageSize": 100, "flag": "3"})
        spoc_courses = self.extract_rows(spoc_data)
        if not spoc_courses:
            spoc_data = self.api_get("spoc/courseInfoStudent/app/myCourseList", {"pageNum": 1, "pageSize": 100})
            spoc_courses = self.extract_rows(spoc_data)
        for c in spoc_courses or []:
            _add_course(c, "SPOC")

        # 2. MOOC 课程
        mooc_params = {
            "pageNum": 1, "pageSize": 100,
            "selectType": "0", "courseType": "0",
            "token": self.token, "userId": self.stu_id,
        }
        mooc_data = self.api_get("spoc/course/mooc/getMyCourseList", mooc_params)
        mooc_courses = self.extract_rows(mooc_data)
        for c in mooc_courses or []:
            if not c.get("courseId") and c.get("moocCourseId"):
                c["courseId"] = c.get("moocCourseId")
            if not c.get("courseInfoId") and c.get("moocCourseInfoId"):
                c["courseInfoId"] = c.get("moocCourseInfoId")
            if not c.get("courseName") and c.get("courseTitle"):
                c["courseName"] = c.get("courseTitle")
            c["classId"] = c.get("classId", "")
            _add_course(c, "MOOC")

        # 3. 资源库课程(走 zyk 域)
        if not self.zyk_token and not self.auth_zyk_domain():
            pass  # 鉴权失败也继续,后续业务接口会重试
        zyk_data = self.api_get_zyk("teacher/courseInfoStudent/myCourseList", {"pageNum": 1, "pageSize": 100})
        zyk_rows = []
        if isinstance(zyk_data, dict):
            zyk_rows = zyk_data.get("rows") or []
        for c in zyk_rows or []:
            if not c.get("courseId"):
                c["courseId"] = c.get("id") or c.get("resourceCourseId") or ""
            if not c.get("courseInfoId"):
                c["courseInfoId"] = c.get("resourceCourseInfoId") or c.get("courseInfoId") or ""
            if not c.get("courseName"):
                c["courseName"] = c.get("name") or c.get("courseTitle") or ""
            c["classId"] = c.get("classId", "")
            _add_course(c, "RESOURCE")

        return list(course_map.values())

    def get_course_activities(self, class_id: str, course_info_id: str, course_id: str) -> list:
        """获取当前正在进行的活动列表。"""
        data = self.api_get("spoc/courseFaceTeachActivity/getCurrentActivityList", {
            "classId": class_id, "courseInfoId": course_info_id, "courseId": course_id,
            "pageNum": "1", "pageSize": "9999", "teachType": "0", "type": "0", "requireType": "2",
        })
        if not data or data.get("code") != 200:
            return []
        acts = data.get("data", []) if isinstance(data.get("data"), list) else data.get("rows", [])
        return acts

    # ==================== 课程树(刷课依赖) ====================

    def _fetch_course_tree_level(self, course_info_id: str, class_id: str, course_id: str,
                                  level: int, parent_id: str = "0", ctype: str = "SPOC") -> list:
        """递归获取课程树的某一层节点。

        MOOC 走 AI 域 courseDesign 接口;SPOC/NZYK 走主域 study/record 接口。
        """
        if ctype == "MOOC":
            if parent_id == "0":
                data = self.api_get_ai("course/courseDesign/getStudentDesignList", {
                    "courseInfoId": course_info_id,
                    "courseId": course_id,
                })
                rows = self._extract_rows_loose(data) or self.extract_rows(data)
                if rows:
                    return rows
            data = self.api_get_ai("course/courseDesign/getCellList", {
                "courseInfoId": course_info_id,
                "courseId": course_id,
                "parentId": parent_id,
            })
            rows = self._extract_rows_loose(data) or self.extract_rows(data)
            return rows or []

        if class_id:
            data = self.api_get("spoc/courseDesign/study/record", {
                "courseId": course_id,
                "courseInfoId": course_info_id,
                "classId": class_id,
                "level": str(level),
                "parentId": parent_id,
            })
            rows = self.extract_rows(data)
            if rows:
                return rows
        # 兜底 API
        for api_path in ["spoc/courseDesign/studyList", "spoc/course/mooc/courseDesign/studyList", "spoc/nzyk/courseDesign/studyList"]:
            data = self.api_get(api_path, {
                "courseId": course_id,
                "courseInfoId": course_info_id,
                "parentId": parent_id,
            })
            rows = self.extract_rows(data)
            if rows:
                return rows
        return []

    def zyk_get_cell_file_url(self, cell_id: str) -> str:
        """资源库相对短链课件即时解析:GET teacher/courseContent/{cellId} → 绝对 fileUrl

        抓包实证(2026-09-19,11 门资源库课 2192 个课件叶子普查):树/列表接口的 fileUrl 有四种
        形态——绝对 URL(630)、`doc|zyk/g@<HEX>.ext`(845)、`doc/e@<HEX>.ext`(558)、空(159)。
        后两类相对短链 = 未绑定知识点(knowledgePointsId 为空)的那批资源,平台库里只存相对键,
        仅在"点开单课件"的详情接口里即时解析(目录分片号不可推导,本地拼前缀必 404)。
        该接口不校验课程归属,学生号 zyk_token 即可;只读、无副作用。
        返回:绝对地址;其余一律 ""(无 token / 非 200 / 仍是相对 / 异常)交调用方回落原兜底。
        """
        if not cell_id:
            return ""
        if not self.zyk_token and not self.auth_zyk_domain():
            return ""
        try:
            data = self.api_get_zyk(f"teacher/courseContent/{cell_id}")
        except Exception:
            return ""
        inner = data.get("data") if isinstance(data, dict) else None
        url = inner.get("fileUrl") if isinstance(inner, dict) else ""
        return url if str(url or "").startswith("http") else ""

    def _zyk_children(self, node: dict, depth: int, course_info_id: str):
        """资源库树单层子节点:GET teacher/courseContent/studyList。非空列表才返回。"""
        children = self.api_get_zyk("teacher/courseContent/studyList", {
            "level": depth + 1,
            "parentId": node.get("id"),
            "courseInfoId": course_info_id,
        })
        return children if isinstance(children, list) and children else None

    def zyk_get_course_tree(self, course_info_id: str, leaf_workers: Optional[int] = None) -> list:
        """资源库课程树:先拉 studyMoudleList 取模块,再递归 studyList 取子节点。

        返回扁平化的叶子节点列表。

        `leaf_workers>=2` 时改走"按层并发"的 BFS:下一层的节点 id 来自上一层响应,
        层与层之间的数据依赖锁死必须串行,但**同一层的父节点之间可以并发**——
        实测一门 1004 叶的课要 1191 次请求,串行 DFS 104.8 秒,16 路按层并发压到个位数秒。
        默认 None = 原串行递归(逐字不变),调用方按课程类型自行决定是否放宽。
        """
        if not self.zyk_token and not self.auth_zyk_domain():
            return []
        leaves = []
        try:
            modules = self.api_get_zyk("teacher/courseContent/studyMoudleList",
                                       {"courseInfoId": course_info_id})
            if not isinstance(modules, list):
                modules = []

            # 中间层 fileType="子节点" 不是叶子,必须继续递归到最深层
            def _recurse(nodes, depth=0):
                if depth > 10:  # 防止无限递归
                    return
                for node in nodes or []:
                    node["_depth"] = depth
                    children = self._zyk_children(node, depth, course_info_id)
                    if children:
                        node["children"] = children
                        _recurse(children, depth + 1)
                    else:
                        leaves.append(node)

            if not leaf_workers or leaf_workers < 2:
                _recurse(modules)
                return leaves

            frontier = [(m, 0) for m in (modules or [])]
            while frontier:
                with ThreadPoolExecutor(max_workers=min(int(leaf_workers), len(frontier))) as _ex:
                    probed = list(_ex.map(
                        lambda it: (it[0], it[1], self._zyk_children(it[0], it[1], course_info_id)),
                        frontier))
                nxt = []
                for node, depth, children in probed:
                    node["_depth"] = depth
                    if children and depth <= 10:
                        node["children"] = children
                        nxt.extend([(ch, depth + 1) for ch in children])
                    else:
                        leaves.append(node)
                frontier = nxt
        except Exception as e:
            log(f"zyk_get_course_tree 异常: {e}", "ERROR")
        return leaves

    def get_course_cells(self, course_info_id: str, class_id: str, course_id: str,
                          include_completed: bool = False, ctype: str = "SPOC",
                          leaf_workers: Optional[int] = None) -> list:
        """获取课程的叶子节点(可刷课的课件单元)。

        :param include_completed: True 包含已完成的(speed>=100)
        :param leaf_workers: 资源库树扫描的按层并发度(仅 RESOURCE 生效,None=原串行)
        :return: 叶子节点列表,每个含 id/name/fileType/fileUrl/_speed 等字段
        """
        # 资源库域:直接用 zyk_get_course_tree 返回的叶子节点
        if ctype == "RESOURCE":
            leaves = self.zyk_get_course_tree(course_info_id, leaf_workers=leaf_workers)
            leaf_cells = [l for l in leaves if (l.get("fileType") or "") not in ["作业", "考试", "测验", "exam", "homework"]]
            # 已刷过的资源库课,叶子上带 studentStudyRecord(含 speed/actualNum/totalNum)。
            # 早先这里无条件写 _speed=0,等于告诉调用方"全课都没刷过",于是每点一次刷课
            # 就把全课几千格重打一遍;现按平台口径回填,读不到时仍是 0(只多刷不漏刷)。
            for l in leaf_cells:
                _ssr = l.get("studentStudyRecord")
                _sp = _ssr.get("speed") if isinstance(_ssr, dict) else None
                try:
                    l["_speed"] = float(_sp) if _sp is not None else 0
                except (TypeError, ValueError):
                    l["_speed"] = 0
                l.setdefault("fileUrl", "")
            # RESOURCE 分支提前 return,走不到下面通用的"已完成"过滤，必须在此自己过
            if not include_completed:
                leaf_cells = [l for l in leaf_cells if l.get("_speed", 0) < 100]
            log(f"[扫描-RESOURCE] 资源库叶子节点={len(leaf_cells)} (已过滤作业/考试/测验)", "INFO")
            return leaf_cells

        leaf_cells = []
        _container_count = 0
        _leaf_count = 0
        _skipped_speed_count = 0
        _skipped_exam_count = 0
        _fallback_count = 0

        def process_node(r: dict, level: int):
            nonlocal _container_count, _leaf_count, _skipped_speed_count, _skipped_exam_count, _fallback_count
            children = r.get("children")
            if isinstance(children, list) and len(children) > 0:
                for child in children:
                    process_node(child, level + 1)
                return

            ftype = r.get("fileType", "")
            speed = self._parse_cell_speed(r, ctype)
            r["_speed"] = speed
            is_leaf = r.get("isLeaf")

            # 图片类课件即使无 fileUrl 也应作为叶子节点
            is_image_cell = self._is_image_cell(ftype, r.get("name", ""))

            is_container = False
            if not is_image_cell:
                _spoc_containers = ["父节点", "子节点", "文件夹", "单元", "模块", "项目", "任务", "篇", "章", "节", "节点", "文件", "dir", "folder", ""]
                if ctype != "MOOC":
                    _spoc_containers += ["压缩包", "其它", "其他", "源文件", "讨论"]
                # 知识点讲解不当作容器:有独立进度体系
                if ftype == "知识点讲解" and ctype != "MOOC":
                    is_container = False
                elif ftype in _spoc_containers:
                    is_container = True
                elif is_leaf == 0 or is_leaf == "0":
                    is_container = True
                elif not r.get("fileUrl") and not r.get("examId"):
                    is_container = True

            if is_container:
                _container_count += 1
                rid = r.get("id", "")
                child_rows = self._fetch_course_tree_level(course_info_id, class_id, course_id, level + 1, rid, ctype)
                if child_rows:
                    for child in child_rows:
                        process_node(child, level + 1)
                elif r.get("fileUrl") or r.get("examId") or is_image_cell:
                    _fallback_count += 1
                    if ftype not in ["考试"]:
                        if r.get("_speed", 0) < 100 or include_completed:
                            leaf_cells.append(r)
            else:
                _leaf_count += 1
                if ftype in ["考试"]:
                    _skipped_exam_count += 1
                elif r.get("_speed", 0) >= 100 and not include_completed:
                    _skipped_speed_count += 1
                else:
                    if ftype == "知识点讲解" and ctype != "MOOC":
                        r["_is_knowledge_explain"] = True
                    leaf_cells.append(r)

        rows_l1 = self._fetch_course_tree_level(course_info_id, class_id, course_id, 1, "0", ctype)
        if rows_l1:
            for r in rows_l1:
                process_node(r, 1)

        from collections import Counter
        _ftype_dist = Counter([(c.get("fileType") or "(空)") for c in leaf_cells])
        log(f"[扫描] 容器={_container_count} 叶子={_leaf_count} 回退={_fallback_count} "
            f"跳过考试={_skipped_exam_count} 跳过已完成={_skipped_speed_count} 最终={len(leaf_cells)} | "
            f"fileType分布: {dict(_ftype_dist)}", "INFO")
        return leaf_cells

    @staticmethod
    def _parse_cell_speed(r: dict, ctype: str) -> float:
        """解析课件进度(0-100),兼容多种字段名。"""
        speed = 0.0
        if ctype == "MOOC":
            speed = float(r.get("speed", 0) or 0)
            if speed <= 0:
                ssr = r.get("studentStudyRecord")
                if isinstance(ssr, dict):
                    s2 = ssr.get("speed") or ssr.get("studySpeed") or ssr.get("studySpeedPercent")
                    if s2 is not None:
                        try:
                            speed = float(s2)
                        except Exception:
                            pass
            if speed <= 0:
                s3 = r.get("studySpeed") or r.get("studySpeedPercent") or r.get("progress")
                if s3 is not None:
                    try:
                        speed = float(s3)
                    except Exception:
                        pass
        else:
            s1 = r.get("speed")
            if s1 is not None:
                try:
                    speed = float(s1)
                except Exception:
                    speed = 0
            if speed <= 0:
                ssr = r.get("studentStudyRecord")
                if isinstance(ssr, dict):
                    s2 = ssr.get("speed")
                    if s2 is not None:
                        try:
                            speed = float(s2)
                        except Exception:
                            speed = 0
            if speed <= 0:
                s3 = r.get("studySpeed") or r.get("studySpeedPercent") or r.get("progress")
                if s3 is not None:
                    try:
                        speed = float(s3)
                    except Exception:
                        pass
        return speed

    @staticmethod
    def _is_image_cell(ftype: str, cell_name: str) -> bool:
        """判断是否为图片类课件(通过 fileType 或文件名扩展名)。"""
        if ftype in IMAGE_TYPES:
            return True
        _img_exts = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".image")
        return any(cell_name.lower().endswith(ext) for ext in _img_exts)

    # ==================== 资源库心跳 ====================

    def zyk_submit_heartbeat(self, course_id: str, course_info_id: str, cell_id: str,
                              study_time: int, total_time: int,
                              parent_id: Optional[str] = None, student_id: Optional[str] = None,
                              is_image: bool = False) -> Optional[dict]:
        """资源库心跳提交:PUT /teacher/studyRecord/(末尾斜杠必需),明文 JSON。

        抓包确认:
        - 明文提交,无 AES 加密
        - URL 末尾必须带斜杠 "/",否则 Spring 路由返回 500 空 body
        - 图片类课件 totalNum=1,actualNum=1,lastNum=1(计数语义非秒数)
        """
        if not self.zyk_token and not self.auth_zyk_domain():
            return None
        if is_image:
            actual_num = 1
            last_num = 1
            total_num = 1
        else:
            actual_num = study_time
            last_num = study_time
            total_num = total_time
        payload = {
            "courseInfoId": course_info_id,
            "id": None,  # 首次提交无已有记录 id;后端自动生成
            "parentId": parent_id or "",
            "sourceId": cell_id,
            "studentId": student_id or self.stu_id or "",
            "studyTime": study_time,
            "actualNum": actual_num,
            "lastNum": last_num,
            "totalNum": total_num,
            "params": {},
        }
        return self.api_put_zyk("teacher/studyRecord/", payload)

    def zyk_refresh_progress(self, course_info_id: str) -> Optional[dict]:
        """资源库进度刷新:GET /teacher/studyRecord/flushSpeed。"""
        if not self.zyk_token and not self.auth_zyk_domain():
            return None
        return self.api_get_zyk("teacher/studyRecord/flushSpeed", {"courseInfoId": course_info_id})

    # ==================== 知识点讲解视频信息 ====================

    def get_knowledge_explain_video_info(self, cell_id: str, class_id: str) -> Optional[dict]:
        """获取"知识点讲解"cell 的视频信息。

        浏览器 Web 端用 GET /spoc/courseDesign/getStudyCellInfo?id=<cellId>&classId=<classId>
        返回 data.studentStudyRecord(含 totalNum)和 data.knowledgeExplainStudyDetails(含 fileUrl)

        :return: {"totalNum": int, "fileUrl": str, "resourceId": str} 或 None
        """
        try:
            data = self.api_get("spoc/courseDesign/getStudyCellInfo", {
                "id": cell_id, "classId": class_id,
            })
            if not data or not isinstance(data, dict):
                return None
            cell_info = data.get("data", data)
            if not isinstance(cell_info, dict):
                return None

            total_num = None
            ssr = cell_info.get("studentStudyRecord")
            if isinstance(ssr, dict):
                tn = ssr.get("totalNum")
                if tn is not None:
                    try:
                        total_num = int(tn)
                    except Exception:
                        pass

            file_url = None
            resource_id = None
            details = cell_info.get("knowledgeExplainStudyDetails")
            if isinstance(details, list) and details:
                for d in details:
                    if not isinstance(d, dict):
                        continue
                    ftype = (d.get("fileType") or "").lower()
                    if ftype in ("video", "mp4", "视频", "audio", "音频"):
                        file_url = d.get("fileUrl")
                        resource_id = d.get("resourceId")
                        tn = d.get("totalNum")
                        if tn is not None:
                            try:
                                total_num = int(tn)
                            except Exception:
                                pass
                        break

            return {"totalNum": total_num, "fileUrl": file_url, "resourceId": resource_id}
        except Exception as e:
            log(f"get_knowledge_explain_video_info 异常: {e}", "ERROR")
            return None

    # ==================== 签到 ====================

    def do_sign(self, sign_id: str, class_id: str, course_id: str, course_info_id: str,
                sign_type: str, teach_id: str = "", gesture: str = "") -> tuple:
        """一键补签(进行中签到)。

        - 二维码签到(sign_type==3):generateQrCode + verifyQrCode
        - 普通/手势/拍照签到:查本人记录 → PUT(minimal) → PUT(full) → POST 兜底

        :return: (ok: bool, msg: str)
        """
        if str(sign_type) == "3":  # 二维码签到
            return self._sign_qr_code(sign_id, teach_id)
        return self._sign_normal(sign_id, class_id, course_id, course_info_id, teach_id)

    def do_sign_ended(self, sign_id: str, class_id: str, course_id: str, course_info_id: str,
                      sign_type: str, teach_id: str = "") -> tuple:
        """已结束签到的补签逻辑:查本人记录 → PUT → POST。

        :return: (ok: bool, msg: str)
        """
        records = self._fetch_sign_records(sign_id, class_id)
        my_record = self._match_my_record(records)

        if not my_record:
            # 无记录,直接 POST 创建
            return self._post_create_sign(sign_id, class_id, course_id, course_info_id, teach_id)

        record_id = my_record.get("id")
        if not record_id:
            return self._post_create_sign(sign_id, class_id, course_id, course_info_id, teach_id,
                                          my_record)

        # 有记录ID,先 PUT 更新
        res = self.api_put("spoc/courseFaceTeachSignStudent", {
            "id": record_id, "signId": sign_id, "signResultType": "1",
        })
        if res and res.get("code") == 200:
            return True, "签到成功(补签)"
        if res and "重复签到" in str(res.get("msg", "")):
            return True, "已签到过"

        # PUT 失败,回退 POST
        put_msg = res.get("msg", "") if res else "接口请求失败"
        ok, msg = self._post_create_sign(sign_id, class_id, course_id, course_info_id, teach_id, my_record)
        if ok:
            return True, msg
        return False, f"PUT失败: {put_msg}; POST失败: {msg}"

    # ── 签到内部方法 ──

    def _sign_qr_code(self, sign_id: str, teach_id: str) -> tuple:
        """二维码签到:获取二维码编号 → 验证。

        优先从签到详情的 qrCode 字段读取(已结束签到也能获取最后二维码),
        仅当详情无 qrCode 时才调用 generateQrCode(进行中的签到)。
        """
        qr_code_number = ""

        # 1. 优先从签到详情读取 qrCode(已结束签到仍能获取)
        detail = self.api_get(f"spoc/courseFaceTeachSign/{sign_id}")
        if detail and detail.get("data"):
            qr_raw = detail["data"].get("qrCode", "")
            if qr_raw:
                try:
                    qr_obj = json.loads(qr_raw) if isinstance(qr_raw, str) else qr_raw
                    qr_code_number = str(qr_obj.get("qrCodeNumber", ""))
                except Exception:
                    qr_code_number = str(qr_raw)

        # 2. 详情无 qrCode 时,调用 generateQrCode(进行中的签到)
        if not qr_code_number:
            qr_r = self.api_get(f"spoc/courseFaceTeachSign/generateQrCode/{sign_id}")
            if qr_r and qr_r.get("code") == 200 and qr_r.get("msg"):
                try:
                    qr_obj = json.loads(qr_r["msg"]) if isinstance(qr_r["msg"], str) else qr_r["msg"]
                    qr_code_number = str(qr_obj.get("qrCodeNumber", ""))
                except Exception:
                    pass

        # 3. 仍然失败,时间戳兜底(保留原逻辑)
        if not qr_code_number:
            qr_code_number = str(int(time.time() * 1000) - 1000 - int(sign_id[-4:]) % 1000)

        result = self.api_get("spoc/courseFaceTeachSign/verifyQrCode", {
            "id": sign_id, "qrCodeNumber": qr_code_number, "teachId": teach_id,
        })
        if result and result.get("code") == 200:
            return True, f"二维码签到成功(编号:{qr_code_number})"
        if result and "重复签到" in str(result.get("msg", "")):
            return True, "已签到过"
        return False, result.get("msg", "验证二维码失败") if result else "接口请求失败"

    def _sign_normal(self, sign_id: str, class_id: str, course_id: str, course_info_id: str,
                     teach_id: str) -> tuple:
        """普通/手势签到:查本人记录 → PUT(minimal) → PUT(full) → POST 兜底。"""
        records = self._fetch_sign_records(sign_id, class_id)
        my_record = self._match_my_record(records)

        if not my_record:
            return self._post_create_sign(sign_id, class_id, course_id, course_info_id, teach_id)

        record_id = my_record.get("id")
        real_student_no = my_record.get("studentNo") or self.user_info.get("userName", "")
        real_student_name = my_record.get("studentName") or self.user_info.get("nickName", "")
        real_student_id = my_record.get("studentId") or (str(self.stu_id) if self.stu_id else "")

        if not record_id:
            return self._post_create_sign(sign_id, class_id, course_id, course_info_id, teach_id, my_record)

        # 第1次PUT:最少参数(进行中的签到用这个就够了)
        result = self.api_put("spoc/courseFaceTeachSignStudent", {
            "id": record_id, "signId": sign_id, "signResultType": "1",
        })
        if result and result.get("code") == 200:
            return True, "签到成功"
        if result and "重复签到" in str(result.get("msg", "")):
            return True, "已签到过"

        # 第2次PUT:完整参数(已结束的签到需要更多参数)
        put_msg = result.get("msg", "") if result else "接口请求失败"
        if "签到时间已结束" in put_msg or "已结束" in put_msg:
            result = self.api_put("spoc/courseFaceTeachSignStudent", {
                "id": record_id, "signId": sign_id, "signResultType": "1",
                "classId": class_id, "courseId": course_id, "courseInfoId": course_info_id,
                "teachId": teach_id,
                "studentId": real_student_id, "studentName": real_student_name, "studentNo": real_student_no,
            })
            if result and result.get("code") == 200:
                return True, "签到成功(补签)"
            if result and "重复签到" in str(result.get("msg", "")):
                return True, "已签到过"
            put_msg = result.get("msg", put_msg) if result else put_msg

        # 两次PUT都失败,回退POST
        ok, msg = self._post_create_sign(sign_id, class_id, course_id, course_info_id, teach_id, my_record)
        if ok:
            return True, msg
        return False, f"PUT失败: {put_msg}; POST失败: {msg}"

    def _fetch_sign_records(self, sign_id: str, class_id: str) -> list:
        """查询某签到活动的所有学生签到记录。"""
        records = []
        for api in ["spoc/courseFaceTeachSignStudent/page/listAll",
                    "spoc/courseFaceTeachSignStudent/list",
                    "spoc/courseFaceTeachSignStudent/page/notSign"]:
            try:
                data = self.api_get(api, {
                    "signId": sign_id, "classId": class_id,
                    "pageNum": "1", "pageSize": "1000",
                })
                rows = self.extract_rows(data)
                if rows:
                    records.extend(rows)
            except Exception as e:
                log(f"查询签到记录异常({api}): {e}", "ERROR")
        # 去重
        seen_ids = set()
        unique = []
        for r in records:
            rid = r.get("id") or r.get("studentId")
            if rid not in seen_ids:
                seen_ids.add(rid)
                unique.append(r)
        return unique

    def _match_my_record(self, records: list) -> Optional[dict]:
        """从签到记录中匹配当前用户的记录。

        匹配优先级:studentId > studentName > studentNo
        """
        my_name = self.user_info.get("nickName", "") if self.user_info else ""
        my_no = self.user_info.get("userName", "") if self.user_info else ""
        stu_id = (self.user_info.get("id") or self.user_info.get("userId") or self.stu_id) if self.user_info else self.stu_id

        for r in records:
            rsid = r.get("studentId")
            rname = r.get("studentName", "")
            rno = r.get("studentNo", "")
            if (stu_id and str(stu_id) == str(rsid)):
                return r
            if (my_name and my_name == rname):
                return r
            if (my_no and my_no == rno):
                return r
        return None

    def _post_create_sign(self, sign_id: str, class_id: str, course_id: str, course_info_id: str,
                           teach_id: str, my_record: Optional[dict] = None) -> tuple:
        """POST 创建签到记录(兜底方案)。"""
        my_name = self.user_info.get("nickName", "") if self.user_info else ""
        my_no = self.user_info.get("userName", "") if self.user_info else ""
        stu_id = (self.user_info.get("id") or self.user_info.get("userId") or self.stu_id) if self.user_info else self.stu_id

        if my_record:
            real_student_no = my_record.get("studentNo") or my_no
            real_student_name = my_record.get("studentName") or my_name
            real_student_id = my_record.get("studentId") or (str(stu_id) if stu_id else "")
        else:
            real_student_no = my_no
            real_student_name = my_name
            real_student_id = str(stu_id) if stu_id else ""

        post_body = {
            "classId": class_id, "courseId": course_id, "courseInfoId": course_info_id,
            "signId": sign_id, "signResultType": "1", "teachId": teach_id,
            "studentId": real_student_id, "studentName": real_student_name, "studentNo": real_student_no,
        }
        result = self.api_post("spoc/courseFaceTeachSignStudent", post_body)
        if result and result.get("code") == 200:
            return True, "签到成功(POST补签)"
        if result and "重复签到" in str(result.get("msg", "")):
            return True, "已签到过"
        return False, result.get("msg", "请求失败") if result else "接口请求失败"

    # ==================== 资源库(zyk)答案抓取 ====================

    def zyk_get_exam_list(self, course_info_id: str, course_id: str, category_id: Optional[str] = None) -> list:
        """资源库独立作业/考试/测验列表:走 teacher/homeworkExam/answeredExamList。

        - categoryId: 1=作业, 2=考试, 3=测验
        - flag=-1 返回全部(含已答+未答)
        """
        if not self.zyk_token and not self.auth_zyk_domain():
            return []
        cat_list = [category_id] if category_id else ["1", "2", "3"]
        cat_label = {"1": "作业", "2": "考试", "3": "测验"}
        exam_nodes = []
        for cat in cat_list:
            r = self.api_get_zyk("teacher/homeworkExam/answeredExamList", {
                "courseInfoId": course_info_id, "courseId": course_id,
                "categoryId": cat, "flag": "-1",
                "pageNum": "1", "pageSize": "100",
            })
            rows = []
            if isinstance(r, dict):
                rows = r.get("rows") or []
            for x in rows:
                cat_str = str(x.get("categoryId") or cat_label.get(cat, ""))
                status_val = str(x.get("status") or "")
                score = x.get("score")
                has_score = score is not None and str(score) != "" and str(score) != "-"
                if cat_str == "1":
                    is_submit = status_val == "1" or has_score
                elif cat_str == "3":
                    is_submit = status_val == "2" or has_score
                else:
                    is_submit = status_val == "2" or has_score
                exam_nodes.append({
                    "id": str(x.get("id")), "examId": str(x.get("id")),
                    "examName": x.get("name", ""), "name": x.get("name", ""),
                    "type": cat_label.get(cat, "作业"), "fileType": cat_label.get(cat, ""),
                    "categoryId": cat, "taskId": x.get("taskId") or "0",
                    "groupId": x.get("groupId") or x.get("projectGroupId") or "0",
                    "courseId": course_id, "courseInfoId": course_info_id,
                    "submit": is_submit, "score": str(score) if score is not None else "-",
                })
        return exam_nodes

    def zyk_get_exam_paper(self, exam_id: str) -> Optional[dict]:
        """资源库试卷:用 teacher/homeworkExam/paper 获取试卷题目。

        返回 data 含 questions,每题 dataJson 含 IsAnswer 字段。
        """
        if not self.zyk_token and not self.auth_zyk_domain():
            return None
        data = self.api_get_zyk("teacher/homeworkExam/paper", {"id": exam_id, "examId": exam_id})
        if isinstance(data, dict):
            return data
        return None

    def zyk_get_homework_answers(self, exam_id: str) -> list:
        """资源库作业/测验/考试答案提取。

        策略:
        - examRecordPaperList.answer 是标准答案来源(服务端给出)
        - 对正常未提交的测验,answer 字段能完整返回标准答案
        """
        if not self.zyk_token and not self.auth_zyk_domain():
            return []
        # 1. 优先用 examRecordPaperList(answer 字段是标准答案)
        data = self.api_get_zyk("teacher/taskExamProblemRecord/examRecordPaperList", {
            "examId": exam_id, "taskId": "0", "groupId": "0",
        })
        rows = []
        if isinstance(data, dict):
            rows = data.get("data") or []
        if not isinstance(rows, list) or not rows:
            return self._zyk_get_answers_from_paper(exam_id)

        has_answer = sum(1 for q in rows if q.get("answer"))
        log(f"[zyk答案] examRecordPaperList 接口: {len(rows)} 题,含标准答案 {has_answer} 题", "INFO")

        # 合并 paper 数据(用于提交时的 optionSort)
        paper = self.zyk_get_exam_paper(exam_id)
        if paper and paper.get("questions"):
            paper_map = {}
            for pq in paper["questions"]:
                pid = str(pq.get("id") or pq.get("paperId") or pq.get("questionId") or "")
                if pid:
                    paper_map[pid] = pq
            for q in rows:
                qid = str(q.get("id") or q.get("paperId") or q.get("questionId") or "")
                pq = paper_map.get(qid)
                if pq:
                    if pq.get("dataJson") and not q.get("dataJson"):
                        q["dataJson"] = pq["dataJson"]
                    if pq.get("optionSort") and not q.get("optionSort"):
                        q["optionSort"] = pq["optionSort"]

        no_answer_count = sum(1 for q in rows if not q.get("answer"))
        if no_answer_count > 0:
            log(f"[zyk答案] {no_answer_count} 题标准答案为空(可能是反复提交导致数据污染)", "WARNING")

        for q in rows:
            self._normalize_zyk_question(q)
        return rows

    def _zyk_get_answers_from_paper(self, exam_id: str) -> list:
        """从 paper 接口提取答案(降级方案,IsAnswer 可能是用户已提交答案)。"""
        paper = self.zyk_get_exam_paper(exam_id)
        if not paper or not paper.get("questions"):
            return []
        paper_qs = paper["questions"]
        has_is_answer = 0
        for q in paper_qs:
            dj = q.get("dataJson") or q.get("optionSort")
            if dj:
                try:
                    dj_list = json.loads(dj) if isinstance(dj, str) else dj
                    if isinstance(dj_list, list) and any(
                        str(opt.get("IsAnswer", "")).lower() == "true" for opt in dj_list
                    ):
                        has_is_answer += 1
                except Exception:
                    pass
        if has_is_answer == 0:
            return []
        log(f"[zyk答案] 降级用 paper 接口: {len(paper_qs)} 题,含 IsAnswer 标记 {has_is_answer} 题", "WARNING")
        for q in paper_qs:
            if q.get("answer"):
                continue
            dj = q.get("dataJson") or q.get("optionSort")
            if not dj:
                continue
            try:
                dj_list = json.loads(dj) if isinstance(dj, str) else dj
                if isinstance(dj_list, list):
                    type_name = str(q.get("typeName") or "")
                    is_judgment = "判断" in type_name
                    if is_judgment:
                        true_vals = [str(opt.get("name", "")) for opt in dj_list
                                     if str(opt.get("IsAnswer", "")).lower() == "true"]
                    else:
                        true_vals = [str(opt.get("SortOrder", "")) for opt in dj_list
                                     if str(opt.get("IsAnswer", "")).lower() == "true"]
                        if "单选" in type_name and len(true_vals) > 1:
                            true_vals = true_vals[:1]
                    if true_vals:
                        q["answer"] = ",".join(true_vals)
                        q["rawAnswer"] = q["answer"]
                        q["_answer_from_isanswer"] = True
            except Exception:
                pass
        for q in paper_qs:
            self._normalize_zyk_question(q)
        return paper_qs

    @staticmethod
    def _normalize_zyk_question(q: dict) -> None:
        """规范化 zyk 题目数据,使其与 SPOC 兼容。

        zyk vs SPOC 差异:typeId 3/4 互换;判断题 answer A/B→1/0;填空题 answer JSON→纯文本。
        """
        type_name = str(q.get("typeName") or "")
        q["rawAnswer"] = q.get("answer", "")
        if "客观填空" in type_name:
            q["typeId"] = "7"
        elif "填空" in type_name:
            q["typeId"] = "4"
        elif "判断" in type_name:
            q["typeId"] = "3"
        if "判断" in type_name:
            ans = str(q.get("answer") or "")
            if ans == "A":
                q["answer"] = "1"
            elif ans == "B":
                q["answer"] = "0"
            dj = q.get("dataJson")
            if isinstance(dj, str) and dj.startswith("["):
                try:
                    dj_list = json.loads(dj)
                    for opt in dj_list:
                        so = opt.get("SortOrder")
                        if so == "A":
                            opt["SortOrder"] = 0
                        elif so == "B":
                            opt["SortOrder"] = 1
                    q["dataJson"] = json.dumps(dj_list, ensure_ascii=False)
                except Exception:
                    pass
        if "填空" in type_name and "客观" not in type_name:
            ans = q.get("answer")
            if isinstance(ans, str) and ans.startswith("["):
                try:
                    ans_list = json.loads(ans)
                    texts = []
                    for item in ans_list:
                        content = item.get("Content", "")
                        text = re.sub(r'<[^>]+>', '', content).strip()
                        if text:
                            texts.append(text)
                    if texts:
                        q["answer"] = "；".join(texts)
                except Exception:
                    pass

    # ==================== 资源库(zyk)提交 ====================

    def zyk_submit_exam(self, exam_id: str, course_info_id: str, course_id: str,
                        cell_id: str, questions: list, category_id: str = "1") -> Optional[dict]:
        """资源库作业/考试最终提交:POST /teacher/homeworkExam/add(AES 加密)。

        - Content-Type: text/plain,body 为 AES-128-ECB + PKCS7 加密的 Base64 密文
        - 固定密钥: djekiytolkijduey(来自前端 JS 逆向)
        - 必须一次性提交所有题目(含未作答的,answer="")
        """
        if not self.zyk_token and not self.auth_zyk_domain():
            return None
        import random as _random
        valid_count = sum(1 for q in questions if (q.get("rawAnswer") or q.get("answer", "")))
        exam_time = _random.randint(60, 120) + valid_count * _random.randint(8, 20)
        log(f"[zyk提交] 伪造答题时长: {exam_time}秒 ({valid_count}题)", "DEBUG")

        record_list = []
        for idx, q in enumerate(questions):
            raw_ans = q.get("rawAnswer") or q.get("answer", "")
            paper_id = q.get("paperId") or q.get("id") or q.get("questionId") or ""
            type_name = str(q.get("typeName") or "")
            is_objective_blank = "客观填空" in type_name
            type_id = str(q.get("typeId") or "")
            is_blank_type = is_objective_blank or type_id == "4"
            if is_blank_type and raw_ans:
                try:
                    ans_obj = json.loads(raw_ans) if isinstance(raw_ans, str) else raw_ans
                    if isinstance(ans_obj, dict):
                        items = ans_obj.get("options", [ans_obj]) if isinstance(ans_obj.get("options"), list) else [ans_obj]
                    elif isinstance(ans_obj, list):
                        items = ans_obj
                    else:
                        items = []
                    texts = []
                    for it in items:
                        if isinstance(it, dict):
                            texts.append(str(it.get("Content", "") or ""))
                        elif isinstance(it, str):
                            texts.append(it)
                    if texts:
                        raw_ans = json.dumps(texts, ensure_ascii=False)
                except Exception:
                    pass
            item = {
                "questionNo": idx, "answer": raw_ans or "", "paperId": paper_id,
                "knowledgePointsId": q.get("knowledgePointsId") or None,
            }
            is_non_option_type = type_id in ("3", "4", "5", "6", "7")
            if raw_ans and not is_non_option_type:
                opt_sort = q.get("dataJson") or q.get("optionSort") or ""
                if opt_sort:
                    item["optionSort"] = opt_sort if isinstance(opt_sort, str) else json.dumps(opt_sort, ensure_ascii=False)
            record_list.append(item)

        if not record_list:
            return {"code": 500, "msg": "无题目可提交"}

        payload = {
            "categoryId": str(category_id), "courseId": course_id, "courseInfoId": course_info_id,
            "examId": exam_id, "examTime": exam_time, "groupId": "0", "isLast": True, "status": "",
            "taskExamProblemRecordList": record_list, "updateBy": "", "updateTime": "", "userId": "",
            "examName": "", "resitId": "", "device": 1,
        }

        FIXED_KEY = b"djekiytolkijduey"
        try:
            json_str = json.dumps(payload, ensure_ascii=False)
            cipher = AES.new(FIXED_KEY, AES.MODE_ECB)
            padded = pad(json_str.encode("utf-8"), AES.block_size)
            encrypted = cipher.encrypt(padded)
            body_b64 = base64.b64encode(encrypted).decode()
        except Exception as e:
            log(f"[zyk提交] 加密失败: {e}", "ERROR")
            return {"code": 500, "msg": f"加密失败: {e}"}

        try:
            headers = {
                "Content-Type": "text/plain",
                "Authorization": f"Bearer {self.zyk_token}",
                "Referer": "https://zyk.icve.com.cn/icve-study/courseDetailed",
            }
            resp = self.session.post(f"{ZYK_BASE_URL}/teacher/homeworkExam/add",
                                     data=body_b64.encode("utf-8"), headers=headers, timeout=30)
            r = resp.json()
        except Exception as e:
            log(f"[zyk提交] 请求异常: {e}", "ERROR")
            return {"code": 500, "msg": f"请求异常: {e}"}

        if isinstance(r, dict) and r.get("code") == 200:
            record_id = str(r.get("msg") or r.get("data") or "")
            log(f"[zyk提交] 加密提交成功: {len(record_list)}题, recordId={record_id}", "INFO")
            return {"code": 200, "msg": f"已自动提交 {len(record_list)} 题",
                    "success": len(record_list), "fail": 0, "recordId": record_id}
        else:
            log(f"[zyk提交] 提交失败: {r}", "DEBUG")
            return {"code": 500, "msg": "提交失败", "fail_details": r}

    # ==================== 同学ID获取 / 教师号预览 ====================

    def _get_class_student_ids(self, class_id: str, course_info_id: str) -> list:
        """获取班级同学列表(不含自己)。"""
        data = self.api_get("spoc/courseInfoStudent/list", {
            "classId": class_id, "courseInfoId": course_info_id,
            "pageNum": "1", "pageSize": "200",
        })
        rows = self._extract_rows_loose(data) or self.extract_rows(data) or []
        if not rows:
            data = self.api_get("spoc/courseInfoStudent/score/list", {
                "classId": class_id, "courseInfoId": course_info_id,
                "pageNum": "1", "pageSize": "200",
            })
            rows = self._extract_rows_loose(data) or self.extract_rows(data) or []
        students = []
        for r in rows:
            uid = r.get("studentId", "") or r.get("userId", "") or r.get("id", "") or r.get("stuId", "")
            name = r.get("studentName", "") or r.get("userName", "") or r.get("realName", "") or r.get("name", "")
            if uid and str(uid) != str(self.stu_id):
                students.append({"userId": uid, "name": name})
        return students

    def _get_mooc_student_ids(self, course_info_id: str, course_id: str) -> list:
        """获取 MOOC 课程同学列表。"""
        data = self.api_get_ai("course/courseInfoStudent/list", {
            "courseInfoId": course_info_id, "courseId": course_id,
            "pageNum": "1", "pageSize": "200",
        })
        rows = self._extract_rows_loose(data) or self.extract_rows(data) or []
        students = []
        for r in rows:
            uid = r.get("studentId", "") or r.get("userId", "") or r.get("id", "") or r.get("stuId", "")
            name = r.get("studentName", "") or r.get("userName", "") or r.get("realName", "") or r.get("name", "")
            if uid and str(uid) != str(self.stu_id):
                students.append({"userId": uid, "name": name})
        return students

    def _teacher_api_get(self, headers: dict, path: str, params: dict, label: str):
        """用教师号 token 调用 API 并提取题目列表。"""
        try:
            resp = requests.get(f"{BASE_URL}/{path}", params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            raw_data = data.get("data")
            if data.get("code") == 200:
                if isinstance(raw_data, list) and raw_data:
                    return raw_data
                elif isinstance(raw_data, dict):
                    qs = (raw_data.get("questions") or raw_data.get("questionList")
                          or raw_data.get("examQuestionList") or raw_data.get("paperQuestionList") or [])
                    if qs:
                        return qs
        except Exception as ex:
            log(f"[教师号] {label}异常: {ex}", "DEBUG")
        return None

    def get_exam_preview_with_teacher(self, teacher_token: str, exam_id: str, class_id: str,
                                       course_info_id: str, course_id: str = "",
                                       stu_task_id: str = "", stu_user_id: str = ""):
        """用教师号 token 获取题目和答案(绕过 answerReleaseTime 限制)。

        依次尝试多个 API,直到成功获取含答案的题目。
        :return: (questions_list, source_info) 或 (None, None)
        """
        try:
            headers = {
                "Authorization": f"Bearer {teacher_token}",
                "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36",
            }
            # 方式0: selectExamRecordPaper(最优先,不受答案公布时间限制)
            if stu_task_id:
                qs = self._teacher_api_get(headers, "spoc/taskExamProblemRecord/selectExamRecordPaper", {
                    "taskId": stu_task_id, "examId": exam_id,
                    "classId": class_id, "userId": stu_user_id or "",
                }, "selectExamRecordPaper")
                if qs:
                    return qs, {"_source": "teacher_selectExamRecordPaper"}
            # 方式1: examRecordPaperList2
            if stu_task_id:
                try:
                    resp = requests.get(f"{BASE_URL}/spoc/taskExamProblemRecord/examRecordPaperList2", params={
                        "taskId": stu_task_id, "groupId": "", "examId": exam_id,
                        "classId": class_id, "userId": stu_user_id or "",
                    }, headers=headers, timeout=15)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("code") == 200:
                            raw = data.get("data")
                            if isinstance(raw, list) and raw:
                                return raw, {"_source": "teacher_examRecordPaperList"}
                            elif isinstance(raw, dict):
                                qs = raw.get("questions") or raw.get("questionList") or []
                                if qs:
                                    return qs, {"_source": "teacher_examRecordPaperList"}
                except Exception:
                    pass
            # 方式2-6: 其他教师号 API
            api_attempts = [
                ("spoc/exam/preview", {"id": exam_id, "examId": exam_id, "classId": class_id,
                                       "courseInfoId": course_info_id, "courseId": course_id, "groupId": "0"}, "preview"),
                ("spoc/file/exam/detail", {"id": exam_id}, "file/exam/detail"),
                ("spoc/exam/record/classGroup/answerViewNew", {"examId": exam_id, "classId": class_id,
                                                               "courseInfoId": course_info_id}, "answerViewNew"),
                ("spoc/fast/course/exam/detail", {"id": exam_id, "classId": class_id,
                                                  "courseInfoId": course_info_id}, "fast/course/exam/detail"),
                ("spoc/file/exam/student/answer/view", {"examId": exam_id, "classId": class_id}, "student/answer/view"),
            ]
            for api_path, params, label in api_attempts:
                qs = self._teacher_api_get(headers, api_path, params, label)
                if qs:
                    return qs, {"_source": f"teacher_{label}"}
            return None, None
        except Exception as ex:
            log(f"[教师号] 获取答案异常: {ex}", "DEBUG")
            return None, None

    # ==================== 同学正确答案扫描 ====================

    def _get_classmate_correct_answers(self, exam_id: str, class_id: str,
                                        course_info_id: Optional[str] = None,
                                        course_id: Optional[str] = None) -> dict:
        """扫描 self.accounts 中的同学 Token,拉取其已提交答卷并整理出正确答案。

        :return: {question_id: {"answer":..., "dataJson":..., "sub_problems":...}}
        """
        correct_map = {}
        try:
            if not self.accounts:
                return {}
            classmates = self._get_class_student_ids(class_id, course_info_id)
            if not classmates:
                return {}
            classmate_ids = {str(c["userId"]) for c in classmates}
            for classmate_name, acc in self.accounts.items():
                if not isinstance(acc, dict):
                    continue
                c_token = acc.get("token")
                c_uid = str(acc.get("stuId") or "")
                if not c_token or c_uid == str(self.stu_id):
                    continue
                if c_uid not in classmate_ids:
                    continue
                headers = {
                    "Authorization": f"Bearer {c_token}",
                    "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36",
                    "platform-type": "android",
                }
                try:
                    r_rec = requests.get(f"{BASE_URL}/spoc/exam/record/list", params={
                        "classId": class_id, "examId": exam_id,
                        "pageSize": "10", "pageNum": "1",
                    }, headers=headers, timeout=5)
                    if r_rec.status_code == 200:
                        rec_data = r_rec.json()
                        if rec_data.get("code") == 200:
                            rows = rec_data.get("rows") or []
                            if not rows and isinstance(rec_data.get("data"), list):
                                rows = rec_data["data"]
                            for r in rows:
                                rid = r.get("id") or r.get("taskId")
                                if rid:
                                    r_paper = requests.get(f"{BASE_URL}/spoc/taskExamProblemRecord/examRecordPaperList2", params={
                                        "taskId": rid, "groupId": "", "examId": exam_id,
                                        "classId": class_id, "userId": c_uid,
                                    }, headers=headers, timeout=5)
                                    if r_paper.status_code == 200:
                                        p_data = r_paper.json()
                                        if p_data.get("code") == 200 and isinstance(p_data.get("data"), list):
                                            extracted = 0
                                            for cq in p_data["data"]:
                                                cq_id = cq.get("questionId") or cq.get("id")
                                                cq_is_right = cq.get("isRight")
                                                cq_ans = cq.get("recordAnswer") or cq.get("stuAnswer") or cq.get("studentAnswer")
                                                if cq_id and cq_is_right is True and cq_ans:
                                                    correct_map[str(cq_id)] = {
                                                        "answer": cq_ans, "dataJson": cq.get("dataJson"),
                                                        "sub_problems": cq.get("taskExamSubProblemRecordPaperVos") or cq.get("questionSubList") or [],
                                                    }
                                                    extracted += 1
                                            if extracted > 0:
                                                log(f"[同学答案] 从同学 {classmate_name} 提取 {extracted} 题正确答案", "DEBUG")
                except Exception:
                    pass
        except Exception as e:
            log(f"[同学答案] 扫描异常: {e}", "DEBUG")
        return correct_map

    def _merge_correct_answers(self, qs: list, correct_map: dict) -> list:
        """将同学正确答案融合到题目列表中。"""
        if not qs:
            return qs
        if not correct_map:
            correct_map = {}
        merged_count = 0
        for q in qs:
            if q.get("answer") or q.get("correctAnswer") or q.get("rightAnswer"):
                continue
            qid = str(q.get("questionId") or q.get("id") or "")
            if q.get("isRight") is True:
                ans = q.get("recordAnswer") or q.get("stuAnswer") or q.get("studentAnswer")
                if ans:
                    parsed_ans = ans
                    if isinstance(ans, str) and ans.startswith("[") and ans.endswith("]"):
                        try:
                            arr = json.loads(ans)
                            if isinstance(arr, list):
                                parsed_ans = ",".join(str(x) for x in arr)
                        except Exception:
                            pass
                    q["answer"] = parsed_ans
                    q["correctAnswer"] = parsed_ans
                    q["rightAnswer"] = parsed_ans
                    merged_count += 1
                    continue
            if qid in correct_map:
                c_info = correct_map[qid]
                c_ans = c_info["answer"]
                parsed_ans = c_ans
                if isinstance(c_ans, str) and c_ans.startswith("[") and c_ans.endswith("]"):
                    try:
                        arr = json.loads(c_ans)
                        if isinstance(arr, list):
                            parsed_ans = ",".join(str(x) for x in arr)
                    except Exception:
                        pass
                q["answer"] = parsed_ans
                q["correctAnswer"] = parsed_ans
                q["rightAnswer"] = parsed_ans
                c_dj = c_info["dataJson"]
                if c_dj:
                    q["dataJson"] = c_dj
                elif q.get("dataJson") and parsed_ans:
                    try:
                        dj_str = q["dataJson"]
                        dj_list = json.loads(dj_str) if isinstance(dj_str, str) else dj_str
                        if isinstance(dj_list, list) and dj_list:
                            ans_indices = set()
                            for a in str(parsed_ans).split(","):
                                a = a.strip()
                                if a.isdigit():
                                    ans_indices.add(int(a))
                            for idx, opt in enumerate(dj_list):
                                opt["IsAnswer"] = (idx in ans_indices)
                            q["dataJson"] = json.dumps(dj_list, ensure_ascii=False)
                    except Exception:
                        pass
                c_subs = c_info["sub_problems"]
                q_subs = q.get("taskExamSubProblemRecordPaperVos") or q.get("questionSubList") or []
                if c_subs and q_subs:
                    c_sub_map = {}
                    for cs in c_subs:
                        cs_sub_id = str(cs.get("questionSubId") or cs.get("id") or "")
                        cs_qid = str(cs.get("questionId") or "")
                        if cs_sub_id:
                            c_sub_map[cs_sub_id] = cs
                        if cs_qid:
                            c_sub_map[f"qid_{cs_qid}"] = cs
                    for qs_item in q_subs:
                        qs_sub_id = str(qs_item.get("questionSubId") or qs_item.get("id") or "")
                        qs_qid = str(qs_item.get("questionId") or "")
                        c_sub = c_sub_map.get(qs_sub_id) or c_sub_map.get(f"qid_{qs_qid}")
                        if c_sub:
                            c_sub_ans = c_sub.get("recordAnswer") or c_sub.get("stuAnswer") or c_sub.get("answer")
                            if c_sub_ans:
                                parsed_sub_ans = c_sub_ans
                                if isinstance(c_sub_ans, str) and c_sub_ans.startswith("[") and c_sub_ans.endswith("]"):
                                    try:
                                        arr = json.loads(c_sub_ans)
                                        if isinstance(arr, list):
                                            parsed_sub_ans = ",".join(str(x) for x in arr)
                                    except Exception:
                                        pass
                                qs_item["answer"] = parsed_sub_ans
                                qs_item["correctAnswer"] = parsed_sub_ans
                                qs_item["rightAnswer"] = parsed_sub_ans
                merged_count += 1
        log(f"[同学答案] 成功融合 {merged_count}/{len(qs)} 题的标准答案", "DEBUG")
        return qs

    # ==================== SPOC/MOOC 答案检索主入口 ====================

    def find_classmate_answers(self, exam_id: str, class_id: str, course_info_id: str,
                                course_id: str, ctype: str = "SPOC"):
        """答案检索主入口(学生号直取 + 同学正确答案融合)。

        答案数据源优先级:
        1. 学生号 examRecordPaperList2 / examRecordPaperList(answer 标准答案)
        2. 进入考试/作业获取真正 taskId 后再取
        3. _get_classmate_correct_answers(同学 token 扫包 isRight=true 的答案)

        :return: (questions_list, record_info) 或 (None, None)
        """
        if ctype == "MOOC":
            return self._find_mooc_answers(exam_id, course_info_id, course_id)
        return self._find_spoc_answers(exam_id, class_id, course_info_id, course_id)

    def _find_mooc_answers(self, exam_id: str, course_info_id: str, course_id: str):
        """MOOC 答案检索。"""
        paper_data = self.api_get_ai("course/exam/paper", {"id": exam_id, "groupId": "0"})
        if paper_data and paper_data.get("questions"):
            paper_record = paper_data.get("taskExamRecord") or {}
            rec_id = paper_record.get("id", "")
            if rec_id:
                info_data = self.api_get_ai("course/exam/record/getInfo", {
                    "courseInfoId": course_info_id, "taskId": rec_id,
                    "examId": exam_id, "studentId": self.stu_id, "type": "1",
                })
                if info_data and info_data.get("code") == 200 and info_data.get("data"):
                    d = info_data["data"]
                    qs = d.get("questions") or d.get("questionList") or []
                    if qs and sum(1 for q in qs if q.get("answer")) > 0:
                        return qs, paper_record
        # 降级:getExamListByStudent
        for cat_id in ["1", "2", "3"]:
            page = 1
            while True:
                data = self.api_get_ai("course/exam/record/getExamListByStudent", {
                    "courseInfoId": course_info_id, "courseId": course_id,
                    "categoryId": cat_id, "pageNum": str(page), "pageSize": "20",
                    "name": "", "submitStatus": "",
                })
                rows = self._extract_rows_loose(data) or self.extract_rows(data) or []
                if not rows:
                    break
                for r in rows:
                    if str(r.get("id", "")) == str(exam_id) or str(r.get("examId", "")) == str(exam_id):
                        tid = r.get("taskId", "")
                        if tid:
                            info_data = self.api_get_ai("course/exam/record/getInfo", {
                                "courseInfoId": course_info_id, "taskId": tid,
                                "examId": exam_id, "studentId": self.stu_id, "type": "1",
                            })
                            if info_data and info_data.get("code") == 200 and info_data.get("data"):
                                d = info_data["data"]
                                qs = d.get("questions") or d.get("questionList") or []
                                if qs:
                                    return qs, r
                total = int(data.get("total", 0)) if data and isinstance(data, dict) else 0
                if page * 20 >= total:
                    break
                page += 1
        return None, None

    def _find_spoc_answers(self, exam_id: str, class_id: str, course_info_id: str, course_id: str):
        """SPOC 答案检索(三阶段降级 + 同学扫包融合)。"""
        try:
            info_data = self.api_get("spoc/exam/app/info", {
                "id": exam_id, "examId": exam_id, "classId": class_id,
                "courseInfoId": course_info_id, "courseId": course_id,
            })
            if info_data and info_data.get("code") == 200 and info_data.get("data"):
                d = info_data["data"]
                task_id = d.get("taskId") or d.get("id")
                if task_id:
                    # Phase 1: examRecordPaperList2
                    paper_data = self.api_get("spoc/taskExamProblemRecord/examRecordPaperList2", {
                        "taskId": task_id, "groupId": "", "examId": exam_id,
                        "classId": class_id, "userId": self.stu_id,
                    })
                    if paper_data and paper_data.get("code") == 200 and paper_data.get("data"):
                        qs = paper_data["data"]
                        if isinstance(qs, list) and qs:
                            self._extract_isanswer_from_datajson(qs)
                            if sum(1 for q in qs if q.get("answer") or q.get("recordAnswer")) > 0:
                                still_no = sum(1 for q in qs if not q.get("answer") and not q.get("correctAnswer"))
                                if still_no > 0:
                                    cmap = self._get_classmate_correct_answers(exam_id, class_id, course_info_id, course_id)
                                    qs = self._merge_correct_answers(qs, cmap or {})
                                return qs, d
                    # Phase 2: examRecordPaperList(不带2)
                    paper_data_v1 = self.api_get("spoc/taskExamProblemRecord/examRecordPaperList", {
                        "taskId": task_id, "groupId": "", "examId": exam_id,
                        "classId": class_id, "userId": self.stu_id,
                    })
                    if paper_data_v1 and paper_data_v1.get("code") == 200 and paper_data_v1.get("data"):
                        qs_v1 = paper_data_v1["data"]
                        if isinstance(qs_v1, list) and qs_v1:
                            self._extract_isanswer_from_datajson(qs_v1)
                            if sum(1 for q in qs_v1 if q.get("answer") or q.get("recordAnswer")) > 0:
                                still_no = sum(1 for q in qs_v1 if not q.get("answer") and not q.get("correctAnswer"))
                                if still_no > 0:
                                    cmap = self._get_classmate_correct_answers(exam_id, class_id, course_info_id, course_id)
                                    qs_v1 = self._merge_correct_answers(qs_v1, cmap or {})
                                return qs_v1, d
                    # Phase 3: 进入考试获取真正 taskId
                    qs = self._enter_exam_and_get_answers(exam_id, class_id, course_info_id, course_id)
                    if qs:
                        return qs, d
        except Exception as ex:
            log(f"[答案调试] SPOC 学生号直取异常: {ex}", "DEBUG")
        return None, None

    def _extract_isanswer_from_datajson(self, qs: list) -> None:
        """从 dataJson 的 IsAnswer 字段提取答案到 answer 字段。"""
        for q in qs:
            if q.get("answer"):
                continue
            dj = q.get("dataJson")
            if not dj:
                continue
            try:
                dj_list = json.loads(dj) if isinstance(dj, str) else dj
                if isinstance(dj_list, list):
                    true_opts = [i for i, opt in enumerate(dj_list)
                                 if str(opt.get("IsAnswer", "")).lower() == "true"]
                    if true_opts:
                        q["answer"] = ",".join(str(i) for i in true_opts)
            except Exception:
                pass

    def _enter_exam_and_get_answers(self, exam_id: str, class_id: str,
                                     course_info_id: str, course_id: str):
        """进入考试/作业获取真正 taskId,再拉取答案。"""
        try:
            _orig_ua = self.session.headers.get("User-Agent", "")
            _orig_platform = self.session.headers.get("platform-type", "")
            _mh = {}
            for hk in ["log-equipment-app-version", "log-equipment-model", "log-equipment-api-version", "log-equipment"]:
                _mh[hk] = self.session.headers.pop(hk, None)
            self.session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            self.session.headers["platform-type"] = "windows"

            enter_resp = self.api_post("spoc/exam/loadExamPaper", {
                "device": "1", "id": exam_id, "resitId": None, "classId": class_id,
            })
            _use_release = True
            if not enter_resp or enter_resp.get("code") != 200 or not enter_resp.get("data"):
                self.session.headers["User-Agent"] = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36"
                self.session.headers["platform-type"] = "android"
                for hk, hv in _mh.items():
                    if hv is not None:
                        self.session.headers[hk] = hv
                enter_resp = self.api_post("spoc/file/exam/detail/with/student", {
                    "examId": exam_id, "classId": class_id, "device": "2", "groupId": "0",
                })
                _use_release = False

            qs = None
            if enter_resp and enter_resp.get("code") == 200 and enter_resp.get("data"):
                ed = enter_resp["data"]
                enter_task_id = ed.get("taskId") or ed.get("id")
                if enter_task_id:
                    paper_data = self.api_get("spoc/taskExamProblemRecord/examRecordPaperList", {
                        "taskId": enter_task_id, "groupId": "", "examId": exam_id,
                        "classId": class_id, "userId": self.stu_id,
                    })
                    if paper_data and paper_data.get("code") == 200 and paper_data.get("data"):
                        qs_enter = paper_data["data"]
                        if isinstance(qs_enter, list) and qs_enter:
                            self._extract_isanswer_from_datajson(qs_enter)
                            if sum(1 for q in qs_enter if q.get("answer") or q.get("recordAnswer")) > 0:
                                qs = qs_enter
                                still_no = sum(1 for q in qs if not q.get("answer") and not q.get("correctAnswer"))
                                if still_no > 0:
                                    cmap = self._get_classmate_correct_answers(exam_id, class_id, course_info_id, course_id)
                                    qs = self._merge_correct_answers(qs, cmap or {})
                            else:
                                qs = qs_enter
                    if _use_release:
                        try:
                            self.api_post("spoc/exam/releaseSession", {
                                "examId": exam_id, "classId": class_id, "taskId": enter_task_id,
                            })
                        except Exception:
                            pass

            self.session.headers["User-Agent"] = _orig_ua
            if _orig_platform:
                self.session.headers["platform-type"] = _orig_platform
            for hk, hv in _mh.items():
                if hv is not None:
                    self.session.headers[hk] = hv
            return qs
        except Exception as ex:
            log(f"[答案调试] 进入考试异常: {ex}", "DEBUG")
            return None

    # ==================== 自建题库匹配(兜底) ====================

    @staticmethod
    def _normalize_title_for_match(title: str) -> str:
        """题干归一化:去 HTML 标签 + 去空白 + 转小写。"""
        if not title:
            return ""
        s = str(title)
        s = re.sub(r'<[^>]+>', '', s)
        s = re.sub(r'&[a-zA-Z]+;', '', s)
        s = re.sub(r'\s+', '', s)
        s = s.replace('\u3000', '')
        return s.lower()

    def _load_question_bank(self) -> list:
        """加载题库目录下所有 *.json 文件并合并(带 mtime 缓存)。"""
        bank_dir = self.question_bank_dir
        if not bank_dir or not os.path.exists(bank_dir):
            return []
        try:
            json_files = sorted(f for f in os.listdir(bank_dir) if f.lower().endswith(".json"))
            if not json_files:
                return []
            latest_mtime = 0
            for fname in json_files:
                try:
                    mt = os.path.getmtime(os.path.join(bank_dir, fname))
                    if mt > latest_mtime:
                        latest_mtime = mt
                except Exception:
                    pass
            if self._question_bank_cache is not None and latest_mtime == self._question_bank_mtime:
                return self._question_bank_cache
            merged = []
            for fname in json_files:
                try:
                    with open(os.path.join(bank_dir, fname), "r", encoding="utf-8") as f:
                        items = json.load(f)
                    if isinstance(items, list):
                        merged.extend(items)
                    elif isinstance(items, dict) and isinstance(items.get("questions"), list):
                        merged.extend(items["questions"])
                except Exception:
                    pass
            seen = {}
            for item in merged:
                if not isinstance(item, dict):
                    continue
                norm = self._normalize_title_for_match(item.get("title", ""))
                if norm:
                    seen[norm] = item
            bank = list(seen.values())
            for item in bank:
                item["_title_norm"] = self._normalize_title_for_match(item.get("title", ""))
            self._question_bank_cache = bank
            self._question_bank_mtime = latest_mtime
            log(f"[题库] 加载完成: {len(json_files)} 个文件, 去重后 {len(bank)} 题", "DEBUG")
            return bank
        except Exception:
            return []

    def find_question_bank_answer(self, title: str, type_id: Optional[str] = None) -> Optional[dict]:
        """从自建题库匹配答案。"""
        if not title:
            return None
        bank = self._load_question_bank()
        if not bank:
            return None
        norm_title = self._normalize_title_for_match(title)
        if not norm_title:
            return None
        # 精确匹配优先
        for item in bank:
            if item.get("_title_norm") == norm_title:
                if type_id and str(item.get("typeId", "")) != str(type_id):
                    continue
                return {"answer": item.get("answer", ""), "options": item.get("options", []) or [],
                        "typeId": str(item.get("typeId", "")), "title": item.get("title", "")}
        # 模糊匹配
        for item in bank:
            bank_title = item.get("_title_norm", "")
            if not bank_title or len(bank_title) < 5:
                continue
            if type_id and str(item.get("typeId", "")) != str(type_id):
                continue
            if bank_title in norm_title or norm_title in bank_title:
                return {"answer": item.get("answer", ""), "options": item.get("options", []) or [],
                        "typeId": str(item.get("typeId", "")), "title": item.get("title", "")}
        return None

    def merge_question_bank_answers(self, questions: list, nickname: str = "") -> int:
        """对 questions 中无答案的题目,用题库兜底匹配。

        :return: 题库补充成功的题目数
        """
        if not questions:
            return 0
        bank = self._load_question_bank()
        if not bank:
            return 0
        real_bank = [item for item in bank if "示例题干" not in item.get("title", "")]
        if not real_bank:
            return 0
        merged_count = 0
        for q in questions:
            existing_ans = q.get("answer") or q.get("correctAnswer") or q.get("rightAnswer")
            if existing_ans:
                continue
            title = q.get("title") or q.get("questionText") or q.get("name") or ""
            type_id = q.get("typeId") or q.get("type") or q.get("questionType")
            match = self.find_question_bank_answer(title, type_id)
            if not match:
                continue
            ans = match.get("answer", "")
            if not ans:
                continue
            q["answer"] = ans
            type_id_str = str(match.get("typeId") or "")
            if type_id_str in ("1", "2") and q.get("dataJson"):
                try:
                    dj_str = q["dataJson"]
                    dj_list = json.loads(dj_str) if isinstance(dj_str, str) else dj_str
                    if isinstance(dj_list, list) and dj_list:
                        ans_indices = set()
                        for a in str(ans).split(","):
                            a = a.strip()
                            if a.isdigit():
                                ans_indices.add(int(a))
                        for idx, opt in enumerate(dj_list):
                            opt["IsAnswer"] = (idx in ans_indices)
                        q["dataJson"] = json.dumps(dj_list, ensure_ascii=False)
                except Exception:
                    pass
            merged_count += 1
        if merged_count > 0 and nickname:
            log(f"[{nickname}] [题库兜底] 成功补充 {merged_count}/{len(questions)} 题答案", "DEBUG")
        return merged_count


# ==================== 模块级辅助函数 ====================

def _extract_access_token(data: dict) -> Optional[str]:
    """从 passLogin 响应中提取 access_token,兼容 dict 和 str 两种 data 格式。"""
    if not data:
        return None
    d = data.get("data")
    if isinstance(d, dict):
        return d.get("access_token")
    if isinstance(d, str):
        return d
    return None


def extract_file_url(file_url_raw) -> str:
    """从 fileUrl 字段提取视频 URL,兼容 JSON 字符串和直接 URL 两种格式。

    - SPOC/MOOC: fileUrl 是 JSON 字符串 '{"ossOriUrl":"...","url":"..."}'
    - 资源库: fileUrl 可能是直接的 URL 字符串 'https://xxx.mp4'
    """
    if not file_url_raw:
        return ""
    if isinstance(file_url_raw, dict):
        return file_url_raw.get("ossOriUrl", "") or file_url_raw.get("url", "") or file_url_raw.get("fileUrl", "")
    if isinstance(file_url_raw, str):
        s = file_url_raw.strip()
        if s.startswith("{"):
            try:
                fu = json.loads(s)
                if isinstance(fu, dict):
                    return fu.get("ossOriUrl", "") or fu.get("url", "") or fu.get("fileUrl", "")
            except Exception:
                return ""
        if s.startswith("http"):
            return s
    return ""


def _parse_moov_duration(data: bytes) -> Optional[int]:
    """从 bytes 数据中查找 moov box 并提取时长(秒),失败返回 None。"""
    # 策略1:按 box 边界顺序解析
    offset = 0
    while offset < len(data) - 8:
        if offset + 8 > len(data):
            break
        box_size = struct.unpack_from(">I", data, offset)[0]
        box_type = data[offset + 4:offset + 8].decode('latin1', errors='ignore')
        if box_size == 1:
            if offset + 16 > len(data):
                break
            box_size = struct.unpack_from(">Q", data, offset + 8)[0]
            box_header_size = 16
        else:
            box_header_size = 8
        if box_size <= 0 or box_size > len(data) * 10:
            break
        if box_type == 'moov':
            moov_data = data[offset + box_header_size: offset + box_size]
            idx = moov_data.find(b'mvhd')
            if idx == -1:
                return None
            version = moov_data[idx + 4]
            if version == 1:
                timescale = struct.unpack_from(">I", moov_data, idx + 24)[0]
                duration = struct.unpack_from(">Q", moov_data, idx + 28)[0]
            else:
                timescale = struct.unpack_from(">I", moov_data, idx + 16)[0]
                duration = struct.unpack_from(">I", moov_data, idx + 20)[0]
            if timescale > 0:
                return int(duration / timescale)
            return None
        offset += box_size

    # 策略2:直接扫描 moov 签名
    search_pos = 0
    while search_pos < len(data) - 8:
        moov_idx = data.find(b'moov', search_pos)
        if moov_idx == -1:
            break
        if moov_idx < 4:
            search_pos = moov_idx + 4
            continue
        try:
            box_size = struct.unpack_from(">I", data, moov_idx - 4)[0]
            box_header_size = 8
            if box_size == 1 and moov_idx + 12 <= len(data):
                box_size = struct.unpack_from(">Q", data, moov_idx + 4)[0]
                box_header_size = 16
            if box_size <= 0 or box_size > 100 * 1024 * 1024:
                search_pos = moov_idx + 4
                continue
            moov_start = moov_idx - 4
            moov_data = data[moov_start + box_header_size: moov_start + box_size]
            idx = moov_data.find(b'mvhd')
            if idx != -1 and idx + 28 < len(moov_data):
                version = moov_data[idx + 4]
                if version == 1 and idx + 36 <= len(moov_data):
                    timescale = struct.unpack_from(">I", moov_data, idx + 24)[0]
                    duration = struct.unpack_from(">Q", moov_data, idx + 28)[0]
                elif idx + 24 <= len(moov_data):
                    timescale = struct.unpack_from(">I", moov_data, idx + 16)[0]
                    duration = struct.unpack_from(">I", moov_data, idx + 20)[0]
                else:
                    search_pos = moov_idx + 4
                    continue
                if timescale > 0:
                    return int(duration / timescale)
        except Exception:
            pass
        search_pos = moov_idx + 4

    return None


def get_mp4_duration(url: str) -> Optional[int]:
    """通过 Range 请求解析 MP4 文件时长(秒),失败返回 None。

    策略1: 先读文件头 128KB(faststart 视频 moov 在头部)
    策略2: 头部无 moov 时,再读文件末尾 512KB
    策略3: 512KB 未找到时,扩大到 2MB 末尾再试
    """
    try:
        r_head = requests.get(url, headers={**{"Range": "bytes=0-131071"}, **_VIDEO_REFERER}, timeout=10)
        if r_head.status_code not in [200, 206]:
            return None
        result = _parse_moov_duration(r_head.content)
        if result is not None:
            return result

        r_head2 = requests.head(url, headers=_VIDEO_REFERER, timeout=10)
        content_length = int(r_head2.headers.get("Content-Length", 0))
        if content_length <= 131072:
            return None

        # 策略2:读末尾 512KB
        tail_size = 524288
        start_byte = max(0, content_length - tail_size)
        r_tail = requests.get(url, headers={**{"Range": f"bytes={start_byte}-{content_length - 1}"}, **_VIDEO_REFERER}, timeout=15)
        if r_tail.status_code in [200, 206]:
            result = _parse_moov_duration(r_tail.content)
            if result is not None:
                return result

        # 策略3:扩大到 2MB
        if content_length > 524288:
            tail_size_large = 2097152
            start_byte = max(0, content_length - tail_size_large)
            r_tail2 = requests.get(url, headers={**{"Range": f"bytes={start_byte}-{content_length - 1}"}, **_VIDEO_REFERER}, timeout=20)
            if r_tail2.status_code in [200, 206]:
                result = _parse_moov_duration(r_tail2.content)
                if result is not None:
                    return result

        return None
    except Exception as e:
        log(f"get_mp4_duration 异常({url[:80]}): {e}", "ERROR")
        return None


# 模块级 session(auth.py 复用)
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36",
    "log-equipment-app-version": "2.5.6",
    "log-equipment-model": "google Pixel 8",
    "log-equipment-api-version": "35",
    "log-equipment": "1",
    "platform-type": "android",
})
