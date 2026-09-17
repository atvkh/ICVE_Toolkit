"""答题模块:资源库 / SPOC / MOOC 三域作业、考试、测验自动答题。

答案数据源优先级:
- 资源库(RESOURCE):zyk_get_homework_answers 直接抓取服务端标准答案 → zyk_submit_exam AES 加密提交
- SPOC/MOOC:find_classmate_answers(学生号直取/同学扫包) → 教师号预览补充 → 题库兜底 → 提交

提交端点:
- MOOC: POST ai.icve.com.cn/prod-api/course/exam/record(提交前 updateExamTime 累加时长)
- SPOC: POST spoc/exam/record(考试 category_id=2 提交后真实等待 exam_time 秒)
- 资源库: zyk_submit_exam(AES-128-ECB 固定密钥加密)
"""

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, Tuple

from zjy_client import ZjyClient
from utils import log

# 考试类型映射:categoryId → 中文类型名
_TYPE_MAP = {"1": "作业", "2": "考试", "3": "测验"}

# 低分阈值(低于此分数视为需要重答)
_LOW_SCORE_THRESHOLD = 60


# ==================== MOOC 重叠流水线助手(2026-09-14,仅 MOOC;SPOC/RESOURCE 绝不进) ====================

def mooc_exam_is_exhausted(msg):
    """205 双语义分流:实测 msg 有两种——「须作答满N分钟」(等即可过)与
    「您已经达到作答次数上限」(等多久都没用)。后者应跳过不算失败,防死等/死重试。"""
    m = str(msg or "")
    return "作答次数" in m and ("上限" in m or "已用完" in m or "用尽" in m)


def mooc_exam_window_closed(msg):
    """205 第三语义分流:「非作答时间禁止进入」=老师设的作答窗口未开放(深夜/未开始/已结束)。
    与「须作答满N分钟」(等待可过)不同,本卷当下不可答;与「作答次数上限」(永久不可交)也不同——
    窗口开放后可重刷。成熟度探测与提交批计数共用本判定。"""
    return "非作答时间" in str(msg or "")


def mooc_stagger_open_exams(client, exams, nickname, jitter=0.5):
    """一次性点开全部未交卷(GET course/exam/paper 有副作用=创建 taskExamRecord,
    createTime 同批起表)。205 闸门只认 now−createTime wall-clock、与期间干什么无关,
    故先起表再去刷课件/讨论,等待被自然消耗。个别失败不阻断(降级=提交时才起表、
    gate_wait 补等满门槛)。返回起表成功数。"""
    opened = 0
    for exam in exams:
        eid = exam.get("id") or exam.get("examId")
        if not eid:
            continue
        try:
            client.api_get_ai("course/exam/paper", {"id": eid, "groupId": "0"})
            opened += 1
        except Exception:
            pass
        time.sleep(jitter)
    if opened:
        log(f"[{nickname}] 🕒 试卷提前起表:已同时打开 {opened} 张计时,稍后按成熟顺序提交", "INFO")
    return opened


