"""刷课模块:SPOC / MOOC / 资源库三类课程刷课 + 自动答题 + 讨论自动回复。

刷课固定走快速模式(并发/车道提交心跳，效率最优)。

刷课范围:
- all:进度 + 答题 + 讨论
- progress:仅进度
- exam:仅答题
- discussion:仅讨论
"""

import json
import math
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from typing import Optional

from zjy_client import (
    ZjyClient, BASE_URL, IMAGE_TYPES, VIDEO_TYPES, AUDIO_TYPES,
    extract_file_url, get_mp4_duration, get_mp3_duration,
)
from utils import log

# 讨论自动生成内容
DISCUSS_TITLES = [
    "学习心得与感悟", "关于本章节内容的思考", "这节课收获很大",
    "谈谈对这门课程的理解", "非常有意义的学习内容", "对本课知识点的总结",
]
DISCUSS_CONTENTS = [
    "老师讲解得很细致，课件的结构也很清晰，帮助我快速理解了核心知识点，非常感谢老师！",
    "这部分内容非常实用，结合了实际案例，生动形象。学完之后有很大的启发，期待以后的课程。",
    "课件做得太好了，通俗易懂，重点和难点都标记得很明确，自主学习效率极高！",
    "老师的教学方法很棒，由浅入深，每个细节都照顾到了。我已经做好了笔记，下课会认真复习。",
    "本章节讲述的内容对我有很大帮助，解答了我之前很多的疑惑，感觉对这门专业有了更深的认识。",
    "内容非常充实，逻辑性很强。感觉跟着老师的节奏能够轻松掌握核心，谢谢老师的辛苦付出！",
    "这节课的学习让我受益匪浅，不仅掌握了理论知识，还理解了如何在实际中应用，真的很棒。",
]

# ==================== 刷课提速参数（按课程类型门控，SPOC 一律走原路径） ====================
# 资源库心跳步长（秒/跳）：实测 zyk 端 `PUT teacher/studyRecord/` 只在 actualNum **严格增大**时
# 才推进记录（同一位置重复上报一律返回 200 却不写回），所以步长只是"几跳爬完一格"的成本旋钮，
# 不能像旧口径那样靠同位置重复堆条数。10 秒/跳与旧 `total_time//10` 同粒度，便于对照。
ZYK_BEAT_STEP = 10.0
# 资源库每格最多几跳（跳数是成本不是收益）：1 跳即实测能把 speed 推到 100，
# 取 2 跳留一档冗余防单条丢失，同时把每格成本从"最多 200 条"压到常数级。
ZYK_MAX_HOPS = 2
# 资源库扫描/解析并发度：一门 1004 叶的课光扫树就要 1191 次 GET（串行实测 104.8 秒），
# 课件里六成以上是相对短链、还要每格一次即时解析 + 1~4 次媒体头 Range 请求。
RESOURCE_SCAN_WORKERS = 16
RESOURCE_PARSE_WORKERS = 12
# MOOC 跨课件车道数：**单课件内部严格串行、课件之间并行 K 条流**。
# 内部串行是因为心跳按"位置递增"记账，同一课件并发上报有乱序少记的风险；跨课件彼此独立。
# 同一批 12 个课件实测：12 车道 70.5 秒 / 被接受 4673 条，原格内 10 路并发+1.5s 冷却
# 87.1 秒 / 4165 条（复测 89.7 秒），即接受速率 47.8 → 66.2 条/秒。前提见 zjy_client 连接池。
MOOC_LANES = 12
# 车道模式下的派发间隔（秒）：只防本地瞬时打满，不承担平台限速职责。
MOOC_LANE_DISPATCH_GAP = 0.2
# MOOC 真实时长档：心跳**条数**由"要攒的秒数"决定，而要攒的秒数 = 平台树里的 videoTime
# （实测 时长 = 5 秒 × 被接受条数、与墙钟无关；完成度 = 末条 actualNum ÷ 申报 totalNum）。
# 旧口径给每格虚增到 random(1200,2400) 秒，于是非图片格固定 240~480 条心跳——
# 12 门课 2138 格实测要发 48.1 万条，而按真实时长只需 8.9 万条（5.4 倍差距），
# 且其中 828/899 次媒体头解析出来的值 <1200 秒、算完即丢。本档同时消掉这两笔成本。
MOOC_BEAT_STEP = 5.0          # 与官方播放器抓包节奏一致（实测严格 5.0 秒一条）
MOOC_IMG_SECONDS = 20.0       # 图片类：平台不给长度时的申报值
MOOC_DEFAULT_SECONDS = 60.0   # 文档/音频/未知类型同上（音频若能解析出真实长度则用解析值）


def _mooc_is_img(cell_type: str) -> bool:
    """MOOC 图片类判定：fileType 主值是 'img'，不在全局 IMAGE_TYPES 集合内。"""
    return cell_type in IMAGE_TYPES or cell_type == "img"


