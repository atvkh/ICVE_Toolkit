# -*- coding: utf-8 -*-
"""MOOC 键面形态自检(离线,零网络、零凭据):验证 typeId=7 匹配题与 typeId=11 综合题的提交形态。

跑法:
    python selfcheck_mooc_shapes.py

四类断言:
  A 外呼纪律    —— 卷内不含 7/11 时,一次键面请求都不发。
  B 等价性      —— 不含 7/11 的卷子,提交体与**改动前**的实现逐字段相同(需 git 环境,否则跳过并说明)。
  C 匹配题形态  —— typeId=7 发键面候选池下标的**逗号串**;键面取不到时发空串,**绝不发候选词池**。
  D 综合题形态  —— typeId=11 父行不发,逐子题发 {questionNo:"[父,子]", paperId, paperSubId, answer},
                  其中填空子题发**长度=空数的 JSON 数组**(整串只判部分分)。

退出码:全过 0,任一失败 1。
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import answer as ans              # noqa: E402
from zjy_client import ZjyClient  # noqa: E402

ans.time.sleep = lambda s: None                    # 心跳/等待在本脚本里全部跳过
FAILS = []
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    if not cond:
        FAILS.append("%s | %s" % (name, detail))


def _opts(n_true=1, pool=None):
    """构造 1 单选型的键面/卷面选项数组(0 基 SortOrder,前 n_true 个 IsAnswer=true)。"""
    pool = pool or ["甲", "乙", "丙", "丁"]
    return [{"Content": c, "SortOrder": i, "IsAnswer": i < n_true} for i, c in enumerate(pool)]


class FakeClient(ZjyClient):
    """不联网的客户端替身:只记录 api_get_ai / session.post 的调用,不建立任何连接。"""

    def __init__(self, key_rows):
        self._key_rows = key_rows
        self.gets = []
        self.posts = []
        self.ai_token = "fake"
        self.session = self

    # 父类构造不参与:直接覆盖所需属性
    def auth_ai_domain(self):
        return None

    def api_get_ai(self, path, params=None, timeout=10):
        self.gets.append((path, dict(params or {})))
        if path.endswith("getByExamId"):
            return {"code": 200, "data": {"paperList": self._key_rows}}
        return {"code": 200, "data": {}}

    def api_post_ai(self, path, data=None, **kw):
        self.posts.append((path, data))
        return {"code": 200}

    def post(self, url, data=None, headers=None, timeout=None):
        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"code": 200, "msg": "ok"}
        self.posts.append((url, data))
        return R()


def build_records(questions, key_rows, mod=ans):
    """跑真实的 `_submit_mooc_exam`,返回它 POST 出去的 taskExamProblemRecordList。

    顺带把本次 `api_get_ai` 的调用序列记在 `build_records.last_gets` 上，供等价性断言比对。
    """
    c = FakeClient(key_rows)
    matching, comp = ({}, {})
    if mod is ans:
        matching, comp = mod.mooc_prepare_key_answers(c, "EX1", questions)
    ok, _ = mod._submit_mooc_exam(c, "自检", "EX1", "", "CI1", "CO1", "3",
                                  "TASK1", questions, 300, "自检卷",
                                  matching_answers=matching, composite_records=comp)
    build_records.last_gets = list(c.gets)          # 供 B 段比对"改前/改后取数序列是否相同"
    if not c.posts:
        return ok, []
    sub = [p for p in c.posts if "course/exam/record" in str(p[0])]
    if not sub:
        return ok, []
    body = sub[-1][1]
    import base64
    from urllib.parse import unquote
    raw = json.loads(unquote(base64.b64decode(body).decode("utf-8")))
    return ok, raw.get("taskExamProblemRecordList") or []


# ---------------------------------------------------------------- 合成卷
Q_CHOICE = {"typeId": "1", "paperId": "P1", "id": "P1", "questionId": "Q1",
            "answer": "2", "dataJson": json.dumps(_opts(1), ensure_ascii=False)}
Q_JUDGE = {"typeId": "3", "paperId": "P3", "id": "P3", "questionId": "Q3", "answer": "1", "dataJson": "[]"}
Q_BLANK = {"typeId": "4", "paperId": "P4", "id": "P4", "questionId": "Q4", "answer": '["甲","乙"]',
           "dataJson": json.dumps([{"Content": "", "SortOrder": 0, "IsAnswer": True},
                                   {"Content": "", "SortOrder": 1, "IsAnswer": True}], ensure_ascii=False)}
Q_ESSAY = {"typeId": "6", "paperId": "P6", "id": "P6", "questionId": "Q6", "answer": "参考答案正文", "dataJson": "[]"}
# 匹配题:卷面 answer 是候选词池(旧实现会把它转成 JSON 数组发出去 ⇒ 平台 500/判 0)
POOL = [{"SortOrder": i, "optionAnswerContent": "词%d" % i} for i in range(4)]
Q_MATCH = {"typeId": "7", "paperId": "P7", "id": "P7", "questionId": "Q7",
           "answer": json.dumps(POOL, ensure_ascii=False), "optionAnswer": json.dumps(POOL, ensure_ascii=False),
           "dataJson": "[]"}
# 综合题:父行 answer 恒空,答案只在子行
Q_COMP = {"typeId": "11", "paperId": "PP", "id": "PP", "questionId": "QP", "answer": "", "dataJson": "[]"}

# 键面(course/exam/getByExamId 的 data.paperList)对应行
K_MATCH = {"typeId": "7", "questionId": "Q7", "id": "P7",
           "dataJson": json.dumps([{"optionContent": "第%d空" % i, "SortOrder": i,
                                    "optionSelectContent": ["2", "0", "3", "1"][i]} for i in range(4)],
                                  ensure_ascii=False)}
K_COMP = {"typeId": "11", "questionId": "QP", "id": "PP", "questionSubList": [
    {"id": "S0", "typeId": "3", "answer": "1", "dataJson": "[]"},
    {"id": "S1", "typeId": "1", "answer": "2", "dataJson": json.dumps(_opts(1), ensure_ascii=False)},
    {"id": "S2", "typeId": "4", "answer": "14.2,17.4",
     "dataJson": json.dumps([{"Content": "14.2", "SortOrder": 0, "IsAnswer": True},
                             {"Content": "17.4", "SortOrder": 1, "IsAnswer": True},
                             {"Content": "干扰", "SortOrder": 2, "IsAnswer": False}], ensure_ascii=False)},
    {"id": "S3", "typeId": "6", "answer": "子题问答正文", "dataJson": "[]"}]}
ROWS = [K_MATCH, K_COMP]

# ---------------------------------------------------------------- A 外呼纪律
plain = [dict(Q_CHOICE), dict(Q_JUDGE), dict(Q_BLANK), dict(Q_ESSAY)]
c = FakeClient(ROWS)
m, k = ans.mooc_prepare_key_answers(c, "EX1", plain)
check("A1 不含7/11 ⇒ 零键面外呼", c.gets == [], "实际外呼=%s" % c.gets)
check("A2 不含7/11 ⇒ 两张表都空", m == {} and k == {}, "%s / %s" % (m, k))
mixed = [dict(Q_CHOICE), dict(Q_MATCH)]
c2 = FakeClient(ROWS)
ans.mooc_prepare_key_answers(c2, "EX1", mixed)
check("A3 含7 ⇒ 只发一次键面", [g[0] for g in c2.gets] == ["course/exam/getByExamId"], c2.gets)
c3 = FakeClient(ROWS)
ans.mooc_prepare_key_answers(c3, "EX1", [dict(Q_COMP)])
check("A4 含11 ⇒ 只发一次键面", len(c3.gets) == 1, c3.gets)

# ---------------------------------------------------------------- B 等价性(与改动前实现比对)
old = None
try:
    # 子进程输出必须按 UTF-8 解:Windows 下 text=True 会用 GBK 解码中文注释,直接抛异常把 B 段吞掉
    src = subprocess.run(["git", "-C", HERE, "show", "HEAD:answer.py"],
                         capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
    if src:
        tmp = os.path.join(tempfile.mkdtemp(prefix="tk_old_"), "answer_old.py")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(src)
        spec = importlib.util.spec_from_file_location("answer_old", tmp)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        old.time.sleep = lambda s: None
except Exception as exc:
    OLD_ERR = "%s: %s" % (type(exc).__name__, exc)
    old = None

if old is None:
    print("(跳过 B:未能加载 HEAD 版 answer.py,通常是无 git 或本文件尚未提交) %s" % OLD_ERR)
else:
    import base64
    from urllib.parse import unquote

    def _decode_records(client_obj):
        posts = [p for p in client_obj.posts if "course/exam/record" in str(p[0])]
        if not posts:
            return None
        return json.loads(unquote(base64.b64decode(posts[-1][1]).decode("utf-8"))
                          )["taskExamProblemRecordList"]

    for label, qs in (("纯普通题卷", plain), ("单选+判断", [dict(Q_CHOICE), dict(Q_JUDGE)])):
        ok_new, rec_new = build_records([dict(x) for x in qs], [])
        c_old = FakeClient([])
        old._submit_mooc_exam(c_old, "自检", "EX1", "", "CI1", "CO1", "3",       # 旧入口无新参数
                              "TASK1", [dict(x) for x in qs], 300, "自检卷")
        rec_old = _decode_records(c_old)
        check("B %s 提交体与改前逐字段相同" % label, rec_new == rec_old,
              "\n  改后=%s\n  改前=%s" % (json.dumps(rec_new, ensure_ascii=False)[:220],
                                          json.dumps(rec_old, ensure_ascii=False)[:220]))
        # 判据是"两侧取数序列完全相同"(updateExamTime 本来就会读一次 paper 面，不是零外呼)
        check("B %s 改前改后取数序列完全相同" % label,
              build_records.last_gets == c_old.gets,
              "  改后=%s 改前=%s" % (build_records.last_gets, c_old.gets))

# ---------------------------------------------------------------- C 匹配题形态
ok, recs = build_records([dict(Q_MATCH)], [K_MATCH])
c7 = [r for r in recs if r.get("paperId") == "P7"]
check("C1 typeId=7 有且只有一条记录", len(c7) == 1, recs)
check("C2 发的是逗号串 2,0,3,1", c7 and c7[0].get("answer") == "2,0,3,1",
      c7[0] if c7 else None)
check("C3 绝不把候选词池发出去", c7 and "optionAnswerContent" not in str(c7[0].get("answer")),
      c7[0] if c7 else None)
ok, recs = build_records([dict(Q_MATCH)], [])      # 键面取不到
c7 = [r for r in recs if r.get("paperId") == "P7"]
check("C4 键面未命中 ⇒ 发空串而不是词池", c7 and c7[0].get("answer") == "",
      c7[0] if c7 else None)

# ---------------------------------------------------------------- D 综合题形态
ok, recs = build_records([dict(Q_COMP)], [K_COMP])
check("D1 纯综合题卷不再被『无答案』拒交", ok is True, "ok=%s" % ok)
check("D2 父行一条不发", [r for r in recs if r.get("paperId") == "PP" and "paperSubId" not in r] == [], recs)
check("D3 逐子题展开 4 条", len([r for r in recs if r.get("paperId") == "PP"]) == 4, recs)
qn = [r.get("questionNo") for r in recs if r.get("paperId") == "PP"]
check("D4 questionNo 是 [父序,子序]", qn == ["[0,0]", "[0,1]", "[0,2]", "[0,3]"], qn)
sub_ids = [r.get("paperSubId") for r in recs if r.get("paperId") == "PP"]
check("D5 paperSubId 取键面子行 id", sub_ids == ["S0", "S1", "S2", "S3"], sub_ids)
by_sub = {r["paperSubId"]: r for r in recs if r.get("paperId") == "PP"}
check("D6 判断子题原样 1", by_sub.get("S0", {}).get("answer") == "1", by_sub.get("S0"))
check("D7 单选子题带 optionSort", "optionSort" in by_sub.get("S1", {})
      and by_sub["S1"]["answer"] == "2", by_sub.get("S1"))
blank = json.loads(by_sub.get("S2", {}).get("answer") or "null")
check("D8 填空子题=长度为空数的 JSON 数组(只取 IsAnswer:true)",
      isinstance(blank, list) and len(blank) == 2 and blank == ["14.2", "17.4"], by_sub.get("S2"))
check("D9 问答子题原样文本", by_sub.get("S3", {}).get("answer") == "子题问答正文", by_sub.get("S3"))

# 综合题混在普通题里:其它题记录不受影响、下标仍按父行在卷内的位置
ok, recs = build_records([dict(Q_CHOICE), dict(Q_MATCH), dict(Q_COMP)], ROWS)
first = [r for r in recs if r.get("paperId") == "P1"]
check("D10 混合卷其它题不受影响(单选仍在下标 0)", first and first[0].get("questionNo") == 0, first)
check("D11 混合卷子题编号用父行在卷内的下标 2",
      [r["questionNo"] for r in recs if r.get("paperId") == "PP"] == ["[2,0]", "[2,1]", "[2,2]", "[2,3]"],
      [r["questionNo"] for r in recs if r.get("paperId") == "PP"])
check("D12 混合卷里匹配题同时命中逗号串",
      [r.get("answer") for r in recs if r.get("paperId") == "P7"] == ["2,0,3,1"],
      [r for r in recs if r.get("paperId") == "P7"])
check("D12b 三型同卷时记录数=1(单选)+1(匹配)+4(子题)", len(recs) == 6, len(recs))

# 一条都备不出时,必须退回改动前行为(父行照旧发出去),不能悄悄交空
ok, recs = build_records([dict(Q_COMP)], [{"typeId": "11", "questionId": "QP", "id": "PP",
                                           "questionSubList": [{"id": "", "typeId": "3", "answer": "1"}]}])
check("D13 子行缺 id ⇒ 不展开,退回改前发父行", [r.get("paperId") for r in recs] == ["PP"]
      and "paperSubId" not in recs[0], recs)

# ---------------------------------------------------------------- 汇总
print("=" * 76)
for name, ok_, detail in CHECKS:
    print("%s  %s%s" % ("PASS" if ok_ else "FAIL", name,
                        "" if ok_ else "  ⇒ " + str(detail)[:180]))
print("=" * 76)
print("合计 %d 条,失败 %d 条" % (len(CHECKS), len(FAILS)))
sys.exit(1 if FAILS else 0)