def _mooc_gate_wait(client, exam_id, nickname, buffer_s=60):
    """MOOC 205 时间闸门真实等待(仅 MOOC + gate_wait 路径调用)。

    平台提交闸门读 now−taskExamRecord.createTime(wall-clock),updateExamTime 叠加值骗不过;
    门槛分钟数随卷下发于 course/exam/paper.minSubmitDuration(0=无门槛)。
    本函数真实等满 门槛+buffer 后放行提交;min=0/无记录/读失败 → 立即返回零劣化。"""
    try:
        p = client.api_get_ai("course/exam/paper", {"id": exam_id, "groupId": "0"}) or {}
    except Exception:
        return 0
    try:
        gate_min = int(p.get("minSubmitDuration") or 0)
    except (TypeError, ValueError):
        gate_min = 0
    rec = p.get("taskExamRecord") or {}
    ct = rec.get("createTime")
    if gate_min <= 0 or not ct:
        return 0
    try:
        created = datetime.strptime(str(ct), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return 0
    need = (datetime.now() - created).total_seconds()
    wait = gate_min * 60 + buffer_s - need
    if wait <= 0:
        return 0
    log(f"[{nickname}] ⏳ 时间闸门:本卷须真实作答满 {gate_min} 分钟,还需等待 {int(wait)} 秒", "INFO")
    _t0 = time.time()
    while True:
        remain = wait - (time.time() - _t0)
        if remain <= 0:
            break
        time.sleep(min(10, remain))
    waited = int(time.time() - _t0)
    log(f"[{nickname}] ✅ 时间闸门等待完成({waited}s),开始提交", "INFO")
    return waited


def mooc_exam_gate_remaining(client, exam_id, buffer_s=60):
    """探测本卷距成熟还差几秒(与 _mooc_gate_wait 同 buffer=60 口径,保证判成熟的卷
    提交时 gate_wait 零等待)。返回 (remaining_s, gate_min):
    - gate=0/无门槛 → (0,0) 成熟;
    - 有门槛且未到期 → (剩余秒, 门槛分);GET paper 顺带起表(新卷 createTime=now,探测即起表);
    - 205「非作答时间禁止进入」→ 按未成熟返回(留给补交,窗口开放即成功);
    - 读取异常 → (0,0) 保守放行,提交路径内 gate_wait 仍会兜底补等。"""
    try:
        p = client.api_get_ai("course/exam/paper", {"id": exam_id, "groupId": "0"}) or {}
    except Exception:
        return 0, 0
    if isinstance(p, dict) and p.get("code") == 205 and mooc_exam_window_closed(p.get("msg")):
        return 3600 + buffer_s, 0
    try:
        gate_min = int(p.get("minSubmitDuration") or 0)
    except (TypeError, ValueError):
        gate_min = 0
    if gate_min <= 0:
        return 0, 0
    rec = p.get("taskExamRecord") or {}
    ct = rec.get("createTime")
    if not ct:
        return gate_min * 60 + buffer_s, gate_min
    try:
        created = datetime.strptime(str(ct), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return 0, gate_min
    need = (datetime.now() - created).total_seconds()
    rem = gate_min * 60 + buffer_s - need
    return (rem if rem > 0 else 0), gate_min


def _encode_mooc_body(submit_body):
    """MOOC 提交体编码(对齐官方 web 端抓包实证):Base64(URLEncode(紧凑JSON))。
    Content-Type: application/json;charset=UTF-8。纯 JSON dict 在含填空题场景曾返回 500。"""
    import base64 as _b64
    from urllib.parse import quote as _quote
    compact = json.dumps(submit_body, ensure_ascii=False, separators=(",", ":"))
    return _b64.b64encode(_quote(compact, safe="").encode("utf-8")).decode("ascii")


# ==================== 考试列表获取 ====================

def get_course_exams_list(client: ZjyClient, class_id: str, course_info_id: str,
                          course_id: str, ctype: str) -> list:
    """获取课程下所有考试/作业/测验列表。

    :param client: ZjyClient 实例
    :param class_id: 班级 ID
    :param course_info_id: 课程信息 ID
    :param course_id: 课程 ID
    :param ctype: 课程类型 "SPOC" / "MOOC" / "RESOURCE"
    :return: [{"id":"", "title":"", "type":"作业/考试/测验", "score":"-", "submit":False, ...}, ...]
    """
    try:
        if ctype == "RESOURCE":
            return _get_resource_exams(client, course_info_id, course_id)
        elif ctype == "MOOC":
            return _get_mooc_exams(client, course_info_id, course_id)
        else:
            return _get_spoc_exams(client, class_id, course_info_id, course_id)
    except Exception as e:
        log(f"[考试列表] 获取异常: {e}", "ERROR")
        return []


def _get_resource_exams(client: ZjyClient, course_info_id: str, course_id: str) -> list:
    """资源库域:调用 zyk_get_exam_list 获取作业/考试/测验列表。"""
    raw_exams = client.zyk_get_exam_list(course_info_id, course_id)
    result = []
    for e in raw_exams:
        exam_id = str(e.get("id") or e.get("examId") or "")
        if not exam_id:
            continue
        result.append({
            "id": exam_id,
            "examId": exam_id,
            "title": e.get("name") or e.get("examName") or "",
            "type": e.get("type") or _TYPE_MAP.get(str(e.get("categoryId", "")), "作业"),
            "score": str(e.get("score", "-")),
            "submit": bool(e.get("submit", False)),
            "categoryId": str(e.get("categoryId", "1")),
            "taskId": str(e.get("taskId", "") or ""),
        })
    log(f"[考试列表-RESOURCE] 共 {len(result)} 个任务", "INFO")
    return result


def _get_mooc_exams(client: ZjyClient, course_info_id: str, course_id: str) -> list:
    """MOOC 域:调用 getExamListByStudent 遍历 3 个 category。"""
    result = []
    for cat_id in ["1", "2", "3"]:
        page = 1
        while True:
            data = client.api_get_ai("course/exam/record/getExamListByStudent", {
                "courseInfoId": course_info_id,
                "courseId": course_id,
                "categoryId": cat_id,
                "pageNum": str(page),
                "pageSize": "20",
                "name": "",
                "submitStatus": "",
            })
            rows = client._extract_rows_loose(data) or client.extract_rows(data) or []
            if not rows:
                break
            for r in rows:
                exam_id = str(r.get("id") or r.get("examId") or "")
                if not exam_id:
                    continue
                score = r.get("score")
                has_score = score is not None and str(score) not in ("", "-", "None")
                submit_status = str(r.get("submitStatus") or r.get("status") or "")
                is_submit = submit_status in ("1", "2") or has_score
                result.append({
                    "id": exam_id,
                    "examId": exam_id,
                    "title": r.get("name") or r.get("examName") or r.get("title") or "",
                    "type": _TYPE_MAP.get(cat_id, "作业"),
                    "score": str(score) if has_score else "-",
                    "submit": is_submit,
                    "categoryId": cat_id,
                    "taskId": str(r.get("taskId", "") or ""),
                })
            # 翻页
            total = 0
            if data and isinstance(data, dict):
                try:
                    total = int(data.get("total", 0))
                except Exception:
                    total = 0
            if page * 20 >= total or len(rows) < 20:
                break
            page += 1
    log(f"[考试列表-MOOC] 共 {len(result)} 个任务", "INFO")
    return result


def _get_spoc_exams(client: ZjyClient, class_id: str, course_info_id: str,
                    course_id: str) -> list:
    """SPOC 域:并发调用 answeredExamList(3 个 category)+ classExam/student/list。"""
    all_items = []  # [(cat_id, row), ...]

    def _fetch_answered(cat_id: str):
        items = []
        try:
            data = client.api_get("spoc/exam/answeredExamList", {
                "classId": class_id,
                "courseInfoId": course_info_id,
                "courseId": course_id,
                "categoryId": cat_id,
                "pageNum": "1",
                "pageSize": "100",
            })
            rows = client._extract_rows_loose(data) or client.extract_rows(data) or []
            for r in rows:
                items.append((cat_id, r))
        except Exception as e:
            log(f"[考试列表-SPOC] answeredExamList(cat={cat_id}) 异常: {e}", "DEBUG")
        return items

    def _fetch_class_exam():
        items = []
        try:
            data = client.api_get("spoc/classExam/student/list", {
                "classId": class_id,
                "courseInfoId": course_info_id,
                "courseId": course_id,
                "pageNum": "1",
                "pageSize": "100",
            })
            rows = client._extract_rows_loose(data) or client.extract_rows(data) or []
            for r in rows:
                # classExam 默认为考试类型
                items.append(("2", r))
        except Exception as e:
            log(f"[考试列表-SPOC] classExam/student/list 异常: {e}", "DEBUG")
        return items

    def _fetch_homework():
        items = []
        try:
            data = client.api_get("spoc/homework/student/list", {
                "classId": class_id,
                "courseInfoId": course_info_id,
                "courseId": course_id,
                "pageNum": "1",
                "pageSize": "100",
            })
            rows = client._extract_rows_loose(data) or client.extract_rows(data) or []
            for r in rows:
                items.append(("1", r))
        except Exception:
            pass
        return items

    # 并发拉取
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_fetch_answered, "1"),
            executor.submit(_fetch_answered, "2"),
            executor.submit(_fetch_answered, "3"),
            executor.submit(_fetch_class_exam),
            executor.submit(_fetch_homework),
        ]
        for f in as_completed(futures):
            try:
                all_items.extend(f.result())
            except Exception:
                pass

    # 去重合并
    seen_ids = set()
    result = []
    for cat_id, r in all_items:
        exam_id = str(r.get("id") or r.get("examId") or "")
        if not exam_id or exam_id in seen_ids:
            continue
        seen_ids.add(exam_id)
        score = r.get("score")
        has_score = score is not None and str(score) not in ("", "-", "None")
        submit_status = str(r.get("submitStatus") or r.get("status") or "")
        is_submit = submit_status in ("1", "2") or has_score
        result.append({
            "id": exam_id,
            "examId": exam_id,
            "title": r.get("name") or r.get("examName") or r.get("title") or "",
            "type": _TYPE_MAP.get(cat_id, "作业"),
            "score": str(score) if has_score else "-",
            "submit": is_submit,
            "categoryId": cat_id,
            "taskId": str(r.get("taskId", "") or ""),
        })
    log(f"[考试列表-SPOC] 共 {len(result)} 个任务(去重后)", "INFO")
    return result


# ==================== 低分判断 ====================

def _is_low_score(exam: dict) -> bool:
    """判断是否需要答题:未提交或分数为 0 / 低分。

    :param exam: 考试 dict,含 submit / score 字段
    :return: True 表示需要答题
    """
    if not exam:
        return False
    # 未提交 → 需要答题
    if not exam.get("submit", False):
        return True
    # 分数为空 → 需要答题
    score = exam.get("score", "-")
    if score in ("-", "", None):
        return True
    # 分数无法解析 → 需要答题
    try:
        s = float(score)
    except (ValueError, TypeError):
        return True
    # 分数为 0 或低于阈值 → 需要答题
    if s <= 0:
        return True
    if s < _LOW_SCORE_THRESHOLD:
        return True
    return False