def _mooc_video_time(cell: dict) -> float:
    """平台树自带的真实长度（秒）；缺失/空串/非数字一律 0.0（=不确定，照旧解析）。"""
    try:
        return float(str((cell or {}).get("videoTime") or "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _mooc_target_seconds(cell: dict, parsed_media_sec=None, cell_type: str = "") -> float:
    """该课件要攒的学习秒数：videoTime > 已解析媒体秒数 > 按类型定值。恒 >0（实测 0 被回 205）。"""
    v = _mooc_video_time(cell)
    if v > 0:
        return v
    if parsed_media_sec and parsed_media_sec > 0:
        return float(parsed_media_sec)
    return MOOC_IMG_SECONDS if _mooc_is_img(cell_type) else MOOC_DEFAULT_SECONDS


# ==================== SPOC 刷课提速（真实时长档 + 跨课件车道；红线：只动 ctype=='SPOC'） ====================
# 三条实测口径决定了这个形态（都在真号真课上量过，不是推断）：
#   ① 时长 = 5 秒 × 被平台接受的心跳条数，与申报位置无关；
#   ② **同一课件内部并发**会把时长入账打到 0.30~0.47（平台侧读-改-写被覆盖），跨课件并发不丢账
#      ⇒ 只能"课件内严格串行 + 课件之间并行 K 条车道"，旧形态的格内 10 路并发是净损失；
#   ③ 压缩包/other/其它 三类附件平台一律回 code=500「资源类型无法学习」，而这类失败会计入
#      "连续 3 次失败即停止尝试"的熔断 ⇒ 不预先剔除，整门课会在第一个附件格之后被放弃。
SPOC_V2 = True                 # False = 逐字退回旧形态（格内并发 + 随机时长），留作线上回退开关
SPOC_LANES = 12                # 跨课件车道数（与 MOOC 同宽，在飞量由车道数天然封顶）
SPOC_LANE_DISPATCH_GAP = 0.2   # 派发间隔（秒）：只防本地瞬时打满，不承担平台限速
SPOC_BEAT_STEP = 5.0           # 与官方播放器一致的 5 秒一条
SPOC_TIME_FACTOR = 2.0         # 申报时长 = 真实长度 × 该系数（1.0 = 与课件等长）
SPOC_DOC_SECONDS = 120.0       # 文档/表格/未知类型的"真实长度"（官方 pptx 实测 111 秒量级）
SPOC_IMG_BEATS = 5             # 图片类计数形心跳条数
SPOC_PARSE_WORKERS = 12        # 视频时长解析并发度（串行 4 → 12 实测快 1.9~2.6 倍）
SPOC_RECHECK_ROUNDS = 3        # 收尾复核轮数：实测"发完立刻读回"入账率只有 0.54（落库滞后）
SPOC_RECHECK_WAIT = 6.0        # 每轮复核前的缓冲秒数，给平台留出库时间
SPOC_REJECT_FILE_TYPES = ("压缩包", "other", "其它")

# SPOC 车道池与 MOOC 池分开建：两类课不互相抢在飞额度
_SPOC_LANE_POOL = ThreadPoolExecutor(max_workers=SPOC_LANES, thread_name_prefix="spoc-lane")


def _spoc_kind_of(cell_type: str) -> str:
    """心跳形态分类：图片走计数形，音视频按解析长度，其余按配置定值。

    这里**刻意只认全局 IMAGE_TYPES**（不把 'img' 特例算进来）：'img' 在资源库里是计数型，
    但在 SPOC 走申报制，两类混用会让同一门课两次刷出来的每格时长对不上。
    """
    if cell_type in IMAGE_TYPES:
        return "img"
    if cell_type in AUDIO_TYPES:
        return "audio"
    if cell_type in VIDEO_TYPES:
        return "video"
    return "doc"


def _spoc_target_seconds(kind: str, parsed_sec=None) -> float:
    """一格要申报的总长 T：条数 = ceil(T/5)、末条位置 = T ⇒ 时长与完成度同时到位。

    平台侧不存在课件长度（树接口无长度字段、getStudyCellInfo 只是上次申报值的回声），
    所以视频只能用自己解析出的媒体头时长；解析不出与文档类一律走配置定值，
    **不再 random(600,1800)** —— 那个随机正是"39 分钟视频记成 1367 秒"的虚增源头。
    """
    if kind == "img":
        return 1.0
    base = float(parsed_sec) if parsed_sec and float(parsed_sec) > 0 else SPOC_DOC_SECONDS
    return round(base * SPOC_TIME_FACTOR, 2)


def _spoc_filter_reject(leaf_cells: list, nickname: str, where: str) -> list:
    """剔掉"平台一律回 500"的附件类课件。跳过、不计失败、不触发熔断。"""
    if not SPOC_V2:
        return leaf_cells
    keep = [c for c in leaf_cells if (c.get("fileType") or "") not in SPOC_REJECT_FILE_TYPES]
    n = len(leaf_cells) - len(keep)
    if n:
        log(f"[{nickname}] 跳过 {n} 个平台不支持心跳的附件课件（{'、'.join(SPOC_REJECT_FILE_TYPES)}，"
            f"必回 500；{where}）", "INFO")
    return keep


def _spoc_beat_payloads(client: ZjyClient, aes_key: str, class_id: str, course_info_id: str,
                        cell_id: str, stu_id, target: float, step: float,
                        is_image: bool, img_beats: int, start: float = 0.0):
    """一格完整心跳流（与旧形态同字段、同 AES、同端点，只改条数与位置形状）。

    start>0 用于收尾复核的差额补发：从平台已记秒数续推，不重发已到账的那一段。
    """
    recs = []
    if is_image:
        for _i in range(max(1, int(img_beats or 1))):
            recs.append({"actualNum": 1, "classId": class_id, "courseInfoId": course_info_id,
                         "id": str(uuid.uuid4()).upper(), "lastNum": 1, "params": {},
                         "resourceTotalNum": 1, "sourceId": cell_id, "speed": 100.0,
                         "studentId": stu_id, "studyTime": 1, "totalNum": 1})
        return recs
    total_num = float(target)
    _from = max(0.0, float(start or 0))
    step = max(1.0, float(step))
    n = max(1, int(math.ceil(max(0.0, total_num - _from) / step)))
    for i in range(n):
        pos = total_num if i == n - 1 else min(total_num, _from + (i + 1) * step)
        recs.append({"actualNum": pos, "classId": class_id, "courseInfoId": course_info_id,
                     "id": str(uuid.uuid4()).upper(), "lastNum": pos, "params": {},
                     "resourceTotalNum": total_num, "sourceId": cell_id, "speed": 100.0,
                     "studentId": stu_id, "studyTime": total_num, "totalNum": total_num})
    return recs


def _spoc_encrypt(client: ZjyClient, aes_key: str, rec: dict):
    """单条心跳加密成 POST body（与旧内联写法逐字同形：sort_keys + 紧凑分隔符 + %25/%2B 转义）"""
    js = json.dumps(rec, separators=(',', ':'), sort_keys=True)
    enc = client.aes_encrypt(js, aes_key)
    if not enc:
        return None
    return {"param": enc.replace('%', '%25').replace('+', '%2B')}


def _spoc_lane_stream(client: ZjyClient, bodies: list) -> int:
    """一条车道 = 一个课件的完整心跳流，**严格串行**（同课件并发会丢时长账）。"""
    ok = 0
    for b in bodies:
        try:
            r = client.session.post(f"{BASE_URL}/spoc/studyRecord", json=b, timeout=15)
            j = r.json() if r.status_code == 200 else None
        except Exception:
            j = None
        if isinstance(j, dict) and j.get("code") == 200:
            ok += 1
    return ok


def _spoc_cell_study(client: ZjyClient, class_id: str, cell_id: str):
    """单格权威读回（getStudyCellInfo）。**不得**用 studyRecord/list 的 rows[0]——
    实测同一格返回 55+ 行（每批心跳落一行），rows[0] 是任意一行。"""
    d = client.api_get("spoc/courseDesign/getStudyCellInfo", {"id": cell_id, "classId": class_id})
    ssr = ((d or {}).get("data") or {}).get("studentStudyRecord") or {}
    try:
        return float(ssr.get("studyTime") or 0), float(ssr.get("speed") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0


# MOOC 车道池：模块级复用，线程只在提交时创建
_LANE_POOL = ThreadPoolExecutor(max_workers=MOOC_LANES, thread_name_prefix="mooc-lane")


def _scan_blocked_reason(client, ctype, net_base=0) -> str:
    """课件清单为空时判断"是读不到还是没有课件"，返回原因文本（空串=没有可报告的失败）。

    MOOC 的清单全在 AI 域、资源库的清单全在 zyk 域，两域都是**换票失败就什么都读不到**；
    而平台的鉴权失效响应形如 HTTP 200 + `{"code":401}`/`data.allowLogin=false`，
    不专门看就会当成"这门课没有课件"。
    """
    if ctype == "MOOC" and getattr(client, "ai_auth_msg", ""):
        return f"AI 域换票失败：{client.ai_auth_msg}"
    if ctype == "RESOURCE" and getattr(client, "zyk_auth_msg", ""):
        return f"资源库域换票失败：{client.zyk_auth_msg}"
    _n = (getattr(client, "net_err", 0) or 0) - (net_base or 0)
    if isinstance(_n, int) and _n > 0:
        return f"读请求异常 {_n} 次"
    return ""


# 心跳阶段跳过的"非课件"类型：资源库只跳作业（测验/考试/讨论都计进 studySpeed 分母，
# 实测走计数形一跳即可标满），其余类型照旧跳全部六类（红线：口径逐字不变）
_SKIP_FT_COMMON = ["作业", "测验", "考试", "讨论", "exam", "homework"]
_SKIP_FT_ZYK = ["作业", "exam", "homework"]


def _skip_fts(ctype: str) -> list:
    return _SKIP_FT_ZYK if ctype == "RESOURCE" else _SKIP_FT_COMMON


# 资源库里"计进度但没有可看的媒体"的格：fileUrl 为空、按视频时长刷不动，
# 实测走图片同款计数形（totalNum=actualNum=1）一跳即 speed=100
ZYK_TASK_TYPES = ("测验", "考试", "讨论")


def _zyk_mark_task(leaf_cells: list) -> int:
    """给资源库的 测验/考试/讨论 格打纳管标记（计数形单跳标满），返回标记格数。"""
    n = 0
    for c in leaf_cells or []:
        if (c.get("fileType") or "") in ZYK_TASK_TYPES:
            c["_zyk_task"] = True
            n += 1
    return n


def _zyk_mark_swf(leaf_cells: list) -> int:
    """给资源库的 `.swf` 课件打纳管标记（心跳改走计数形单次上报），返回标记格数。"""
    n = 0
    for c in leaf_cells or []:
        if ".swf" in (c.get("fileUrl") or ""):
            c["_zyk_swf"] = True
            n += 1
    return n


def _zyk_cell_speed(cell: dict) -> float:
    """平台口径的课件进度（0-100）；读不到按 0（=当作未完成，只会多刷不会漏刷）。"""
    v = (cell or {}).get("_speed")
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _zyk_hop_step(total: int, max_hops: int = ZYK_MAX_HOPS, min_step: float = ZYK_BEAT_STEP) -> float:
    """把"该格申报的总长"折算成步长：短课件维持 ≥min_step 的小步，长课件用 total/max_hops
    收敛为常数级跳数。脏输入回落默认步长（保守多发，不漏刷）。
    """
    try:
        mh = max(1, int(max_hops))
    except (TypeError, ValueError):
        mh = ZYK_MAX_HOPS
    try:
        s0 = float(min_step)
    except (TypeError, ValueError):
        s0 = ZYK_BEAT_STEP
    if s0 <= 0:
        s0 = ZYK_BEAT_STEP
    try:
        t = float(total)
    except (TypeError, ValueError):
        return s0
    if t <= s0 * mh:
        return s0
    return max(s0, t / mh)


def _zyk_incr_pairs(actual, total: int, step: float = ZYK_BEAT_STEP) -> list:
    """资源库心跳的 (actualNum, totalNum) 递增序列。

    返回 [] 表示"没有新信息可上报"（申报总长不可用）——调用方据此回落到单发兜底。
    脏输入策略：位置读不到就当 0 从头推进，**绝不因脏值跳过课件**。
    """
    try:
        t = float(total)
    except (TypeError, ValueError):
        return []
    if t <= 0:
        return []
    try:
        a = 0.0 if actual in (None, "", {}) else float(actual)
    except (TypeError, ValueError):
        a = 0.0
    if a < 0:
        a = 0.0
    if a >= t:
        return []
    out, p = [], a
    while p < t:
        p = min(p + step, t)
        if p >= t:
            p = t                       # 消除浮点累加误差：末条必须精确等于申报值
        out.append((round(p, 2), t))
    return out


def run_speed_course(client: ZjyClient, course: dict, speed_type: str = "all") -> None:
    """一键自动刷课。

    :param client: ZjyClient 实例
    :param course: 课程 dict,需含 classId/courseInfoId/courseId/_courseType/courseName
    :param speed_type: "all" | "progress" | "exam" | "discussion"

    MOOC 重叠流水线(2026-09-14 生产移植):任务开头先把全部未交卷"点开"起表
    (205 时间闸门只认 wall-clock,起表后刷课件/讨论的耗时天然攒够答题时间)→
    刷进度 → 成熟卷并发即交、未熟卷留补交 → 刷讨论 → 补交剩余(并行补等)。
    SPOC/RESOURCE 不进入流水线(红线:行为与原顺序模式一致)。
    """
    class_id = course.get("classId", "")
    course_info_id = course.get("courseInfoId", "")
    course_id = course.get("courseId", "")
    ctype = course.get("_courseType", "") or course.get("ctype", "") or "SPOC"
    course_name = course.get("courseName", "未知课程")
    nickname = (client.user_info or {}).get("nickName", "未知")

    log(f"[{nickname}] 🚀 启动刷课: {course_name} (类型:{ctype}, 模式:{speed_type})", "INFO")

    _mooc_exam_pending = None   # None=未探测;list=尚未成功提交的未交卷
    _mooc_stats = {"success": 0, "fail": 0, "skip": 0, "window_skip": 0}

    try:
        # MOOC 重叠流水线·提前起表(仅 MOOC;起表失败自动降级=答题环节重新枚举)
        if ctype == "MOOC" and speed_type in ("all", "exam"):
            try:
                from answer import (get_course_exams_list, _is_low_score,
                                    mooc_stagger_open_exams)
                _ex0 = get_course_exams_list(client, class_id, course_info_id, course_id, ctype)
                _mooc_exam_pending = [e for e in _ex0 if _is_low_score(e)]
                if _mooc_exam_pending:
                    mooc_stagger_open_exams(client, _mooc_exam_pending, nickname)
            except Exception as e:
                _mooc_exam_pending = None
                log(f"[{nickname}] ⚠️ 提前起表失败(答题环节将重新枚举): {e}", "WARNING")

        # Part 1: 刷进度
        if speed_type in ["all", "progress"]:
            _brush_progress(client, nickname, class_id, course_info_id, course_id, ctype)

        # Part 1.5: 自动答题(MOOC 走成熟度分流+并发提交;SPOC/RESOURCE 原顺序模式)
        if speed_type in ["all", "exam"]:
            if ctype == "MOOC":
                _mooc_exam_pending = _brush_exam_mooc_stage1(
                    client, nickname, class_id, course_info_id,
                    course_id, _mooc_exam_pending, _mooc_stats)
            else:
                _brush_exam(client, nickname, class_id, course_info_id, course_id, ctype)

        # Part 2: 刷讨论
        if speed_type in ["all", "discussion"]:
            _brush_discussion(client, nickname, class_id, course_info_id, course_id, ctype)

        # Part 2.5: MOOC 未成熟卷补交(刷讨论期间已继续垫墙钟,并发补等剩余差额)
        if (ctype == "MOOC" and speed_type in ("all", "exam") and _mooc_exam_pending):
            log(f"[{nickname}] 🕒 补交未到答题时间的 {len(_mooc_exam_pending)} 张试卷(不足部分并行补等)...", "INFO")
            _mooc_submit_batch(client, nickname, class_id, course_info_id, course_id,
                               _mooc_exam_pending, _mooc_stats)

        # MOOC 答题环节汇总
        if ctype == "MOOC" and speed_type in ("all", "exam") and _mooc_exam_pending is not None:
            _tail = f",跳过(作答次数上限) {_mooc_stats['skip']}" if _mooc_stats["skip"] else ""
            if _mooc_stats["window_skip"]:
                _tail += f",跳过(作答窗口未开放) {_mooc_stats['window_skip']}"
            log(f"[{nickname}] 🎉 自动答题结束:成功 {_mooc_stats['success']} 个,"
                f"失败 {_mooc_stats['fail']} 个{_tail}", "INFO")

    except Exception as e:
        log(f"[{nickname}] 刷课异常: {e}", "ERROR")

    log(f"[{nickname}] 🏁 刷课结束: {course_name}", "INFO")


def _brush_exam_mooc_stage1(client: ZjyClient, nickname: str, class_id: str,
                            course_info_id: str, course_id: str,
                            pending, stats: dict) -> list:
    """MOOC 重叠流水线·第一遍提交:优先用任务头起表清单(起表失败则就地重新枚举+起表);
    逐卷探测成熟度,已成熟批并发即交(gate_wait 秒过),未熟卷返回给调用方待 Part 2.5 补交。"""
    from answer import (get_course_exams_list, _is_low_score, mooc_stagger_open_exams,
                        mooc_exam_gate_remaining)
    if pending is None:
        exams = get_course_exams_list(client, class_id, course_info_id, course_id, "MOOC")
        pending = [e for e in exams if _is_low_score(e)]
        if pending:
            mooc_stagger_open_exams(client, pending, nickname)
    _mature, _immature = [], []
    for exam in list(pending):
        eid = exam.get("id") or exam.get("examId")
        rem, _gmin = mooc_exam_gate_remaining(client, eid) if eid else (0, 0)
        (_immature if rem > 0 else _mature).append(exam)
    if not _mature and not _immature:
        log(f"[{nickname}] 没有发现未提交或低分的作业或考试", "INFO")
        return []
    log(f"[{nickname}] 📋 答题:{len(_mature)} 张已成熟即交,{len(_immature)} 张未到时间闸门留待后续补交", "INFO")
    if _mature:
        _mooc_submit_batch(client, nickname, class_id, course_info_id, course_id, _mature, stats)
    return _immature


def _mooc_submit_batch(client: ZjyClient, nickname: str, class_id: str,
                       course_info_id: str, course_id: str, exam_list: list,
                       stats: dict) -> int:
    """并发提交一批 MOOC 卷(8 workers,gate_wait=True → 已成熟秒过、未熟线程内并行补等,
    总墙钟≈最慢单卷)。205 双语义分流:「作答次数上限」/「非作答时间」记跳过不记失败。
    返回实际提交数。仅 MOOC 路径调用。"""
    from answer import (do_auto_answer_single_exam, mooc_exam_is_exhausted,
                        mooc_exam_window_closed)
    if not exam_list:
        return 0

    def _one(exam):
        eid = exam.get("id") or exam.get("examId")
        title = exam.get("title", "未命名任务")
        etype = exam.get("type", "")
        cat = "2" if etype == "考试" else ("3" if etype == "测验" else "1")
        return do_auto_answer_single_exam(
            client, nickname, eid, class_id, course_info_id, course_id,
            "MOOC", title, cat, gate_wait=True)

    with ThreadPoolExecutor(max_workers=8) as _ex_pool:
        _futs = {_ex_pool.submit(_one, e): e for e in exam_list}
        for _f in as_completed(_futs):
            try:
                ok, msg = _f.result()
            except Exception as _fe:
                ok, msg = False, f"EXC {str(_fe)[:80]}"
            if ok:
                stats["success"] += 1
            elif mooc_exam_is_exhausted(msg):
                stats["skip"] += 1          # 次数上限:不重试不记失败
            elif mooc_exam_window_closed(msg):
                stats["window_skip"] += 1   # 窗口未开放:记跳过非失败
            else:
                stats["fail"] += 1
    return len(exam_list)


# ==================== Part 1: 刷进度 ====================

def _brush_progress(client: ZjyClient, nickname: str, class_id: str,
                     course_info_id: str, course_id: str, ctype: str) -> None:
    """刷课件进度:SPOC/MOOC/资源库三分支。"""
    log(f"[{nickname}] 🚀 开始秒刷课件进度...", "INFO")

    # 资源库课件树按层并发扫描；其余类型不传宽度 = 原口径逐字不变
    _scan_w = RESOURCE_SCAN_WORKERS if ctype == "RESOURCE" else None
    _net0 = getattr(client, "net_err", 0) or 0
    # 先扫描未完成的课件
    leaf_cells = client.get_course_cells(course_info_id, class_id, course_id, include_completed=False,
                                         ctype=ctype, leaf_workers=_scan_w)
    leaf_cells = [c for c in leaf_cells if (c.get("fileType") or "") not in _skip_fts(ctype)]

    # 清单为空有两种完全不同的原因："真的都刷完了" 与 "根本没读到"（换票失败/网络异常）。
    # 旧代码一律当前者，于是登录态过期时用户看到的是"所有课件已完成"，以为功能坏了。
    _blocked = _scan_blocked_reason(client, ctype, _net0)
    if _blocked:
        log(f"[{nickname}] ⛔ 课件清单没读到（{_blocked}）——这不是「全部已完成」，本轮不刷进度；"
            f"请到主菜单重新登录后再试", "ERROR")
        return
    # 资源库心跳统计（免发/SWF 通道），只 RESOURCE 分支读写
    zyk_swf_stat = {"ok": 0, "noop": 0, "skipped": 0, "disabled": False}
    # 测验/考试/讨论 单独一账：一种被平台拒绝不得牵连另一种（也不进连续失败熔断）
    zyk_task_stat = {"ok": 0, "noop": 0, "skipped": 0, "disabled": False}
    zyk_noop_stat = {"skipped": 0, "beats_saved": 0, "fallback": 0}

    def _swf_gate(_cells):
        """计数形纳管分流：资源库给 .swf 与 测验/考试/讨论 打标记，其余类型照旧整格跳过。"""
        if ctype == "RESOURCE":
            return _cells, _zyk_mark_swf(_cells) + _zyk_mark_task(_cells)
        _sk = [c for c in _cells if ".swf" in (c.get("fileUrl") or "")]
        if _sk:
            _cells = [c for c in _cells if ".swf" not in (c.get("fileUrl") or "")]
        return _cells, len(_sk)

    # SWF 课件预过滤:.swf Flash 动画在主域直接拒绝心跳,历史连败 3 次会触发熔断把整门课拖死,
    # 故 SPOC/MOOC 继续跳过不计失败；资源库（zyk 域）实测接受计数形上报，改为纳管。
    leaf_cells, _swf_n = _swf_gate(leaf_cells)
    if _swf_n:
        log(f"[{nickname}] "
            + (f"纳管 {_swf_n} 个 SWF 动画/测验/考试/讨论课件（计数形单次心跳）" if ctype == "RESOURCE"
               else f"跳过 {_swf_n} 个 SWF 动画课件(平台不支持心跳刷时长)"), "INFO")
    # SPOC 必拒附件类预过滤（不计失败、不进熔断；MOOC/RESOURCE 逐字不变）
    if ctype == "SPOC":
        leaf_cells = _spoc_filter_reject(leaf_cells, nickname, "首轮扫描")

    # 全部已完成则重刷(加时长)
    if not leaf_cells:
        log(f"[{nickname}] 所有课件已完成,将全部重刷以增加时长...", "INFO")
        leaf_cells = client.get_course_cells(course_info_id, class_id, course_id, include_completed=True,
                                             ctype=ctype, leaf_workers=_scan_w)
        leaf_cells = [c for c in leaf_cells if (c.get("fileType") or "") not in _skip_fts(ctype)]
        # SWF 同款分流（重刷分支，口径同上）
        leaf_cells, _swf_n2 = _swf_gate(leaf_cells)
        if _swf_n2:
            log(f"[{nickname}] "
                + (f"纳管 {_swf_n2} 个 SWF 动画/测验/考试/讨论课件（重刷分支）" if ctype == "RESOURCE"
                   else f"跳过 {_swf_n2} 个 SWF 动画课件(重刷分支)"), "INFO")
        if ctype == "SPOC":
            leaf_cells = _spoc_filter_reject(leaf_cells, nickname, "重刷分支")
        # 跳过已完成的图片课件(重刷会导致进度回退)
        # SPOC 图片课件 fileType 主值是 'img'（不在 IMAGE_TYPES 集合），同为计数型 totalNum=1
        # 课件，重刷同样回退——仅在本保护点补 'img'，不动全局集合。
        _skipped_img = 0
        _filtered = []
        for c in leaf_cells:
            _ct = (c.get("fileType") or "").lower()
            if (_ct in IMAGE_TYPES or _ct == "img") and c.get("_speed", 0) >= 100:
                _skipped_img += 1
                continue
            _filtered.append(c)
        leaf_cells = _filtered
        if _skipped_img > 0:
            log(f"[{nickname}] 跳过 {_skipped_img} 个已完成的图片课件(避免重刷导致进度回退)", "INFO")

    if not leaf_cells:
        _b2 = _scan_blocked_reason(client, ctype, _net0)
        if _b2:
            log(f"[{nickname}] ⛔ 重扫仍没读到课件清单（{_b2}），本轮不刷进度", "ERROR")
        else:
            log(f"[{nickname}] 没有扫描到可刷的进度课件", "INFO")
        return

    log(f"[{nickname}] 找到 {len(leaf_cells)} 个课件，开始提交心跳...", "INFO")
    aes_key = client.generate_aes_key() if ctype not in ("MOOC", "RESOURCE") else None

    # 并行解析 MP4 时长（真实时长档会跳过有 videoTime 的格）
    # 资源库相对短链即时解析:同轮按 cellId 去重缓存,失败同样缓存以免重试打点
    zyk_url_cache = {}
    zyk_resolve_stat = {"ok": 0, "fail": 0}
    mp4_duration_cache = _parse_mp4_durations_parallel(
        client, nickname, leaf_cells, ctype, zyk_url_cache, zyk_resolve_stat,
        workers=(RESOURCE_PARSE_WORKERS if ctype == "RESOURCE"
                 # SPOC 真实时长档：申报值就是解析值 × 系数，解析是**必需**成本，故并发度单独放宽
                 else SPOC_PARSE_WORKERS if (ctype == "SPOC" and SPOC_V2) else 4),
        # 平台已记满的资源库格下面直接免发，解析结果用不上 —— 不为之花一次 RTT
        skip_speed_full=ctype == "RESOURCE")

    # MOOC 跨课件车道:每格整条心跳流派给一条车道串行发送，主循环立刻派发下一格。
    # 只有 MOOC 进入本块；SPOC/RESOURCE 的 lane_ctx 恒 None（红线）
    # MOOC/SPOC 跨课件车道:每格整条心跳流派给一条车道串行发送，主循环立刻派发下一格。
    # 两型共用同一套记账结构，但**各自一条池**（互不抢在飞额度）；RESOURCE 恒 None（红线）
    lane_ctx = None
    _lane_w = (MOOC_LANES if ctype == "MOOC" else SPOC_LANES if ctype == "SPOC" else 1)
    if ctype in ("MOOC", "SPOC") and _lane_w > 1:
        lane_ctx = {"running": set(), "futs": [], "api": None, "done": 0, "beats": 0, "zero": 0,
                    "sent": 0, "t0": time.time(), "t_note": 0.0,
                    "pool": _LANE_POOL if ctype == "MOOC" else _SPOC_LANE_POOL,
                    "gap": MOOC_LANE_DISPATCH_GAP if ctype == "MOOC" else SPOC_LANE_DISPATCH_GAP,
                    "items": []}

        def _lane_reap():
            """收割已完成车道，按节流打一条"还在跑"的进度行。

            逐格 ✅ 留给派发时打（与刷 SPOC 时的输出顺序一致：序号必然递增），
            这里只负责"中段不静默"——车道并发下完成顺序天然乱序，逐格打就成了乱序。
            """
            left = []
            for nm, f in lane_ctx["futs"]:
                if f.done():
                    try:
                        n = f.result() or 0
                    except Exception:
                        n = 0
                    lane_ctx["done"] += 1
                    lane_ctx["beats"] += n
                    if n == 0:
                        lane_ctx["zero"] += 1
                        log(f"[{nickname}] 刷进度 ❌ {nm} 车道内全部心跳未被接受", "WARNING")
                else:
                    left.append((nm, f))
            lane_ctx["futs"][:] = left
            lane_ctx["running"] = {f for f in lane_ctx["running"] if not f.done()}
            # 存活证据：每 ~25 秒一条（一条 390 格的课能稳稳定点报，不会再看着像卡死）
            _el = time.time() - lane_ctx["t0"]
            if lane_ctx["futs"] and _el - lane_ctx["t_note"] >= 25:
                lane_ctx["t_note"] = _el
                _rate = lane_ctx["beats"] / max(1.0, _el)
                log(f"[{nickname}] [车道] 进行中：已派发 {lane_ctx['sent']}/{len(leaf_cells)} 格、"
                    f"已完成 {lane_ctx['done']} 格、被接受 {lane_ctx['beats']} 条心跳、"
                    f"在飞 {len(lane_ctx['running'])} 车道、均速 {_rate:.0f} 条/秒"
                    f"，已用 {_el:.0f} 秒", "INFO")

        log(f"[{nickname}] 跨课件并行已启用：车道={_lane_w}（每课件内部严格串行），"
            f"每格派发即报一行，另每 25 秒报一次总进度", "INFO")

    success_count = 0
    fail_count = 0
    consecutive_fails = 0

    for idx, cell in enumerate(leaf_cells):
        # 连续失败保护:MOOC 阈值 10——实测平台限流为瞬时抖动，同节点 2 分钟后单发即成功，
        # 原阈值 3 使一次抖动直接中断整场刷课。SPOC/RESOURCE 维持 3。
        _fail_limit = 10 if ctype == "MOOC" else 3
        if consecutive_fails >= _fail_limit:
            log(f"[{nickname}] ⚠️ 连续{_fail_limit}次刷课失败,停止尝试", "WARNING")
            break

        cell_id = cell.get("id")
        cell_name = cell.get("name", "?")
        cell_type = (cell.get("fileType") or "").lower()
        file_url_raw = cell.get("fileUrl")

        # 计算目标时长。资源库满格下面直接免发，就不必为它花一次媒体头解析
        # （并行阶段已跳过这些格，若在此再算会退化成串行逐格解析）
        if ctype == "RESOURCE" and _zyk_cell_speed(cell) >= 100:
            total_time = 0
        else:
            total_time = _calculate_total_time(client, nickname, cell, idx, class_id, course_info_id,
                                                course_id, ctype, cell_type, file_url_raw,
                                                mp4_duration_cache, zyk_url_cache, zyk_resolve_stat)

        # 提交心跳
        status = _submit_heartbeat(client, nickname, cell, idx, len(leaf_cells), class_id, course_info_id,
                                   course_id, ctype, cell_type, total_time, aes_key,
                                   lane_ctx, zyk_swf_stat, zyk_noop_stat, zyk_task_stat)

        if status == "skip":
            # 平台明确不接受的通道（本课已关闭 SWF 上报）：静默跳过，不进熔断计数
            continue

        if status == "dispatched":
            # 车道模式：派发即按序号报一行（完成顺序是乱的，逐格等打完就成乱序），
            # 真实"被平台接受多少条"在车道回收时结算，一格也没被接受的会改判成失败并点名。
            success_count += 1
            consecutive_fails = 0
            lane_ctx["sent"] += 1
            log(f"[{nickname}] 刷进度 ✅ [{idx+1}/{len(leaf_cells)}] {cell_name}", "INFO")
            _lane_reap()
            time.sleep(lane_ctx["gap"])
            continue

        if status in ("ok", "noop"):
            success_count += 1
            consecutive_fails = 0
            if status == "noop":
                log(f"[{nickname}] 刷进度 ⏭ [{idx+1}/{len(leaf_cells)}] {cell_name}（平台已记满，免发）", "INFO")
            else:
                log(f"[{nickname}] 刷进度 ✅ [{idx+1}/{len(leaf_cells)}] {cell_name}", "INFO")
        else:
            fail_count += 1
            consecutive_fails += 1
            # 前3次失败打印 cell 详情,帮助定位特殊课件类型
            if consecutive_fails <= 3:
                log(f"[{nickname}] 刷进度 ❌ [{idx+1}/{len(leaf_cells)}] {cell_name} 心跳失败 "
                    f"(id={cell.get('id','?')}, fileType={cell.get('fileType','?')}, "
                    f"totalTime={total_time}, _speed={cell.get('_speed','?')})", "WARNING")

        # 节点间冷却：MOOC 逐课件串行时原 0.02s 过密（80 节点连续上万条心跳会撞平台限流），
        # 实测 1.5s 可全程不撞；改车道并发后在飞量由车道数封顶，派发间隔压到 0.2s。
        # RESOURCE 间隔维持原值不变（红线）。
        if lane_ctx is not None:
            time.sleep(lane_ctx["gap"])
        elif ctype == "MOOC":
            time.sleep(1.5)
        else:
            time.sleep(0.02)

    # ---- MOOC 车道收尾：等待期间继续逐格报进度，最后按"平台真正接受的条数"结算 ----
    if lane_ctx is not None:
        _waited = 0.0
        while lane_ctx["futs"] and _waited < 3600:
            _dn, _ = wait(list(lane_ctx["running"]), timeout=5.0, return_when=FIRST_COMPLETED)
            lane_ctx["running"] -= _dn
            _waited += 5.0
            _lane_reap()
        _lane_reap()
        _zero = lane_ctx["zero"]
        success_count = max(0, success_count - _zero)
        fail_count += _zero
        log(f"[{nickname}] 车道回收:{lane_ctx['done']}/{lane_ctx['sent']} 个课件已发完,"
            f"被平台接受心跳 {lane_ctx['beats']} 条"
            + (f",零接受 {_zero} 个(已改判失败)" if _zero else ""), "INFO")

        # ---- 平台复核补满（仅 MOOC 车道）：实测"心跳全 200 却停在 speed=99 不入 completed"，
        # 唯一权威口径是平台的已完成集合；位置语义=平台取历史最大值，整段重灌不会掉档。
        _api = lane_ctx["api"]
        if _api:
            for _rd in (1, 2):
                _comp = client.mooc_completed_cells(course_info_id, course_id)
                if _comp is None:
                    log(f"[{nickname}] ⚠️ 复核读不到平台已完成集合，跳过补满（以平台页面为准）", "WARNING")
                    break
                _miss = [cl for cl in leaf_cells if str(cl.get("id")) not in _comp]
                if not _miss:
                    break
                log(f"[{nickname}] 复核第 {_rd} 轮：平台判定未学完 {len(_miss)} 个，补满中...", "WARNING")
                _rf = []
                for _cl in _miss:
                    _ct = (_cl.get("fileType") or "").lower()
                    _img = _mooc_is_img(_ct)
                    _tg = _mooc_target_seconds(_cl, None, _ct)
                    _rf.append(_LANE_POOL.submit(
                        _mooc_beat_stream, client,
                        _mooc_payloads(_mooc_base_record(_cl, course_id, course_info_id,
                                                         client.stu_id, class_id, _tg, _img),
                                       _tg, _img), *_api))
                wait(_rf)
            _comp = client.mooc_completed_cells(course_info_id, course_id)
            if _comp is not None:
                _still = [cl for cl in leaf_cells if str(cl.get("id")) not in _comp]
                _mins = client.mooc_total_study_minutes(course_info_id, course_id)
                log(f"[{nickname}] 平台复核：本次课件已学完 {len(leaf_cells) - len(_still)}"
                    f"/{len(leaf_cells)} 个"
                    + (f" · 全课学习时长 {_mins:.1f} 分" if _mins is not None else ""), "INFO")
                for _cl in _still[:20]:
                    log(f"[{nickname}] ⚠️ 复核后仍未学完：{str(_cl.get('name'))[:20]}", "WARNING")
                if len(_still) > 20:
                    log(f"[{nickname}] ⚠️ 另有 {len(_still) - 20} 个未学完（明细略）", "WARNING")

        # ---- SPOC 平台复核补满（真实时长档配套，仅 SPOC 车道）----
        # 为什么必须有：实测"发完立刻读回"时长入账率只有 0.54（平台落库滞后），而旧形态
        # 收尾从不读回 ⇒ "心跳全 200 但时长没满"在执行侧完全看不见，用户只能自己发现。
        if ctype == "SPOC" and lane_ctx["items"]:
            _items = lane_ctx["items"]          # [(格号, cellId, 申报总长, 课件名), ...]

            def _rd_one(_it):
                try:
                    return _spoc_cell_study(client, class_id, _it[1])
                except Exception:
                    return (0.0, 0.0)

            _rd_n = 0
            _last_sig = None
            while _rd_n < SPOC_RECHECK_ROUNDS:
                if SPOC_RECHECK_WAIT > 0:
                    time.sleep(SPOC_RECHECK_WAIT)      # 给平台留出库时间，否则读到的是旧值
                with ThreadPoolExecutor(max_workers=8) as _ex:
                    _rdv = list(_ex.map(_rd_one, _items))
                _short = [(_it, _st, _sp) for _it, (_st, _sp) in zip(_items, _rdv)
                          if _it[2] > 1 and (_st < _it[2] - 0.5 or _sp < 100)]
                if not _short:
                    break
                # 护栏：读回值与上一轮**完全一致** ⇒ 平台已不再推进，停手别把同样条数白发几轮
                _sig = tuple(round(_st + _sp, 1) for _st, _sp in _rdv)
                if _sig == _last_sig:
                    log(f"[{nickname}] SPOC 复核：第 {_rd_n + 1} 轮读回与上一轮完全一致"
                        f"（平台未推进），停止补发", "WARNING")
                    break
                _last_sig = _sig
                _rd_n += 1
                _gap = sum(max(0.0, _it[2] - _st) for _it, _st, _sp in _short)
                log(f"[{nickname}] SPOC 复核第 {_rd_n} 轮：{len(_short)} 个课件时长/完成度未满"
                    f"（缺口约 {_gap:.0f} 秒），从已记秒数续推补发...", "WARNING")
                _tf = []
                for _it, _st, _sp in _short:
                    _recs = _spoc_beat_payloads(client, aes_key, class_id, course_info_id,
                                                _it[1], client.stu_id, _it[2], SPOC_BEAT_STEP,
                                                False, 0, start=_st)
                    _b = [x for x in (_spoc_encrypt(client, aes_key, r) for r in _recs) if x]
                    if _b:
                        _tf.append(_SPOC_LANE_POOL.submit(_spoc_lane_stream, client, _b))
                if _tf:
                    wait(_tf)
            with ThreadPoolExecutor(max_workers=8) as _ex:
                _fin = list(_ex.map(_rd_one, _items))
            _t_ok = sum(1 for _it, (_st, _sp) in zip(_items, _fin)
                        if _it[2] <= 1 or _st >= _it[2] - 0.5)
            _s_ok = sum(1 for _it, (_st, _sp) in zip(_items, _fin) if _sp >= 100)
            _acc = sum(_st for _it, (_st, _sp) in zip(_items, _fin) if _it[2] > 1)
            log(f"[{nickname}] SPOC 平台复核：时长满 {_t_ok}/{len(_items)}、"
                f"完成度满 {_s_ok}/{len(_items)}，本轮到账合计 {_acc:.0f} 秒"
                f"（={_acc/60:.1f} 分，含补发 {_rd_n} 轮）", "INFO")
            for _it, (_st, _sp) in list(zip(_items, _fin))[:5]:
                if _it[2] > 1 and (_st < _it[2] - 0.5 or _sp < 100):
                    log(f"[{nickname}] ⚠️ 复核后仍未满：{str(_it[3])[:20]} "
                        f"时长 {_st:.0f}/{_it[2]:.0f} 秒 speed={_sp:.0f}", "WARNING")

    log(f"[{nickname}] 🎉 进度秒刷结束:成功 {success_count} 个,失败 {fail_count} 个", "INFO")

    # 资源库专属汇总：解析统计只在真的发生过解析时才输出，避免给 SPOC/MOOC 日志加噪音
    if zyk_resolve_stat["ok"] + zyk_resolve_stat["fail"]:
        log(f"[{nickname}] RESOURCE 相对短链即时解析:成功 {zyk_resolve_stat['ok']} 个 / "
            f"失败 {zyk_resolve_stat['fail']} 个(失败仍用随机时长兜底,不影响进度)", "INFO")
    if ctype == "RESOURCE" and zyk_noop_stat["skipped"]:
        log(f"[{nickname}] RESOURCE 心跳去重:平台位置已达标免发 {zyk_noop_stat['skipped']} 个课件"
            f"（省下约 {zyk_noop_stat['beats_saved']} 条重复上报）", "INFO")
    if ctype == "RESOURCE" and (zyk_task_stat["ok"] + zyk_task_stat["noop"] + zyk_task_stat["skipped"]):
        _tt = f"，通道被拒后跳过 {zyk_task_stat['skipped']} 个" if zyk_task_stat["skipped"] else ""
        _tt += f"，平台已满免发 {zyk_task_stat['noop']} 个" if zyk_task_stat["noop"] else ""
        log(f"[{nickname}] RESOURCE 测验/考试/讨论纳管:计数形心跳标满 {zyk_task_stat['ok']} 个{_tt}", "INFO")
    if ctype == "RESOURCE" and (zyk_swf_stat["ok"] + zyk_swf_stat["noop"] + zyk_swf_stat["skipped"]):
        _dt = f"，本课已关闭该通道跳过 {zyk_swf_stat['skipped']} 个" if zyk_swf_stat["skipped"] else ""
        _dt += f"，平台已满免发 {zyk_swf_stat['noop']} 个" if zyk_swf_stat["noop"] else ""
        log(f"[{nickname}] RESOURCE SWF 纳管:计数形心跳成功 {zyk_swf_stat['ok']} 个{_dt}", "INFO")

    # ---- 资源库平台复核：拿平台自己的读数说话，不拿"HTTP 200 的条数"当完成 ----
    if ctype == "RESOURCE" and leaf_cells:
        _rv = client.zyk_get_course_tree(course_info_id, leaf_workers=RESOURCE_SCAN_WORKERS)
        _sp_map = {}
        for _l in _rv or []:
            _ssr = _l.get("studentStudyRecord")
            _sp_map[str(_l.get("id"))] = _zyk_cell_speed({"_speed": (_ssr or {}).get("speed") if isinstance(_ssr, dict) else None})
        if not _sp_map:
            log(f"[{nickname}] ⚠️ RESOURCE 平台复核读回为空（不计百分比，请以平台页面为准）", "WARNING")
        else:
            _full = sum(1 for c in leaf_cells if _sp_map.get(str(c.get("id")), 0.0) >= 100)
            log(f"[{nickname}] RESOURCE 平台复核:本次课件已学完 {_full}/{len(leaf_cells)} 个", "INFO")

    # 刷新进度
    _refresh_progress(client, ctype, course_info_id, class_id)


def _video_url_or_resolve(client: ZjyClient, ctype: str, cell: dict, raw,
                           zyk_cache: dict, zyk_stat: Optional[dict] = None) -> str:
    """课件视频地址提取:先按原有口径,提不到且属资源库时向平台即时解析一次。

    背景(2026-09-19 普查 11 门资源库课 / 2192 个课件叶子):资源库树接口的 fileUrl 有四种
    形态——绝对 URL、`doc|zyk/g@<HEX>.ext`、`doc/e@<HEX>.ext`、空;后两类相对短链占 64%,
    是"未绑定知识点"那批资源的存储方式(目录分片号不可推导,本地拼前缀必 404),
    extract_file_url 提不出地址 → 整课视频退化成随机时长冒充真实视频长度。
    平台只在"点开单课件"的详情接口里即时解析,故此处按 cellId 补一次只读请求。

    约束:仅 ctype=='RESOURCE' 触发(SPOC/MOOC 行为逐字不变);同轮按 cellId 去重缓存,
    失败同样缓存以免重试打点;任何取不到地址的情况返回 "",由调用方原样回落随机时长,
    绝不因此跳过课件或计入失败。
    """
    url = extract_file_url(raw)
    if url or ctype != "RESOURCE":
        return url
    cell_id = str((cell or {}).get("id") or "")
    if not cell_id:
        return ""
    if cell_id not in zyk_cache:
        try:
            resolved = client.zyk_get_cell_file_url(cell_id)
        except Exception:
            resolved = ""
        # 双保险:客户端层已限定只回绝对地址,这里再校验一次,
        # 防未来实现松动时把相对串直接送进 Range 请求(表现为"解析异常"噪音)
        resolved = resolved if str(resolved or "").startswith("http") else ""
        zyk_cache[cell_id] = resolved
        if zyk_stat is not None:
            zyk_stat["ok" if zyk_cache[cell_id] else "fail"] += 1
    return zyk_cache[cell_id]


def _parse_mp4_durations_parallel(client: ZjyClient, nickname: str, leaf_cells: list,
                                   ctype: str = "SPOC", zyk_cache: Optional[dict] = None,
                                   zyk_stat: Optional[dict] = None,
                                   workers: int = 4, skip_speed_full: bool = False) -> dict:
    """并行解析所有视频课件的 MP4 时长。

    :param workers: 并发度（资源库短链占比高、每格还要 1~4 次 Range 请求，故单独放宽）
    :param skip_speed_full: True=跳过平台已记满的格（这类格下面直接免发，解析结果用不上）
    """
    if zyk_cache is None:
        zyk_cache = {}
    mp4_parse_tasks = []
    for _idx, _cell in enumerate(leaf_cells):
        _cell_type = (_cell.get("fileType") or "").lower()
        _file_url_raw = _cell.get("fileUrl")
        if skip_speed_full and _zyk_cell_speed(_cell) >= 100:
            continue
        # MOOC 真实时长档：平台树里的 videoTime 就是最终申报值，解析值没有任何消费者，
        # 不必为它花 ≥1 次外部请求 + ≥128KB 出口流量（非 faststart 视频最坏 +2MB）。
        if ctype == "MOOC" and _mooc_video_time(_cell) > 0:
            continue
        if _cell_type in VIDEO_TYPES and _file_url_raw:
            mp4_parse_tasks.append((_idx, _cell))

    if not mp4_parse_tasks:
        return {}

    log(f"[{nickname}] 🔍 并行解析 {len(mp4_parse_tasks)} 个视频时长...", "INFO")
    cache = {}

    def _parse(task):
        _i, _c = task
        try:
            _ori_url = _video_url_or_resolve(client, ctype, _c, _c.get("fileUrl"),
                                             zyk_cache, zyk_stat)
            if _ori_url:
                # 音频课件先走 MP3 解析(生产 2026-09-16 消噪移植):MP3 无 moov box,
                # 直进 MP4 三策略必失败;先 MP3、失败回落 MP4(m4a 实为 MP4 容器,双跳零回归)。
                _dur = None
                if (_c.get("fileType") or "").lower() in AUDIO_TYPES:
                    _dur = get_mp3_duration(_ori_url)
                if not _dur:
                    _dur = get_mp4_duration(_ori_url)
                return (_i, _dur if _dur and _dur > 0 else None)
        except Exception:
            pass
        return (_i, None)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = {executor.submit(_parse, t): t for t in mp4_parse_tasks}
        for f in as_completed(futures):
            _i, _dur = f.result()
            cache[_i] = _dur

    return cache


def _calculate_total_time(client: ZjyClient, nickname: str, cell: dict, idx: int,
                           class_id: str, course_info_id: str, course_id: str,
                           ctype: str, cell_type: str, file_url_raw, mp4_cache: dict,
                           zyk_cache: Optional[dict] = None,
                           zyk_stat: Optional[dict] = None) -> float:
    """计算课件的目标学习时长(秒)。

    优化:不再对每个课件串行查询 spoc/studyRecord/list(385个课件=385次HTTP请求,极慢)。
    改为优先用 MP4 时长和 studentStudyRecord,仅在都失败时才惰性查询 studyRecord/list。

    MOOC 走"真实时长档"提前返回：申报值直接取平台树的 videoTime，读不到才用解析值/按类型定值。
    """
    if ctype == "MOOC":
        return _mooc_target_seconds(cell, mp4_cache.get(idx), (cell.get("fileType") or "").lower())

    # SPOC 真实时长档：申报总长 = 解析长度（或文档定值）× 系数，条数 = 申报总长/5。
    # 放在函数最前面是有意的：下面那串"旧记录时长 → 逐格 GET 知识点详情 → studyRecord/list
    # → random(600,1800)"的阶梯里，**没有一项是课件的真实长度**（平台侧压根不给），
    # 继续走下去只会白烧一次每格 RTT 再虚增时长。知识点讲解格同样按定值申报（不再逐格 GET）。
    if ctype == "SPOC" and SPOC_V2:
        return _spoc_target_seconds(_spoc_kind_of(cell_type), mp4_cache.get(idx))

    total_time = None
    spoc_record_time = None
    if zyk_cache is None:
        zyk_cache = {}

    # 从 studentStudyRecord 获取时长(本地数据,无网络请求)
    record_time = None
    ssr = cell.get("studentStudyRecord")
    if isinstance(ssr, dict):
        for key in ["totalNum", "resourceTotalNum"]:
            t_num = ssr.get(key)
            if t_num is not None:
                try:
                    if int(t_num) > 0:
                        record_time = int(t_num)
                        break
                except Exception:
                    pass

    # 知识点讲解类型:通过 getStudyCellInfo 获取视频时长
    if cell.get("_is_knowledge_explain") and ctype not in ("MOOC", "RESOURCE"):
        ke_info = client.get_knowledge_explain_video_info(cell.get("id"), class_id)
        if ke_info:
            if ke_info.get("totalNum") and ke_info["totalNum"] > 0:
                record_time = ke_info["totalNum"]
                spoc_record_time = ke_info["totalNum"]
            if ke_info.get("fileUrl"):
                file_url_raw = ke_info["fileUrl"]
                cell_type = "video"
        else:
            log(f"[{nickname}] ⚠️ {cell.get('name', '?')} 知识点讲解详情获取失败,用随机时长", "WARNING")

    # MP4 真实时长(优先级最高)
    mp4_time = None
    if cell_type in VIDEO_TYPES and file_url_raw:
        cached_dur = mp4_cache.get(idx)
        if cached_dur is not None:
            mp4_time = cached_dur
        else:
            try:
                ori_url = _video_url_or_resolve(client, ctype, cell, file_url_raw,
                                                zyk_cache, zyk_stat)
                if ori_url:
                    # 音频先 MP3 解析(同并行路径),失败回落 MP4
                    parsed = None
                    if cell_type in AUDIO_TYPES:
                        parsed = get_mp3_duration(ori_url)
                    if not parsed:
                        parsed = get_mp4_duration(ori_url)
                    if parsed and parsed > 0:
                        mp4_time = parsed
                    else:
                        log(f"[{nickname}] ⚠️ {cell.get('name','?')} MP4时长解析失败,将用随机时长", "WARNING")
            except Exception as e:
                log(f"[{nickname}] ⚠️ {cell.get('name','?')} 解析异常: {e},将用随机时长", "WARNING")

    # 时长优先级:MP4真实时长 > 知识点讲解totalNum > 旧记录时长(视频不用旧记录)
    if mp4_time and mp4_time > 0:
        total_time = mp4_time
    elif cell.get("_is_knowledge_explain") and record_time and record_time > 0:
        total_time = record_time
    elif record_time and record_time > 0 and cell_type not in VIDEO_TYPES:
        total_time = record_time
    elif spoc_record_time and spoc_record_time > 0 and cell_type not in VIDEO_TYPES:
        total_time = spoc_record_time

    # 惰性查询:仅当以上都未获取到时长时,才查询 spoc/studyRecord/list
    # (优化:避免对每个课件都发起此请求,385个课件可省去绝大多数HTTP调用)
    if total_time is None and ctype not in ("MOOC", "RESOURCE"):
        try:
            rec_data = client.api_get("spoc/studyRecord/list", {
                "classId": class_id, "courseInfoId": course_info_id,
                "sourceId": cell.get("id"), "pageNum": "1", "pageSize": "5",
            })
            if rec_data:
                rows = client.extract_rows(rec_data)
                for r in rows:
                    tn = r.get("totalNum") or r.get("resourceTotalNum")
                    if tn is not None:
                        try:
                            tn = int(tn)
                            if tn > 0:
                                total_time = tn
                                break
                        except Exception:
                            pass
        except Exception:
            pass

    # 随机时长兜底
    if total_time is None:
        total_time = _random_duration(cell_type)

    # 最短时长约束
    if total_time < 60 and cell_type not in IMAGE_TYPES:
        total_time = random.randint(60, 180)
    # 图片类型确保有合理浏览时长
    if cell_type in IMAGE_TYPES and total_time < 30:
        total_time = random.randint(30, 60)

    return total_time


def _random_duration(cell_type: str) -> int:
    """根据课件类型生成随机时长。"""
    if cell_type in IMAGE_TYPES:
        return random.randint(5, 15)
    if cell_type in ["pdf", "ppt", "word", "excel", "doc", "文档", "图文"]:
        return random.randint(300, 600)
    if cell_type in VIDEO_TYPES:
        return random.randint(600, 1800)
    return random.randint(300, 900)


def _submit_heartbeat(client: ZjyClient, nickname: str, cell: dict, idx: int, total: int,
                      class_id: str, course_info_id: str, course_id: str,
                      ctype: str, cell_type: str, total_time: int,
                      aes_key: Optional[str],
                      lane_ctx: Optional[dict] = None,
                      zyk_swf_stat: Optional[dict] = None,
                      zyk_noop_stat: Optional[dict] = None,
                      zyk_task_stat: Optional[dict] = None) -> str:
    """提交心跳包,根据课程类型走不同分支。

    返回 `"ok"`/`"fail"`（三类通用），资源库另可返回 `"noop"`（平台已记满，无需上报）
    与 `"skip"`（本课已关闭该通道，不计成功也不计失败）。
    """
    if ctype == "MOOC":
        ok = _submit_mooc_heartbeat(client, nickname, cell, idx, total, class_id, course_info_id,
                                    course_id, cell_type, total_time, lane_ctx)
        if ok == "dispatched":
            return "dispatched"
        return "ok" if ok else "fail"
    elif ctype == "RESOURCE":
        return _submit_resource_heartbeat(client, nickname, cell, course_info_id,
                                          course_id, cell_type, total_time,
                                          zyk_swf_stat if zyk_swf_stat is not None else
                                          {"ok": 0, "noop": 0, "skipped": 0, "disabled": False},
                                          zyk_noop_stat if zyk_noop_stat is not None else
                                          {"skipped": 0, "beats_saved": 0, "fallback": 0},
                                          zyk_task_stat if zyk_task_stat is not None else
                                          {"ok": 0, "noop": 0, "skipped": 0, "disabled": False})
    else:
        ok = _submit_spoc_heartbeat(client, nickname, cell, class_id, course_info_id,
                                    course_id, cell_type, total_time, aes_key, lane_ctx)
        if ok == "dispatched":
            return "dispatched"
        return "ok" if ok else "fail"


def _submit_mooc_heartbeat(client: ZjyClient, nickname: str, cell: dict, idx: int, total: int,
                            class_id: str, course_info_id: str, course_id: str,
                            cell_type: str, total_time: float,
                            lane_ctx: Optional[dict] = None):
    """MOOC 心跳提交:6个API探测 → 车道串行流 / 单课件内并发回退。

    车道模式下返回 `"dispatched"`（成败与"平台接受条数"改由主循环收割时判定），其余返回 bool。
    """
    cell_id = cell.get("id")
    is_image = _mooc_is_img(cell_type)

    if is_image:
        _img_count = 1
        mooc_record = {
            "actualNum": _img_count, "courseId": course_id, "courseInfoId": course_info_id,
            "id": str(uuid.uuid4()).upper(), "lastNum": _img_count, "params": {},
            "resourceTotalNum": _img_count, "sourceId": cell_id, "speed": 100.0,
            "studentId": client.stu_id, "studyDuration": total_time, "totalNum": _img_count,
        }
    else:
        mooc_record = {
            "actualNum": total_time, "courseId": course_id, "courseInfoId": course_info_id,
            "id": str(uuid.uuid4()).upper(), "lastNum": total_time, "params": {},
            "resourceTotalNum": total_time, "sourceId": cell_id, "speed": 100.0,
            "studentId": client.stu_id, "studyDuration": total_time, "totalNum": total_time,
        }
    if class_id:
        mooc_record["classId"] = class_id

    # 条数由申报值决定（见 _mooc_payloads）：时长 = 5 秒 × 被接受条数，
    # 且不设旧口径的 600 条封顶——真实时长档下长视频必须发满才到 100%
    heartbeat_interval = 5

    # 探测可用 API(全灭→冷却 15s+重认证 AI 域后二轮探测;仍败才计失败——
    # 实测限流为瞬时抖动,冷却后大概率恢复;原单轮即弃使一次抖动废掉整个课件)
    probe_last = {}
    working_api = _probe_mooc_api(client, mooc_record, heartbeat_interval, probe_last)
    if not working_api:
        _pmsg = _mooc_probe_refusal(probe_last)
        if _pmsg:
            # 平台按课程"开课时间"整课拒绝学习：这时冷却 15 秒 + 重鉴权 + 二轮探测纯属白等
            # （实测一门 6 格的课因此耗掉 109 秒且零入账）。直接把平台原因报出来，
            # 交给上方的连续失败保护收尾，不改判定、不跳过任何本可成功的课件。
            log(f"[{nickname}] 刷进度 ❌ [{idx+1}/{total}] {cell.get('name','?')} "
                f"平台拒绝学习：{_pmsg[:60]}", "WARNING")
            return False
        time.sleep(15)
        try:
            client.auth_ai_domain()
        except Exception:
            pass
        working_api = _probe_mooc_api(client, mooc_record, heartbeat_interval, probe_last)
    if not working_api:
        log(f"[{nickname}] 刷进度 ❌ [{idx+1}/{total}] {cell.get('name','?')} MOOC提交全部失败(无可用API)", "WARNING")
        return False

    method, api_path, use_ai = working_api

    payloads = _mooc_payloads(mooc_record, total_time, is_image)
    if lane_ctx is not None:
        # 车道模式：本格整条心跳流派给一条车道**串行**发送，主循环立刻派发下一格。
        # 心跳的位置语义是"递增才记"，同一课件内部乱序并发有少记风险；课件之间彼此独立，
        # 所以并行度放在"课件 × 车道"这一层，课件内保持严格串行。
        while lane_ctx["running"] and len(lane_ctx["running"]) >= MOOC_LANES:
            _dn, _ = wait(lane_ctx["running"], timeout=2.0, return_when=FIRST_COMPLETED)
            lane_ctx["running"] -= _dn
            if not _dn:
                break            # 干等 2 秒无进展：不再阻塞派发，交给池自己的队列
        _fut = _LANE_POOL.submit(_mooc_beat_stream, client, payloads, method, api_path, use_ai)
        if lane_ctx["api"] is None:
            # 首个探测到的端点记给"平台复核补满"复用，省掉补满时每格一次探测
            lane_ctx["api"] = (method, api_path, use_ai)
        lane_ctx["futs"].append((f"[{idx+1}/{total}] {cell.get('name','?')}"[:48], _fut))
        lane_ctx["running"].add(_fut)
        return "dispatched"
    return _mooc_fast_concurrent(client, payloads, method, api_path, use_ai)


def _mooc_payloads(mooc_record: dict, total_time, is_image: bool) -> list:
    """MOOC 快速模式的心跳流：位置从 0 每 5 秒一跳递增，**末条精确等于申报值**。

    两条实测硬要求：
    1) 完成度 = 末条 actualNum ÷ 申报 totalNum，而 videoTime 常带小数（实测 1655.37），
       等步长累加的浮点误差会停在 99%（实测 380 条全 200 仍判未完成）；
    2) 图片类沿用被长期验证的 totalNum=actualNum=1 计数形，只由条数控制时长。
    """
    tgt = float(total_time)
    out = []
    if is_image:
        for _ in range(max(1, int(tgt // MOOC_BEAT_STEP))):
            hb = dict(mooc_record)
            hb["id"] = str(uuid.uuid4()).upper()
            hb["studyDuration"] = MOOC_BEAT_STEP
            out.append(hb)
        return out
    pos = 0.0
    while pos < tgt:
        pos = min(pos + MOOC_BEAT_STEP, tgt)
        if pos >= tgt:
            pos = tgt                      # 消除浮点累加误差，保证末条精确到位
        hb = dict(mooc_record)
        hb["id"] = str(uuid.uuid4()).upper()
        hb["studyDuration"] = MOOC_BEAT_STEP
        hb["actualNum"] = pos
        hb["lastNum"] = pos
        hb["totalNum"] = tgt
        out.append(hb)
    return out


def _mooc_base_record(cell: dict, course_id: str, course_info_id: str, stu_id: str,
                      class_id: str, target, is_image: bool) -> dict:
    """平台复核补满用的基准心跳体（字段集与主路径 mooc_record 一致）。"""
    n = 1 if is_image else float(target)
    rec = {"actualNum": n, "courseId": course_id, "courseInfoId": course_info_id,
           "id": str(uuid.uuid4()).upper(), "lastNum": n, "params": {},
           "resourceTotalNum": n, "sourceId": cell.get("id"), "speed": 100.0,
           "studentId": stu_id, "studyDuration": float(target), "totalNum": n}
    if class_id:
        rec["classId"] = class_id
    return rec


def _mooc_send(client: ZjyClient, p: dict, method: str, api_path: str, use_ai: bool):
    """单条 MOOC 心跳提交（POST/PUT × AI域/主域四种组合）。"""
    try:
        if use_ai:
            return client.api_put_ai(api_path, p) if method == "PUT" else client.api_post_ai(api_path, p)
        return client.api_put(api_path, p) if method == "PUT" else client.api_post(api_path, p)
    except Exception:
        return None


def _mooc_beat_stream(client: ZjyClient, payloads: list, method: str, api_path: str, use_ai: bool) -> int:
    """一条课件流的心跳**严格串行**发送，返回平台真正接受的条数（车道模式用）。"""
    accepted = 0
    for p in payloads:
        r = _mooc_send(client, p, method, api_path, use_ai)
        if r and r.get("code") == 200:
            accepted += 1
    return accepted


def _probe_mooc_api(client: ZjyClient, mooc_record: dict, heartbeat_interval: int,
                    last: Optional[dict] = None):
    """探测 MOOC 可用的心跳提交 API。

    :param last: 传 dict 时把探测阶段收到的平台响应原文累积进 `last["msgs"]`，供调用方判断
                 是"整课被拒"还是"瞬时抖动"（此前这些原文被丢弃，日志只剩"无可用API"四个字）。
    """
    mooc_submit_apis = [
        ("POST", "course/studyRecord", True),
        ("PUT", "course/studyRecord", True),
        ("POST", "course/mooc/studyRecord", True),
        ("PUT", "course/mooc/studyRecord", True),
        ("POST", "spoc/course/mooc/studyRecord", False),
        ("PUT", "spoc/course/mooc/studyRecord", False),
    ]
    for method, api_path, use_ai in mooc_submit_apis:
        test_record = dict(mooc_record)
        test_record["studyDuration"] = heartbeat_interval
        test_record["actualNum"] = heartbeat_interval
        test_record["lastNum"] = heartbeat_interval
        try:
            if use_ai:
                if method == "PUT":
                    result = client.api_put_ai(api_path, test_record)
                else:
                    result = client.api_post_ai(api_path, test_record)
            else:
                if method == "PUT":
                    result = client.api_put(api_path, test_record)
                else:
                    result = client.api_post(api_path, test_record)
            if result and result.get("code") == 200:
                return (method, api_path, use_ai)
            if last is not None and result:
                last.setdefault("msgs", []).append(str(result.get("msg") or ""))
        except Exception:
            continue
    return None


def _mooc_probe_refusal(probe_last: dict) -> str:
    """从探测阶段的全部平台响应里取"整课被拒"的原文（非开课时间是课程级闸门，
    只要有一条这么答就算，末条常被主域端点的其它错误覆盖）。无此类响应返回 ""。
    """
    for m in (probe_last or {}).get("msgs") or []:
        if "非开课时间" in m:
            return m
    return ""



def _mooc_fast_concurrent(client: ZjyClient, payloads: list,
                           method: str, api_path: str, use_ai: bool) -> bool:
    """MOOC 快速模式（单课件内并发）：车道未启用时的回退路径。"""
    ok_count = 0
    _batch_size = 100
    for _batch_start in range(0, len(payloads), _batch_size):
        _batch = payloads[_batch_start:_batch_start + _batch_size]
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_mooc_send, client, p, method, api_path, use_ai): p for p in _batch}
            for f in as_completed(futures):
                try:
                    r = f.result()
                    if r and r.get("code") == 200:
                        ok_count += 1
                except Exception:
                    pass

    # 全部失败时并发重试前30条
    if ok_count == 0:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_mooc_send, client, p, method, api_path, use_ai): p
                       for p in payloads[:30]}
            for f in as_completed(futures):
                try:
                    r = f.result()
                    if r and r.get("code") == 200:
                        ok_count += 1
                except Exception:
                    pass

    return ok_count > 0


def _submit_resource_heartbeat(client: ZjyClient, nickname: str, cell: dict,
                                course_info_id: str, course_id: str,
                                cell_type: str, total_time: int,
                                swf_stat: dict, noop_stat: dict,
                                task_stat: Optional[dict] = None) -> str:
    """资源库心跳提交:zyk 域明文 JSON,URL 末尾斜杠必需。

    三条口径（实测，与旧"同一位置重复发 total_time//10 条"的差别就是资源库慢的真凶）：
    ① 平台只在 `actualNum` **严格增大**时才推进记录，同位置重复上报一律返 200 却不写回
       ⇒ 改申报式递增流：每格最多 `ZYK_MAX_HOPS` 跳、末条精确等于 total_time；
    ② 不从用户已看位置续推，流起点恒 0（平台位置只增，低位置申报被忽略不会倒退）；
       平台 speed 已满的格整格免发——再发也不增加任何东西；
    ③ 「计进度但没有可看媒体」的格——`.swf` 动画与 测验/考试/讨论——走图片同款计数形
       （totalNum=actualNum=1）单跳即满；首格被拒即对本课关闭**该条通道**并跳过剩余格，
       **不计失败**（连败会触发熔断把整门课拖死）。两条通道**分账**，一种被拒不牵连另一种。

    :return: "ok" 已达标 / "noop" 平台已满免发 / "skip" 本课已关闭该通道 / "fail" 失败
    """
    cell_id = cell.get("id")
    is_image = cell_type in IMAGE_TYPES
    is_swf = bool(cell.get("_zyk_swf"))
    is_task = bool(cell.get("_zyk_task"))
    cell_parent_id = cell.get("parentId", "") or ""
    speed = _zyk_cell_speed(cell)

    def _beat(study_time, total, as_image):
        try:
            r = client.zyk_submit_heartbeat(course_id, course_info_id, cell_id,
                                            study_time, total,
                                            parent_id=cell_parent_id,
                                            student_id=client.stu_id,
                                            is_image=as_image)
        except Exception:
            return None
        return bool(r and (r.get("code") == 200 or r.get("code") == 0))

    # 计数形通道：SWF 与 测验/考试/讨论 各自一个开关、各自记账
    _cnt_stat = swf_stat if is_swf else ((task_stat if task_stat is not None
                                          else {"ok": 0, "noop": 0, "skipped": 0, "disabled": False})
                                         if is_task else None)
    _cnt_name = "SWF 课件" if is_swf else "测验/考试/讨论格"
    if _cnt_stat is not None:
        if _cnt_stat["disabled"]:
            _cnt_stat["skipped"] += 1
            return "skip"
        if speed >= 100:
            _cnt_stat["noop"] += 1          # 满格免发，区别于"真发了一跳"
            return "noop"
        if _beat(1, 1, True):
            _cnt_stat["ok"] += 1
            return "ok"
        _cnt_stat["disabled"] = True
        log(f"[{nickname}] ⚠️ {_cnt_name}心跳被平台拒绝 → 本课剩余 {_cnt_name}直接跳过、不计失败"
            f"(id={cell_id}, name={cell.get('name','?')})", "WARNING")
        return "skip"

    if is_image:
        ok_count = 0
        for _ in range(min(max(1, total_time // 10), 5)):
            if _beat(total_time, total_time, True):
                ok_count += 1
        return "ok" if ok_count else "fail"

    if speed >= 100:
        # 旧口径这一格要打 total//10 条同位置心跳（最多 200 条）。免发计数用平台已存的申报总长
        # 折算，而不是本轮的随机 total_time（满格现在连时长都不算了）。
        _ssr = cell.get("studentStudyRecord")
        _decl = _ssr.get("totalNum") if isinstance(_ssr, dict) else None
        try:
            _decl = float(_decl) if _decl is not None else float(total_time or 0)
        except (TypeError, ValueError):
            _decl = 0.0
        noop_stat["skipped"] += 1
        noop_stat["beats_saved"] += int(max(1, _decl // ZYK_BEAT_STEP))
        return "noop"

    ok_count = 0
    for _a, _t in _zyk_incr_pairs(None, total_time, _zyk_hop_step(total_time)):
        if _beat(_a, _t, False):
            ok_count += 1
    if ok_count:
        return "ok"

    # 递增流零接受（含申报总长为脏值导致空序列）→ 回退旧单发一次，宁多不漏
    noop_stat["fallback"] += 1
    return "ok" if _beat(total_time, total_time, False) else "fail"


def _submit_spoc_heartbeat(client: ZjyClient, nickname: str, cell: dict,
                            class_id: str, course_info_id: str, course_id: str,
                            cell_type: str, total_time: int,
                            aes_key: Optional[str], lane_ctx: Optional[dict] = None):
    """SPOC 心跳提交:AES-128-ECB 加密,服务器每次+5秒。

    lane_ctx 非空 = SPOC 真实时长档已启用 ⇒ 走**跨课件车道**：本格整条心跳流一次加密好，
    交给一条车道严格串行发完，主循环立刻派发下一格，返回 "dispatched" 由收尾统一结算
    （真值在"车道回收 + 平台复核"里逐格判，与 MOOC 同口径）。
    lane_ctx 为空或 SPOC_V2=False ⇒ 逐字退回旧的"格内 10 路并发"形态。
    """
    if not aes_key:
        log(f"[{nickname}] SPOC 刷课失败: AES 密钥为空(token缺失)", "ERROR")
        return False

    cell_id = cell.get("id")
    is_image = cell_type in IMAGE_TYPES

    if lane_ctx is not None and SPOC_V2:
        _kind = _spoc_kind_of(cell_type)
        _recs = _spoc_beat_payloads(client, aes_key, class_id, course_info_id, cell_id,
                                    client.stu_id, total_time, SPOC_BEAT_STEP,
                                    _kind == "img", SPOC_IMG_BEATS)
        _bodies = [b for b in (_spoc_encrypt(client, aes_key, r) for r in _recs) if b]
        if not _bodies:
            return False
        # 在飞车道数闸门：满了就等最早的让位，最多干等 60 秒后强行派发（防卡死）
        _spin = 0
        while len(lane_ctx["running"]) >= SPOC_LANES:
            _dn, _ = wait(lane_ctx["running"], timeout=2.0, return_when=FIRST_COMPLETED)
            lane_ctx["running"] -= _dn
            _spin += 1
            if _spin > 30 and not _dn:
                break
        _f = lane_ctx["pool"].submit(_spoc_lane_stream, client, _bodies)
        lane_ctx["futs"].append((cell.get("name", "?"), _f))
        lane_ctx["running"].add(_f)
        # 复核对账用：(格号, cellId, 申报总长, 课件名)。图片格申报 1 秒，复核按 <=1 豁免
        lane_ctx["items"].append((len(lane_ctx["items"]), cell_id, float(total_time),
                                  cell.get("name", "?")))
        return "dispatched"

    if is_image:
        hb_count = 5
        _img_count = 1
        record_proto = {
            "actualNum": _img_count, "classId": class_id, "courseInfoId": course_info_id,
            "id": "", "lastNum": _img_count, "params": {},
            "resourceTotalNum": _img_count, "sourceId": cell_id, "speed": 100.0,
            "studentId": client.stu_id, "studyTime": total_time, "totalNum": _img_count,
        }
    else:
        hb_count = min(max(1, total_time), 2000)
        record_proto = {
            "actualNum": total_time, "classId": class_id, "courseInfoId": course_info_id,
            "id": "", "lastNum": total_time, "params": {},
            "resourceTotalNum": total_time, "sourceId": cell_id, "speed": 100.0,
            "studentId": client.stu_id, "studyTime": total_time, "totalNum": total_time,
        }

    return _spoc_fast_concurrent(client, record_proto, hb_count, total_time,
                                  is_image, aes_key, nickname)



def _spoc_fast_concurrent(client: ZjyClient, record_proto: dict, hb_count: int,
                           total_time: int, is_image: bool, aes_key: str,
                           nickname: str = "") -> bool:
    """SPOC 快速模式:高并发批量提交(服务器每次+5秒)。

    nickname 用于失败诊断日志(修复 NameError:原实现引用未定义 nickname,
    心跳全灭走诊断分支时必崩)。"""
    _progress_num = 1 if is_image else total_time
    payloads = []
    for _ in range(hb_count):
        hb = dict(record_proto)
        hb["id"] = str(uuid.uuid4()).upper()
        hb["studyTime"] = total_time
        hb["actualNum"] = _progress_num
        hb["lastNum"] = _progress_num
        json_str = json.dumps(hb, separators=(',', ':'), sort_keys=True)
        encrypted = client.aes_encrypt(json_str, aes_key)
        if encrypted:
            safe_enc = encrypted.replace('%', '%25').replace('+', '%2B')
            payloads.append({"param": safe_enc})

    if not payloads:
        return False

    def _submit(p):
        try:
            _r = client.session.post(f"{BASE_URL}/spoc/studyRecord", json=p, timeout=15)
            if _r.status_code == 200:
                return _r.json()
        except Exception:
            pass
        return None

    ok_count = 0
    _batch_size = 100
    for _batch_start in range(0, len(payloads), _batch_size):
        _batch = payloads[_batch_start:_batch_start + _batch_size]
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_submit, p): p for p in _batch}
            for f in as_completed(futures):
                try:
                    r = f.result()
                    if r and r.get("code") == 200:
                        ok_count += 1
                except Exception:
                    pass

    # 全部失败时重试前10条,并打印诊断日志
    if ok_count == 0:
        # 诊断:打印第一条 payload 的实际响应,定位失败原因
        if payloads:
            try:
                _diag_r = _submit(payloads[0])
                if _diag_r:
                    log(f"[{nickname}] [诊断] SPOC心跳响应: code={_diag_r.get('code')}, msg={str(_diag_r.get('msg',''))[:200]}", "WARNING")
                else:
                    log(f"[{nickname}] [诊断] SPOC心跳无响应(可能token过期或网络异常)", "WARNING")
            except Exception as _e:
                log(f"[{nickname}] [诊断] SPOC心跳请求异常: {_e}", "WARNING")
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_submit, p): p for p in payloads[:10]}
            for f in as_completed(futures):
                try:
                    r = f.result()
                    if r and r.get("code") == 200:
                        ok_count += 1
                except Exception:
                    pass

    return ok_count > 0


def _refresh_progress(client: ZjyClient, ctype: str, course_info_id: str, class_id: str) -> None:
    """刷课后刷新进度。"""
    if ctype == "MOOC":
        client.api_get_ai("spoc/mooc/course/study/refresh", {"courseInfoId": course_info_id})
    elif ctype == "RESOURCE":
        client.zyk_refresh_progress(course_info_id)
    else:
        client.api_get("spoc/fast/course/study/refresh", {"courseInfoId": course_info_id, "classId": class_id})


# ==================== Part 1.5: 自动答题 ====================

def _brush_exam(client: ZjyClient, nickname: str, class_id: str,
                course_info_id: str, course_id: str, ctype: str) -> None:
    """自动答题:扫描课程下所有未提交/低分的作业/考试/测验并自动完成。

    答案数据源优先级:
    - 资源库:zyk_get_homework_answers(直接抓取服务端标准答案)
    - SPOC/MOOC:find_classmate_answers(学生号直取/同学扫包) + 教师号预览 + 题库兜底
    """
    from answer import (get_course_exams_list, _is_low_score, do_auto_answer_single_exam,
                         zyk_exam_window_closed)

    log(f"[{nickname}] 🚀 开始自动答题...", "INFO")
    try:
        exams = get_course_exams_list(client, class_id, course_info_id, course_id, ctype)
        unsubmitted_exams = [e for e in exams if _is_low_score(e)]

        if not unsubmitted_exams:
            log(f"[{nickname}] 没有发现未提交或低分的作业或考试", "INFO")
            return

        log(f"[{nickname}] 发现 {len(unsubmitted_exams)} 个未提交的作业/考试,开始逐一答题...", "INFO")
        exam_success = 0
        exam_fail = 0
        exam_window_skip = 0
        for exam in unsubmitted_exams:
            exam_id = exam.get("id") or exam.get("examId")
            title = exam.get("title", "未命名任务")
            etype = exam.get("type", "")
            category_id = "2" if etype == "考试" else ("3" if etype == "测验" else "1")
            ok, msg = do_auto_answer_single_exam(
                client, nickname, exam_id, class_id, course_info_id, course_id, ctype, title, category_id
            )
            if ok:
                exam_success += 1
            elif ctype == "RESOURCE" and zyk_exam_window_closed(msg):
                # 平台按作答窗口拒绝：不是链坏了，也不该记成失败（SPOC/MOOC 此计数恒 0，汇总串不变）
                exam_window_skip += 1
            else:
                exam_fail += 1
            time.sleep(1)
        _ztail = f",窗口未开放/已结束跳过 {exam_window_skip} 个" if exam_window_skip else ""
        log(f"[{nickname}] 🎉 自动答题结束:成功 {exam_success} 个,失败 {exam_fail} 个{_ztail}", "INFO")
    except Exception as e:
        log(f"[{nickname}] 自动答题环节异常: {e}", "ERROR")


# ==================== Part 2: 刷讨论 ====================

def _brush_discussion(client: ZjyClient, nickname: str, class_id: str,
                       course_info_id: str, course_id: str, ctype: str) -> None:
    """自动回复讨论:课堂活动/MOOC板块/课件讨论三分支。"""
    log(f"[{nickname}] 🚀 开始自动回复讨论...", "INFO")
    discuss_count = 0

    # 2.1 课堂活动讨论(仅 SPOC)
    if class_id and ctype == "SPOC":
        discuss_count += _brush_classroom_discussion(client, nickname, class_id, course_info_id, course_id)

    # 2.2 MOOC 板块讨论
    elif ctype == "MOOC":
        discuss_count += _brush_mooc_discussion(client, nickname, course_info_id, course_id)

    # 2.3 课件讨论(SPOC/NZYK)
    if ctype not in ("MOOC", "RESOURCE"):
        discuss_count += _brush_courseware_discussion(client, nickname, class_id, course_info_id, course_id, ctype)

    log(f"[{nickname}] 🎉 讨论回复结束:共成功回复 {discuss_count} 个讨论", "INFO")


def _brush_classroom_discussion(client: ZjyClient, nickname: str, class_id: str,
                                 course_info_id: str, course_id: str) -> int:
    """扫描并回复课堂活动讨论。"""
    log(f"[{nickname}] 💬 正在扫描随堂活动讨论...", "INFO")
    count = 0
    for req_type in ["1", "2", "3"]:
        params = {
            "classId": class_id, "courseInfoId": course_info_id, "courseId": course_id,
            "pageNum": "1", "pageSize": "9999", "teachType": "0", "type": "0", "requireType": req_type,
        }
        data = client.api_get("spoc/courseFaceTeachActivity/getCurrentActivityList", params)
        activities = client.extract_rows(data)
        for act in activities:
            atype = act.get("activityType") or act.get("activityTypeId")
            if str(atype) == "4":
                discuss_id = act.get("activityId") or act.get("id") or ""
                teach_id = act.get("teachId", "")
                title = act.get("title", "课堂讨论")
                payload = {
                    "classId": class_id, "courseId": course_id, "courseInfoId": course_info_id,
                    "discussId": discuss_id, "parentId": "0", "requireType": req_type,
                    "teachId": teach_id, "content": random.choice(DISCUSS_CONTENTS),
                    "fileUrl": None, "id": None,
                }
                res = client.api_post("spoc/courseFaceTeachDiscussStudent/", payload)
                if res and res.get("code") == 200:
                    count += 1
                    log(f"[{nickname}] 💬 回复课堂讨论 ✅ {title}", "INFO")
                time.sleep(0.3)
    return count


def _brush_mooc_discussion(client: ZjyClient, nickname: str,
                            course_info_id: str, course_id: str) -> int:
    """扫描并回复 MOOC 板块讨论。"""
    log(f"[{nickname}] 💬 正在扫描MOOC活动讨论...", "INFO")
    count = 0
    page = 1
    all_discuss = []
    while True:
        data = client.api_get_ai("course/courseInfoDiscuss/list", {
            "courseId": course_id, "courseInfoId": course_info_id,
            "pageNum": str(page), "pageSize": "20", "discussType": "4",
            "queryUser": "2", "keyword": "",
        })
        rows = client.extract_rows(data) or []
        if not rows:
            break
        all_discuss.extend(rows)
        total = data.get("total") if isinstance(data, dict) else 0
        if len(all_discuss) >= total or len(rows) < 20:
            break
        page += 1

    for d in all_discuss:
        discuss_id = d.get("id", "")
        d_title = d.get("title", "讨论")

        # 检查是否已回复
        reply_data = client.api_get_ai("course/courseInfoReply/list", {
            "courseId": course_id, "courseInfoId": course_info_id,
            "discussId": discuss_id, "replyId": "0", "typeId": "1",
            "pageNum": "1", "pageSize": "10", "type": "0",
        })
        already_replied = False
        classmate_contents = []
        if reply_data and isinstance(reply_data, dict):
            records = reply_data.get("records") or []
            for rec in records:
                if str(rec.get("userId", "")) == str(client.stu_id):
                    already_replied = True
                else:
                    # content 键存在但值为 null 时 .get 默认值不生效→None.replace 必崩
                    # (生产 M-10 修复移植)
                    c_text = (rec.get("content") or "").replace("<p>", "").replace("</p>", "").strip()
                    if c_text and len(c_text) > 5:
                        classmate_contents.append(c_text)

        if already_replied:
            continue

        reply_text = random.choice(classmate_contents) if classmate_contents else random.choice(DISCUSS_CONTENTS)
        reply_payload = {
            "courseId": course_id, "courseInfoId": course_info_id,
            "discussId": discuss_id, "content": f"<p>{reply_text}</p>", "typeId": 1,
        }
        res = client.api_post_ai("course/courseInfoReply/add", reply_payload)
        if res and res.get("code") == 200:
            count += 1
            log(f"[{nickname}] 💬 回复MOOC讨论 ✅ {d_title}", "INFO")
        time.sleep(0.3)
    return count


def _brush_courseware_discussion(client: ZjyClient, nickname: str, class_id: str,
                                  course_info_id: str, course_id: str, ctype: str) -> int:
    """扫描并回复课件讨论。"""
    log(f"[{nickname}] 💬 正在扫描并回复课件讨论...", "INFO")
    count = 0
    leaf_cells = client.get_course_cells(course_info_id, class_id, course_id, include_completed=True, ctype=ctype)

    discuss_apis = ["spoc/courseInfoDiscuss/"]
    if ctype == "NZYK":
        discuss_apis += ["spoc/nzyk/courseInfoDiscuss/", "spoc/resource/courseInfoDiscuss/"]

    for idx, cell in enumerate(leaf_cells):
        cell_id = cell.get("id", "")
        payload = {
            "discussType": "1", "star": 5, "title": random.choice(DISCUSS_TITLES),
            "content": random.choice(DISCUSS_CONTENTS), "typeId": 1, "classId": class_id,
            "courseId": course_id, "courseInfoId": course_info_id, "courseDesignId": cell_id,
        }
        posted = False
        for api_path in discuss_apis:
            res = client.api_post(api_path, payload)
            if res and res.get("code") == 200:
                count += 1
                posted = True
                break
            elif res and ("classId" in str(res.get("msg", "")) or "不存在" in str(res.get("msg", ""))):
                continue
            else:
                break

        if posted and (count % 10 == 0 or idx == len(leaf_cells) - 1):
            log(f"[{nickname}] 💬 课件讨论回复中...(当前已累计回复 {count} 个讨论)", "INFO")
        time.sleep(0.1)
    return count