# ==================== 单个考试答题主流程 ====================

def do_auto_answer_single_exam(client: ZjyClient, nickname: str, exam_id: str,
                               class_id: str, course_info_id: str, course_id: str,
                               ctype: str, title: str, category_id: str,
                               teacher_token: str = "",
                               gate_wait: bool = False) -> Tuple[bool, str]:
    """单个考试/作业自动答题主流程。

    :param client: ZjyClient 实例
    :param nickname: 用户昵称(日志用)
    :param exam_id: 考试/作业 ID
    :param class_id: 班级 ID
    :param course_info_id: 课程信息 ID
    :param course_id: 课程 ID
    :param ctype: 课程类型 "SPOC" / "MOOC" / "RESOURCE"
    :param title: 考试标题
    :param category_id: "1"=作业 "2"=考试 "3"=测验
    :param teacher_token: 教师号 token(可选,默认空字符串)
    :param gate_wait: MOOC 重叠流水线模式——提交前真实等满时间闸门(仅 MOOC 生效)
    :return: (ok: bool, msg: str)
    """
    if not exam_id:
        return False, "缺少 exam_id"

    log(f"[{nickname}] 📝 开始答题: {title} (类型:{ctype}, categoryId:{category_id})", "INFO")

    try:
        if ctype == "RESOURCE":
            return _do_resource_answer(client, nickname, exam_id, course_info_id,
                                       course_id, category_id, title)
        else:
            return _do_spoc_mooc_answer(client, nickname, exam_id, class_id,
                                        course_info_id, course_id, ctype, title,
                                        category_id, teacher_token, gate_wait)
    except Exception as e:
        log(f"[{nickname}] 答题异常: {e}", "ERROR")
        return False, f"答题异常: {e}"


# ==================== 资源库(RESOURCE)答题 ====================

def _do_resource_answer(client: ZjyClient, nickname: str, exam_id: str,
                        course_info_id: str, course_id: str, category_id: str,
                        title: str) -> Tuple[bool, str]:
    """资源库答题:zyk_get_homework_answers 抓答案 → zyk_submit_exam 提交。"""
    # 1. 抓取答案
    questions = client.zyk_get_homework_answers(exam_id)
    if not questions:
        log(f"[{nickname}] ❌ {title}: 未获取到题目", "WARNING")
        return False, "未获取到题目"

    has_answer = sum(1 for q in questions if q.get("answer") or q.get("rawAnswer"))
    log(f"[{nickname}] 📋 {title}: 共 {len(questions)} 题,有答案 {has_answer} 题", "INFO")

    if has_answer == 0:
        return False, "所有题目均无答案,无法提交"

    # 2. 提交(资源库 cell_id 不参与提交体,传空字符串)
    result = client.zyk_submit_exam(
        exam_id, course_info_id, course_id, "", questions, category_id
    )

    if result and result.get("code") == 200:
        msg = result.get("msg", "提交成功")
        log(f"[{nickname}] ✅ {title}: {msg}", "SUCCESS")
        return True, msg

    err_msg = result.get("msg", "提交失败") if result else "提交请求失败"
    log(f"[{nickname}] ❌ {title}: {err_msg}", "WARNING")
    return False, err_msg


# ==================== SPOC / MOOC 答题 ====================

def _do_spoc_mooc_answer(client: ZjyClient, nickname: str, exam_id: str,
                         class_id: str, course_info_id: str, course_id: str,
                         ctype: str, title: str, category_id: str,
                         teacher_token: str, gate_wait: bool = False) -> Tuple[bool, str]:
    """SPOC/MOOC 答题:答案检索 → 教师号补充 → 题库兜底 → 构建payload → 提交。"""
    # 1. 答案检索(学生号直取 + 同学扫包)
    questions, record = client.find_classmate_answers(
        exam_id, class_id, course_info_id, course_id, ctype
    )
    if not questions:
        log(f"[{nickname}] ❌ {title}: 未获取到题目", "WARNING")
        return False, "未获取到题目"

    has_answer = sum(1 for q in questions if q.get("answer") or q.get("correctAnswer") or q.get("rightAnswer"))
    log(f"[{nickname}] 📋 {title}: 共 {len(questions)} 题,初始有答案 {has_answer} 题", "INFO")

    # 2. 教师号预览补充答案
    if teacher_token:
        stu_task_id = ""
        if record and isinstance(record, dict):
            stu_task_id = str(record.get("taskId") or record.get("id") or "")
        teacher_qs, _ = client.get_exam_preview_with_teacher(
            teacher_token, exam_id, class_id, course_info_id, course_id,
            stu_task_id=stu_task_id,
            stu_user_id=str(client.stu_id or ""),
        )
        if teacher_qs:
            _merge_teacher_answers(questions, teacher_qs)
            log(f"[{nickname}] 👨‍🏫 教师号补充答案完成", "DEBUG")

    # 3. 题库兜底
    bank_count = client.merge_question_bank_answers(questions, nickname)
    if bank_count > 0:
        log(f"[{nickname}] 📚 题库兜底补充 {bank_count} 题", "DEBUG")

    # 4. 检查最终答案覆盖率
    final_has_answer = sum(1 for q in questions if q.get("answer") or q.get("rawAnswer") or q.get("correctAnswer"))
    log(f"[{nickname}] 📊 {title}: 最终有答案 {final_has_answer}/{len(questions)} 题", "INFO")

    if final_has_answer == 0:
        return False, "所有题目均无答案,无法提交"

    # 5. 计算答题时长(对齐商业版)
    valid_count = final_has_answer
    base_time = valid_count * random.randint(15, 40)
    exam_time = max(base_time, random.randint(30, 120))
    # MOOC: 考试最少1200秒(20分钟),测验最少300秒(5分钟),作业最少60秒,上限1800秒
    # SPOC: 考试/测验/作业有不同最小值,但SPOC考试需真实等待,限制最大180秒避免阻塞
    if ctype == "MOOC":
        min_time = 1200 if str(category_id) == "2" else (300 if str(category_id) == "3" else 60)
        exam_time = max(min_time, min(exam_time, 1800))
    else:
        if str(category_id) == "2":
            min_time = random.randint(900, 1200)
        elif str(category_id) == "3":
            min_time = random.randint(300, 500)
        else:
            min_time = random.randint(60, 180)
        # SPOC考试需真实等待,限制最大180秒
        if str(category_id) == "2":
            exam_time = min(exam_time, 180)
        else:
            exam_time = max(min_time, min(exam_time, random.randint(1600, 1900)))

    task_id = ""
    if record and isinstance(record, dict):
        task_id = str(record.get("taskId") or record.get("id") or "")

    # 6. 提交
    if ctype == "MOOC":
        ok, result = _submit_mooc_exam(client, nickname, exam_id, class_id,
                                        course_info_id, course_id, category_id,
                                        task_id, questions, exam_time, title,
                                        gate_wait=gate_wait)
    else:
        result = _submit_spoc_exam(client, nickname, exam_id, class_id,
                                   course_info_id, course_id, category_id,
                                   task_id, questions, exam_time, title)
        ok = bool(result and result.get("code") == 200)

    if ok:
        msg = (result or {}).get("msg", "提交成功")
        log(f"[{nickname}] ✅ {title}: {msg} (有答案 {final_has_answer}/{len(questions)} 题)", "SUCCESS")
        return True, msg

    err_msg = (result or {}).get("msg", "提交失败") if result else "提交请求失败"
    log(f"[{nickname}] ❌ {title}: {err_msg}", "WARNING")
    return False, err_msg


# ==================== MOOC 提交 ====================

def _submit_mooc_exam(client: ZjyClient, nickname: str, exam_id: str,
                      class_id: str, course_info_id: str, course_id: str,
                      category_id: str, task_id: str, questions: list,
                      exam_time: int, title: str = "",
                      gate_wait: bool = False) -> Tuple[bool, Optional[dict]]:
    """MOOC 提交:updateExamTime 累加时长 → (gate_wait 真实等满闸门) → POST course/exam/record。

    对齐生产版(2026-09-14/16):
    - 提交体 Base64(URLEncode(紧凑JSON)) 编码(纯 JSON dict 含填空题曾返回 500)
    - gate_wait=True:提交前真实等满 now−createTime 时间闸门(updateExamTime 叠加值骗不过,实验证)
    - 205「作答次数上限」→ 直接跳过不 delete 重试(重试白白重置计时且注定再败),msg 透传调用方分流
    - 其余失败 → 删除旧记录重新起表再提交(重建已把 createTime 归零,gate_wait 模式须对新记录再等满)
    返回 (ok, result_or_None)。
    """
    # 1. updateExamTime 累加作答时长(查已累加值只补差值、熔断、重新认证)
    _mooc_update_exam_time(client, exam_id, course_info_id, course_id, exam_time, task_id, nickname)

    # 2. 构建 taskExamProblemRecordList(仅 questionNo/paperId/answer,无 optionSort)
    records = []
    for i, q in enumerate(questions):
        type_id = _get_type_id(q)
        paper_id = str(q.get("paperId") or q.get("id") or q.get("questionId") or "")
        answer = _convert_answer_for_submit(q, type_id)
        # 从 dataJson 的 IsAnswer 提取答案(答案为空时)
        if not answer:
            dj = q.get("dataJson")
            if dj and type_id in ("1", "2"):
                try:
                    dj_list = json.loads(dj) if isinstance(dj, str) else dj
                    if isinstance(dj_list, list):
                        ans_indices = [str(opt.get("SortOrder", "")) for opt in dj_list
                                       if str(opt.get("IsAnswer", "")).lower() == "true"]
                        if ans_indices:
                            answer = ",".join(ans_indices)
                except Exception:
                    pass
        # MOOC 填空题转 JSON 数组
        if type_id == "4" and answer:
            try:
                dj = q.get("dataJson")
                if dj:
                    dj_list = json.loads(dj) if isinstance(dj, str) else dj
                    if isinstance(dj_list, list):
                        blanks = [opt.get("Content", "") for opt in dj_list]
                        answer = json.dumps(blanks, ensure_ascii=False)
            except Exception:
                pass
        records.append({"questionNo": i, "paperId": paper_id, "answer": str(answer)})

    # 3. 构建提交 payload
    payload = {
        "courseId": course_id,
        "courseInfoId": course_info_id,
        "examId": exam_id,
        "examName": title,
        "device": 2,
        "categoryId": str(category_id),
        "examTime": exam_time,
        "groupId": 0,
        "isLast": True,
        "id": task_id or "",
        "taskExamProblemRecordList": records,
    }

    log(f"[{nickname}] [MOOC提交] {len(records)} 题, examTime={exam_time}s", "DEBUG")

    # 刷新 AI Token
    client.auth_ai_domain()

    # 提交(对齐官方 web 端:Body=Base64(URLEncode(紧凑JSON));401 自动重认证重试一次)
    mooc_fail_msg = ""

    def _do_submit(submit_body):
        nonlocal mooc_fail_msg
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        ai_tok = client.ai_token
        if ai_tok:
            headers["Authorization"] = ai_tok if ai_tok.startswith("Bearer ") else f"Bearer {ai_tok}"
        encoded = _encode_mooc_body(submit_body)
        try:
            resp = client.session.post("https://ai.icve.com.cn/prod-api/course/exam/record",
                                       data=encoded, headers=headers, timeout=30)
            if resp.status_code == 401:
                client.auth_ai_domain()
                ai_tok2 = client.ai_token
                if ai_tok2:
                    headers["Authorization"] = ai_tok2 if ai_tok2.startswith("Bearer ") else f"Bearer {ai_tok2}"
                resp = client.session.post("https://ai.icve.com.cn/prod-api/course/exam/record",
                                           data=encoded, headers=headers, timeout=30)
            if resp.status_code == 200:
                j = resp.json()
                if j.get("code") == 200:
                    return True, j
                if j.get("code") == 205 and j.get("msg"):
                    mooc_fail_msg = str(j.get("msg"))
                return False, j
        except Exception as e:
            log(f"[{nickname}] [MOOC提交] 异常: {e}", "ERROR")
        return False, None

    # 时间闸门真实等待(仅 gate_wait 的主动答题入口;放在 updateExamTime 之后,
    # 累加循环已消耗的 wall-clock 一并计入)
    if gate_wait:
        _mooc_gate_wait(client, exam_id, nickname)

    submitted, result = _do_submit(payload)
    if submitted:
        return True, result

    # 205「作答次数上限」:等多久/重试都没用(平台硬拒),跳过 delete+重试
    # (重试会白白重置计时),由调用方按 msg 分流不记失败。
    if mooc_exam_is_exhausted(mooc_fail_msg):
        log(f"[{nickname}] ⏭ {title}:{mooc_fail_msg},跳过重试(等待/重交均无效)", "INFO")
        return False, {"code": 205, "msg": mooc_fail_msg}

    # 其余失败:删除旧记录重新起表再提交
    log(f"[{nickname}] [MOOC提交] 首次提交失败,尝试删除旧记录后重试...", "WARNING")
    try:
        client.api_post_ai("course/exam/record/delete", {
            "courseId": course_id, "courseInfoId": course_info_id,
            "examId": exam_id, "groupId": 0,
        })
    except Exception:
        pass
    new_paper_data = client.api_get_ai("course/exam/paper", {"id": exam_id, "groupId": "0"})
    if new_paper_data and new_paper_data.get("questions"):
        new_record = new_paper_data.get("taskExamRecord") or {}
        new_rec_id = new_record.get("id", "")
        if new_rec_id:
            new_body = dict(payload)
            new_body["id"] = new_rec_id
            # 新记录计时归零,需完整重新累加
            _mooc_update_exam_time(client, exam_id, course_info_id, course_id, exam_time, new_rec_id, nickname)
            # delete+重建已把 createTime 归零,gate_wait 模式必须对新记录再等满门槛
            if gate_wait:
                _mooc_gate_wait(client, exam_id, nickname)
            client.auth_ai_domain()
            submitted, result = _do_submit(new_body)
            if submitted:
                return True, result

    return False, result


def _mooc_update_exam_time(client: ZjyClient, exam_id: str, course_info_id: str,
                           course_id: str, exam_time: int, task_id: str, nickname: str) -> None:
    """MOOC 累加作答时长(对齐商业版)。

    - 查询当前记录已累加的 examTime,只补差值
    - 每 10 秒一次,有熔断保护(连续5次失败停止)
    - 首次失败时重新认证 AI 域名
    - 每次间隔 1 秒
    """
    # 查询当前已累加的作答时间
    current_exam_time = 0
    try:
        paper_data = client.api_get_ai("course/exam/paper", {"id": exam_id, "groupId": "0"})
        if paper_data and isinstance(paper_data, dict):
            paper_record = paper_data.get("taskExamRecord") or {}
            current_exam_time = int(paper_record.get("examTime") or 0)
    except Exception:
        pass

    interval = 10
    need_add = max(interval, exam_time - current_exam_time)
    # 反指纹(2026-09-16 生产移植):步进不再恒 10(恒 10 → 平台记录几乎全是 10 的倍数)。
    # 每轮 rand(8,13),rounds 按期望均值 10.5 折算,总时长仍≈exam_time;至少调 1 次保持活跃。
    _STEP_MIN, _STEP_MAX = 8, 13
    rounds = max(1, int(round(need_add / ((_STEP_MIN + _STEP_MAX) / 2))))

    update_payload = {
        "courseId": course_id, "courseInfoId": course_info_id,
        "examId": exam_id, "examTime": interval,
        "groupId": 0, "taskId": task_id,
    }

    success_count = 0
    fail_streak = 0
    _accum_s = 0  # 实际累计秒数(随机步进)
    for r in range(rounds):
        try:
            _step = random.randint(_STEP_MIN, _STEP_MAX)
            update_payload["examTime"] = _step
            result = client.api_post_ai("course/exam/record/updateExamTime", update_payload)
            if result and result.get("code") == 200:
                success_count += 1
                _accum_s += _step
                fail_streak = 0
            elif r == 0 and (not result or result.get("code") != 200):
                # 首次失败,重新认证 AI 域名重试
                log(f"[{nickname}] [MOOC] updateExamTime首次失败,重新认证AI域名...", "WARNING")
                client.auth_ai_domain()
                result = client.api_post_ai("course/exam/record/updateExamTime", update_payload)
                if result and result.get("code") == 200:
                    success_count += 1
                    _accum_s += _step
                    fail_streak = 0
                else:
                    fail_streak += 1
            else:
                fail_streak += 1
        except Exception:
            fail_streak += 1

        if fail_streak >= 5:
            log(f"[{nickname}] [MOOC] updateExamTime连续5次失败,熔断停止 (已完成{success_count}/{rounds})", "WARNING")
            break
        if r < rounds - 1:
            time.sleep(1)

    log(f"[{nickname}] [MOOC] updateExamTime: {success_count}/{rounds}次成功, 新增约{_accum_s}秒 (已有{current_exam_time}秒)", "INFO")


# ==================== SPOC 提交 ====================

def _submit_spoc_exam(client: ZjyClient, nickname: str, exam_id: str,
                      class_id: str, course_info_id: str, course_id: str,
                      category_id: str, task_id: str, questions: list,
                      exam_time: int, title: str) -> Optional[dict]:
    """SPOC 提交:完整选项乱序映射 + 删除旧记录 + 失败重试 + updateExamTime"""
    import re as _re

    # 1. 构建 answer_list(旧格式降级用)
    answer_list = []
    for q in questions:
        question_id = q.get("id") or q.get("questionId")
        type_id = str(q.get("typeId", ""))
        do_answer = _convert_answer_for_submit(q, type_id)
        item = {"questionId": question_id, "doAnswer": do_answer, "typeId": type_id}
        if q.get("score"):
            item["score"] = q.get("score")
        answer_list.append(item)

    # 2. 获取学生答卷结构与 taskId
    record_id_for_submit = None
    student_questions = []
    sign_id_for_submit = None
    try:
        stu_detail = client.api_post("spoc/file/exam/detail/with/student", {
            "examId": exam_id, "classId": class_id, "device": "2", "groupId": "0"
        })
        if stu_detail and stu_detail.get("code") == 200 and stu_detail.get("data"):
            d = stu_detail["data"]
            record_id_for_submit = d.get("taskId") or d.get("id") or d.get("recordId")
            student_questions = d.get("answerSheets") or []
            sign_id_for_submit = d.get("signId")
            if record_id_for_submit:
                log(f"[{nickname}] SPOC file/exam/detail/with/student成功, taskId={record_id_for_submit}, 试卷题目数量={len(student_questions)}", "INFO")
    except Exception as e:
        log(f"[{nickname}] SPOC file/exam/detail/with/student异常: {e}", "WARNING")

    # 降级: exam/enter
    if not record_id_for_submit:
        try:
            enter_data = client.api_get("spoc/exam/enter", {
                "examId": exam_id, "classId": class_id,
                "courseInfoId": course_info_id, "courseId": course_id,
            })
            if enter_data and enter_data.get("code") == 200 and enter_data.get("data"):
                d = enter_data["data"]
                record_id_for_submit = d.get("id") or d.get("recordId") or d.get("taskId")
                if record_id_for_submit:
                    log(f"[{nickname}] SPOC exam/enter成功, recordId={record_id_for_submit}", "INFO")
        except Exception as e:
            log(f"[{nickname}] SPOC exam/enter异常: {e}", "WARNING")

    # 3. 删除旧提交记录
    try:
        rec_data = client.api_get("spoc/exam/record/list", {
            "examId": exam_id, "classId": class_id, "pageNum": "1", "pageSize": "10",
        })
        if rec_data and rec_data.get("code") == 200 and rec_data.get("data"):
            rows = rec_data["data"].get("rows") or rec_data["data"]
            if isinstance(rows, list):
                for row in rows:
                    rid = row.get("id")
                    if rid:
                        del_res = client.api_delete("spoc/exam/record", {"id": str(rid)})
                        if del_res and del_res.get("code") == 200:
                            log(f"[{nickname}] SPOC删除旧记录成功(id={rid})", "INFO")
    except Exception as e:
        log(f"[{nickname}] SPOC删除旧记录异常: {e}", "WARNING")

    # 4. 构建答案映射(题库答案)
    classmate_answers_map = {}
    classmate_titles_map = {}
    for q_item in questions:
        qid = q_item.get("questionId") or q_item.get("id")
        if qid:
            classmate_answers_map[str(qid)] = q_item
        title = q_item.get("title") or q_item.get("examName") or q_item.get("examContent")
        if title:
            clean_title = _re.sub(r'<[^>]+>', '', str(title)).strip()
            if clean_title:
                classmate_titles_map[clean_title] = q_item

    # 5. 选择目标题目列表(优先学生答卷)
    if student_questions:
        target_questions = student_questions
        use_student_sheets = True
    else:
        target_questions = questions
        use_student_sheets = False

    # 6. 构建 spoc_record_list(选项乱序映射)
    spoc_record_list = []
    for i, q in enumerate(target_questions):
        type_id = str(q.get("typeId", ""))

        if use_student_sheets:
            q_id = q.get("id")
            global_qid = q.get("questionId")
            classmate_q = classmate_answers_map.get(str(global_qid))
            if not classmate_q:
                q_title = q.get("title") or q.get("examName") or q.get("examContent") or ""
                if q_title:
                    clean_q_title = _re.sub(r'<[^>]+>', '', str(q_title)).strip()
                    classmate_q = classmate_titles_map.get(clean_q_title)
            raw_answer = ""
            if classmate_q:
                raw_answer = (classmate_q.get("answer") or classmate_q.get("correctAnswer")
                              or classmate_q.get("rightAnswer") or classmate_q.get("recordAnswer")
                              or classmate_q.get("stuAnswer") or "")
        else:
            q_id = q.get("id") or q.get("questionId") or ""
            global_qid = q_id
            classmate_q = q
            raw_answer = (q.get("answer") or q.get("correctAnswer") or q.get("rightAnswer")
                          or q.get("recordAnswer") or q.get("stuAnswer") or "")

        # 选项乱序映射
        option_sort_str = ""
        student_data_json = q.get("dataJson") or q.get("optionSort")
        student_opts = []
        if student_data_json:
            try:
                student_opts = json.loads(student_data_json) if isinstance(student_data_json, str) else student_data_json
            except:
                pass

        if type_id in ["1", "2"]:
            # 选择题
            final_answer = str(raw_answer) if raw_answer else ""
            if isinstance(student_opts, list):
                classmate_opts = []
                if classmate_q:
                    cq_data = classmate_q.get("dataJson") or classmate_q.get("optionSort")
                    if cq_data:
                        try:
                            classmate_opts = json.loads(cq_data) if isinstance(cq_data, str) else cq_data
                        except:
                            pass
                full_opts = []
                ans_sos_set = set(x.strip() for x in final_answer.split(",") if x.strip())
                for idx, opt in enumerate(student_opts):
                    so = str(opt.get("SortOrder", ""))
                    content = opt.get("Content", "")
                    if not content and classmate_opts and idx < len(classmate_opts):
                        content = classmate_opts[idx].get("Content", "")
                    opt_item = {"Content": content, "IsAnswer": so in ans_sos_set, "SortOrder": so}
                    opt_item["name"] = opt.get("name") or (chr(65 + idx) if idx < 26 else str(idx))
                    full_opts.append(opt_item)
                option_sort_str = json.dumps(full_opts, ensure_ascii=False)
            record_item = {"questionNo": i, "paperId": q_id, "optionSort": option_sort_str, "answer": final_answer}

        elif type_id == "3":
            # 判断题
            if isinstance(student_opts, list) and len(student_opts) >= 2:
                classmate_opts = []
                if classmate_q:
                    cq_data = classmate_q.get("dataJson") or classmate_q.get("optionSort")
                    if cq_data:
                        try:
                            classmate_opts = json.loads(cq_data) if isinstance(cq_data, str) else cq_data
                        except:
                            pass
                full_opts = []
                ans_val = str(raw_answer).strip()
                for idx, opt in enumerate(student_opts):
                    so = str(opt.get("SortOrder", ""))
                    content = opt.get("Content", "")
                    if not content and classmate_opts and idx < len(classmate_opts):
                        content = classmate_opts[idx].get("Content", "")
                    is_ans = so == ans_val or (ans_val == "1" and "正确" in str(content)) or (ans_val == "0" and "错误" in str(content))
                    full_opts.append({"Content": content, "IsAnswer": is_ans, "SortOrder": so, "name": opt.get("name", "")})
                option_sort_str = json.dumps(full_opts, ensure_ascii=False)
            else:
                option_sort_str = json.dumps([
                    {"label": "1", "SortOrder": "A", "Content": "正确"},
                    {"label": "0", "SortOrder": "B", "Content": "错误"},
                ], ensure_ascii=False)
            record_item = {"questionNo": i, "paperId": q_id, "optionSort": option_sort_str, "answer": str(raw_answer)}

        elif type_id in ["4", "5"]:
            # 填空题/简答题
            ans_list = []
            if raw_answer:
                try:
                    parsed = json.loads(str(raw_answer))
                    if isinstance(parsed, list):
                        ans_list = [str(x) for x in parsed]
                except:
                    pass
            if not ans_list and type_id == "4" and classmate_q:
                cq_data = classmate_q.get("dataJson") or classmate_q.get("optionSort")
                if cq_data:
                    try:
                        cq_opts = json.loads(cq_data) if isinstance(cq_data, str) else cq_data
                        if isinstance(cq_opts, list):
                            extracted = [str(opt.get("Content", "")).strip() for opt in cq_opts]
                            if any(extracted):
                                ans_list = extracted
                    except:
                        pass
            if not ans_list and raw_answer:
                if "," in str(raw_answer):
                    ans_list = [str(x).strip() for x in str(raw_answer).split(",")]
                elif "，" in str(raw_answer):
                    ans_list = [str(x).strip() for x in str(raw_answer).split("，")]
                else:
                    ans_list = [str(raw_answer)]
            if not ans_list:
                ans_list = [""]
            record_item = {"questionNo": i, "paperId": q_id, "answer": json.dumps(ans_list, ensure_ascii=False)}

        else:
            record_item = {"questionNo": i, "paperId": q_id, "answer": str(raw_answer)}
            if option_sort_str:
                record_item["optionSort"] = option_sort_str

        spoc_record_list.append(record_item)

    # 7. 构建 payload
    spoc_submit_body = {
        "categoryId": int(category_id) if str(category_id).isdigit() else 1,
        "courseId": course_id, "courseInfoId": course_info_id, "examId": exam_id,
        "examTime": exam_time, "groupId": "0", "isLast": True, "status": "",
        "taskExamProblemRecordList": spoc_record_list,
        "updateBy": "", "updateTime": "", "userId": "",
        "classId": class_id, "resitId": "", "device": "1",
    }
    if record_id_for_submit:
        spoc_submit_body["id"] = record_id_for_submit

    # 8. SPOC考试真实等待
    submitted = False
    record_id = None
    if str(category_id) == "2" and exam_time > 0:
        log(f"[{nickname}] SPOC考试真实等待 {exam_time} 秒 ({exam_time//60}分{exam_time%60}秒),请勿关闭...", "INFO")
        time.sleep(exam_time)
        log(f"[{nickname}] SPOC考试等待完成,开始提交", "INFO")

    # 9. 提交
    log(f"[{nickname}] SPOC提交payload: isLast={spoc_submit_body['isLast']}, id={spoc_submit_body.get('id', 'N/A')}", "INFO")
    res = client.api_post("spoc/exam/record", spoc_submit_body, timeout=300)
    if res and res.get("code") == 200:
        submitted = True
        if res.get("data"):
            d = res["data"]
            if isinstance(d, dict):
                record_id = d.get("id") or d.get("recordId") or d.get("examRecordId")
            elif isinstance(d, str):
                record_id = d
        log(f"[{nickname}] SPOC提交成功({len(spoc_record_list)}题)", "INFO")
    else:
        msg = res.get("msg", "") if res else "请求失败"
        code = res.get("code", "") if res else ""
        log(f"[{nickname}] SPOC提交失败: code={code}, msg={msg}", "WARNING")

        # 失败重试:去掉 id 字段(首次提交模式)
        if use_student_sheets:
            log(f"[{nickname}] 去掉题目id字段重试(首次提交模式)...", "WARNING")
            clean_list = [{k: v for k, v in item.items() if k not in ("id", "knowledgePointsId")} for item in spoc_record_list]
            clean_body = dict(spoc_submit_body)
            clean_body["taskExamProblemRecordList"] = clean_list
            res2 = client.api_post("spoc/exam/record", clean_body, timeout=300)
            if res2 and res2.get("code") == 200:
                submitted = True
                if res2.get("data"):
                    d2 = res2["data"]
                    if isinstance(d2, dict):
                        record_id = d2.get("id") or d2.get("recordId") or d2.get("examRecordId")
                    elif isinstance(d2, str):
                        record_id = d2
                log(f"[{nickname}] SPOC首次提交成功({len(clean_list)}题)", "INFO")
            else:
                msg2 = res2.get("msg", "") if res2 else "请求失败"
                code2 = res2.get("code", "") if res2 else ""
                log(f"[{nickname}] SPOC首次提交也失败: code={code2}, msg={msg2}", "WARNING")

    # 10. 旧格式降级
    if not submitted:
        log(f"[{nickname}] SPOC抓包格式提交失败,尝试旧格式...", "WARNING")
        submit_apis = [
            "spoc/exam/record", "spoc/exam/record/submit", "spoc/exam/record/submitExam",
            "spoc/homeWork/submit", "spoc/classExam/student/submit"
        ]
        submit_payloads = [
            {"examId": exam_id, "classId": class_id, "courseInfoId": course_info_id, "courseId": course_id,
             "examQuestionList": answer_list, "examTime": exam_time},
            {"examId": exam_id, "classId": class_id, "courseInfoId": course_info_id, "courseId": course_id,
             "answerList": answer_list, "examTime": exam_time}
        ]
        for api_path in submit_apis:
            for p in submit_payloads:
                res = client.api_post(api_path, p)
                if res:
                    code = res.get("code")
                    msg = str(res.get("msg", ""))
                    if code == 200:
                        submitted = True
                        break
                    elif "重复" in msg or "已提交" in msg:
                        submitted = True
                        break
                    elif "参数" in msg or "格式" in msg or "不能为空" in msg:
                        continue
                    else:
                        log(f"[{nickname}] SPOC旧格式 {api_path} 失败: code={code}, msg={msg}", "WARNING")
                        continue
            if submitted:
                break

    # 11. 提交成功后 updateExamTime 累加时长
    if submitted:
        if not record_id:
            try:
                rec_data2 = client.api_get("spoc/exam/record/list", {
                    "examId": exam_id, "classId": class_id, "pageNum": "1", "pageSize": "10",
                })
                if rec_data2 and rec_data2.get("code") == 200 and rec_data2.get("data"):
                    rows2 = rec_data2["data"] if isinstance(rec_data2["data"], list) else rec_data2["data"].get("rows", [])
                    if rows2:
                        record_id = rows2[0].get("id") or rows2[0].get("recordId")
            except:
                pass

        interval = 10
        rounds = exam_time // interval
        spoc_update_payload = {
            "classId": class_id, "courseInfoId": course_info_id, "courseId": course_id,
            "examTime": interval, "groupId": 0,
            "taskId": record_id_for_submit or record_id or "",
        }
        if record_id:
            spoc_update_payload["id"] = record_id

        spoc_success = 0
        spoc_fail_streak = 0
        for r in range(rounds):
            try:
                result = client.api_post("spoc/exam/record/updateExamTime", spoc_update_payload)
                if result and result.get("code") == 200:
                    spoc_success += 1
                    spoc_fail_streak = 0
            except:
                spoc_fail_streak += 1
            if spoc_fail_streak >= 5:
                log(f"[{nickname}] SPOC updateExamTime连续5次失败,熔断停止 (已完成{spoc_success}/{rounds})", "WARNING")
                break
            if r < rounds - 1:
                time.sleep(0.05)
        if spoc_success > 0:
            log(f"[{nickname}] SPOC updateExamTime: {spoc_success}/{rounds}次成功, 累计约{spoc_success * interval}秒", "INFO")

    return {"code": 200, "msg": "提交成功"} if submitted else {"code": 500, "msg": "提交失败"}


# ==================== 提交记录构建 ====================

def _build_submit_records(questions: list) -> list:
    """构建 taskExamProblemRecordList,每题含 questionNo/paperId/answer/optionSort。

    :param questions: 题目列表
    :return: 提交记录列表
    """
    records = []
    for idx, q in enumerate(questions):
        type_id = _get_type_id(q)
        paper_id = str(q.get("paperId") or q.get("id") or q.get("questionId") or "")
        answer = _convert_answer_for_submit(q, type_id)

        record = {
            "questionNo": idx,
            "paperId": paper_id,
            "answer": answer,
            "optionSort": "",
        }

        # optionSort: 仅选择题(typeId=1/2)需要,从 dataJson 或 optionSort 字段提取
        if type_id in ("1", "2") and answer:
            opt_sort = q.get("optionSort") or q.get("dataJson") or ""
            if opt_sort:
                if isinstance(opt_sort, str):
                    record["optionSort"] = opt_sort
                else:
                    record["optionSort"] = json.dumps(opt_sort, ensure_ascii=False)

        records.append(record)
    return records


# ==================== 答案格式转换 ====================

def _convert_answer_for_submit(q: dict, type_id: str) -> str:
    """转换答案格式用于提交。

    - 选择题(typeId=1/2): SortOrder 数字如"0,1" → 选项名
    - 判断题(typeId=3): "1"(正确)/"0"(错误)
    - 填空题(typeId=4): JSON 数组
    - 问答题(typeId=5/6): 文本
    """
    ans = (q.get("answer") or q.get("rawAnswer") or q.get("correctAnswer")
           or q.get("rightAnswer") or "")
    if not ans:
        return ""

    if type_id in ("1", "2"):
        return _convert_choice_answer(ans, q.get("dataJson"))
    elif type_id == "3":
        return _convert_judgment_answer(ans)
    elif type_id == "4":
        return _convert_blank_answer(ans)
    elif type_id == "7":
        # 客观填空:同填空题
        return _convert_blank_answer(ans)
    else:
        # 问答题(5/6)及其他:文本
        return str(ans)


def _convert_choice_answer(ans, data_json) -> str:
    """选择题答案转换:SortOrder 索引"0,1" → 选项名(SortOrder 字段值)。

    若答案已是数字索引,尝试从 dataJson 映射到 SortOrder 字段值;
    若答案是字母(A,B),先转为索引再映射。
    """
    ans_str = str(ans).strip()
    if not ans_str:
        return ""

    parts = [p.strip() for p in ans_str.split(",") if p.strip()]
    if not parts:
        return ans_str

    # 字母格式(A,B,C...) → 索引
    letter_map = {"A": "0", "B": "1", "C": "2", "D": "3", "E": "4", "F": "5", "G": "6", "H": "7"}
    if all(p.upper() in letter_map for p in parts):
        parts = [letter_map[p.upper()] for p in parts]

    # 数字索引 → SortOrder 字段值
    if all(p.isdigit() for p in parts) and data_json:
        try:
            dj_list = json.loads(data_json) if isinstance(data_json, str) else data_json
            if isinstance(dj_list, list):
                sort_orders = []
                for p in parts:
                    idx = int(p)
                    if 0 <= idx < len(dj_list):
                        opt = dj_list[idx]
                        so = opt.get("SortOrder")
                        if so is not None:
                            sort_orders.append(str(so))
                        else:
                            sort_orders.append(p)
                    else:
                        sort_orders.append(p)
                if sort_orders:
                    return ",".join(sort_orders)
        except Exception:
            pass

    return ",".join(parts)


def _convert_judgment_answer(ans) -> str:
    """判断题答案转换:统一为"1"(正确)/"0"(错误)。"""
    ans_str = str(ans).strip().lower()
    if ans_str in ("1", "true", "正确", "对", "a", "是"):
        return "1"
    if ans_str in ("0", "false", "错误", "错", "b", "否"):
        return "0"
    return str(ans)


def _convert_blank_answer(ans) -> str:
    """填空题答案转换:转为 JSON 数组格式。"""
    if isinstance(ans, list):
        return json.dumps([str(x) for x in ans], ensure_ascii=False)
    if isinstance(ans, str):
        ans_str = ans.strip()
        if ans_str.startswith("["):
            # 已是 JSON 数组,验证格式
            try:
                arr = json.loads(ans_str)
                if isinstance(arr, list):
                    return json.dumps(arr, ensure_ascii=False)
            except Exception:
                pass
        # 按 ; 或 ； 分割为多个空
        parts = [p.strip() for p in ans_str.replace("；", ";").split(";") if p.strip()]
        if not parts:
            parts = [ans_str]
        return json.dumps(parts, ensure_ascii=False)
    return json.dumps([str(ans)], ensure_ascii=False)


# ==================== 教师号答案合并 ====================

def _merge_teacher_answers(questions: list, teacher_qs: list) -> None:
    """将教师号获取的答案合并到题目列表中(仅补充无答案的题目)。"""
    if not questions or not teacher_qs:
        return

    # 构建教师号题目映射
    t_map = {}
    for tq in teacher_qs:
        qid = str(tq.get("questionId") or tq.get("id") or tq.get("paperId") or "")
        if qid:
            t_map[qid] = tq

    merged = 0
    for q in questions:
        existing = q.get("answer") or q.get("correctAnswer") or q.get("rightAnswer")
        if existing:
            continue
        qid = str(q.get("questionId") or q.get("id") or q.get("paperId") or "")
        tq = t_map.get(qid)
        if not tq:
            continue
        ans = tq.get("answer") or tq.get("correctAnswer") or tq.get("rightAnswer")
        if ans:
            q["answer"] = ans
            q["correctAnswer"] = ans
            # 同步 dataJson(用于 optionSort)
            dj = tq.get("dataJson")
            if dj and not q.get("dataJson"):
                q["dataJson"] = dj
            merged += 1

    if merged > 0:
        log(f"[教师号] 补充 {merged}/{len(questions)} 题答案", "DEBUG")


# ==================== 题型识别 ====================

def _get_type_id(q: dict) -> str:
    """获取题目 typeId,兼容多种字段名;typeId 缺失时从 typeName 推断。

    :return: "1"=单选 "2"=多选 "3"=判断 "4"=填空 "5"=问答 "6"=论述 "7"=客观填空
    """
    type_id = str(q.get("typeId") or q.get("type") or "")
    if type_id:
        return type_id
    type_name = str(q.get("typeName") or q.get("questionTypeName") or "").lower()
    if "单选" in type_name:
        return "1"
    if "多选" in type_name:
        return "2"
    if "判断" in type_name:
        return "3"
    if "客观填空" in type_name:
        return "7"
    if "填空" in type_name:
        return "4"
    if "问答" in type_name or "简答" in type_name:
        return "5"
    if "论述" in type_name:
        return "6"
    return ""


# ==================== 批量答题 ====================

def run_auto_answer_all_task(client: ZjyClient, nickname: str, class_id: str,
                             course_info_id: str, course_id: str, ctype: str,
                             teacher_token: str = "") -> None:
    """批量答题:获取考试列表 → 过滤未提交/低分 → 逐一自动答题。

    :param client: ZjyClient 实例
    :param nickname: 用户昵称(日志用)
    :param class_id: 班级 ID
    :param course_info_id: 课程信息 ID
    :param course_id: 课程 ID
    :param ctype: 课程类型 "SPOC" / "MOOC" / "RESOURCE"
    :param teacher_token: 教师号 token(可选)
    """
    log(f"[{nickname}] 🚀 启动批量自动答题 (类型:{ctype})...", "INFO")

    try:
        exams = get_course_exams_list(client, class_id, course_info_id, course_id, ctype)
        unsubmitted = [e for e in exams if _is_low_score(e)]

        if not unsubmitted:
            log(f"[{nickname}] 没有发现未提交或低分的作业/考试/测验", "INFO")
            return

        log(f"[{nickname}] 发现 {len(unsubmitted)} 个待答题任务,开始逐一答题...", "INFO")
        success = 0
        fail = 0
        for idx, exam in enumerate(unsubmitted, 1):
            exam_id = exam.get("id") or exam.get("examId")
            title = exam.get("title", "未命名任务")
            etype = exam.get("type", "")
            category_id = str(exam.get("categoryId") or "")
            if not category_id:
                category_id = "2" if etype == "考试" else ("3" if etype == "测验" else "1")

            log(f"[{nickname}] [{idx}/{len(unsubmitted)}] 处理: {title} ({etype})", "INFO")

            ok, msg = do_auto_answer_single_exam(
                client, nickname, exam_id, class_id, course_info_id, course_id,
                ctype, title, category_id, teacher_token
            )
            if ok:
                success += 1
            else:
                fail += 1
            # 任务间隔
            time.sleep(1)

        log(f"[{nickname}] 🎉 批量答题结束:成功 {success} 个,失败 {fail} 个", "INFO")
    except Exception as e:
        log(f"[{nickname}] 批量答题异常: {e}", "ERROR")
