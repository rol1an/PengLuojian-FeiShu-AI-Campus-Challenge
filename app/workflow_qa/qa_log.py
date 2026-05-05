"""Structured Q&A logging and LLM-as-Judge evaluation.

Three record types written to qa_log.jsonl (append-only):
  {"type": "qa",       "qa_id": ..., "ts": ..., ...}
  {"type": "feedback", "qa_id": ..., "ts": ..., "rating": "good"|"bad"}
  {"type": "judge",    "qa_id": ..., "ts": ..., "score": 1-5, ...}
"""
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from app.llm import call_llm

logger = logging.getLogger(__name__)

_LOG_PATH = Path("qa_log.jsonl")

_JUDGE_SYSTEM_PROMPT = """\
你是 Q&A 质量评估器。根据提供的上下文和问答记录，输出 JSON 评估结果。
只输出纯 JSON，不加任何注释或 Markdown。\
"""

_JUDGE_USER_TEMPLATE = """\
问题：{question}
回答：{reply}
检索路径：{path}（live_search=实时搜索命中 | key_docs_fallback=从会前文档回退 | brief_only=仅依靠摘要）

传入 LLM 的上下文节选（前 1500 字）：
{context_snippet}

评估输出（严格 JSON）：
{{
  "on_topic": true/false,
  "grounded": true/false,
  "source_cited": true/false,
  "score": 1-5,
  "reason": "一句话说明理由"
}}

评分标准：
1 = 完全无关或胡编  2 = 答非所问  3 = 基本回答但来源不清  4 = 准确且有来源  5 = 完美\
"""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class QARecord:
    qa_id: str
    ts: float
    user_open_id: str
    event_id: str
    event_title: str
    question: str
    reply: str
    path: str           # "live_search" | "key_docs_fallback" | "brief_only"
    context_chars: int
    context_snippet: str = ""   # first 1500 chars, used by judge
    feedback: str = ""          # "good" | "bad" | "" (filled later)


def new_qa_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def log_qa(record: QARecord) -> None:
    """Append a Q&A record to the JSONL log."""
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            entry = {"type": "qa", **asdict(record)}
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(
            "QA_LOG qa_id=%s path=%s ctx_chars=%d question='%s'",
            record.qa_id, record.path, record.context_chars, record.question[:40],
        )
    except Exception as e:
        logger.debug("Failed to write QA record: %s", e)


def log_feedback(qa_id: str, user_open_id: str, rating: str) -> None:
    """Append a user feedback record."""
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            entry = {
                "type": "feedback",
                "qa_id": qa_id,
                "user_open_id": user_open_id,
                "rating": rating,
                "ts": time.time(),
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("FEEDBACK qa_id=%s rating=%s user=%s", qa_id, rating, user_open_id)
    except Exception as e:
        logger.debug("Failed to write feedback record: %s", e)


def _log_judge(qa_id: str, result: dict) -> None:
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            entry = {"type": "judge", "qa_id": qa_id, "ts": time.time(), **result}
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("Failed to write judge record: %s", e)


# ---------------------------------------------------------------------------
# Read + merge
# ---------------------------------------------------------------------------

def read_qa_records(limit: int = 100) -> list[dict]:
    """Read recent Q&A records merged with feedback and judge results."""
    if not _LOG_PATH.exists():
        return []

    qa_records: dict[str, dict] = {}
    feedback_map: dict[str, str] = {}
    judge_map: dict[str, dict] = {}

    for line in _LOG_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = entry.get("type")
        qa_id = entry.get("qa_id", "")
        if t == "qa":
            qa_records[qa_id] = entry
        elif t == "feedback" and qa_id:
            feedback_map[qa_id] = entry.get("rating", "")
        elif t == "judge" and qa_id:
            judge_map[qa_id] = entry

    # Merge
    for qa_id, rec in qa_records.items():
        if qa_id in feedback_map:
            rec["feedback"] = feedback_map[qa_id]
        if qa_id in judge_map:
            j = judge_map[qa_id]
            rec.update({
                "judge_score": j.get("score"),
                "judge_on_topic": j.get("on_topic"),
                "judge_grounded": j.get("grounded"),
                "judge_source_cited": j.get("source_cited"),
                "judge_reason": j.get("reason", ""),
            })

    sorted_records = sorted(qa_records.values(), key=lambda r: r.get("ts", 0), reverse=True)
    return sorted_records[:limit]


# ---------------------------------------------------------------------------
# LLM-as-Judge
# ---------------------------------------------------------------------------

async def judge_qa_record(record: dict) -> dict | None:
    """Evaluate a single Q&A record with LLM. Returns parsed judge dict or None."""
    user_msg = _JUDGE_USER_TEMPLATE.format(
        question=record.get("question", ""),
        reply=record.get("reply", ""),
        path=record.get("path", ""),
        context_snippet=(record.get("context_snippet") or "")[:1500],
    )
    try:
        raw = await call_llm(_JUDGE_SYSTEM_PROMPT, user_msg, temperature=0.1)
        # Strip markdown fences if present
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(clean)
        logger.info(
            "JUDGE qa_id=%s score=%s on_topic=%s grounded=%s reason='%s'",
            record.get("qa_id"), result.get("score"), result.get("on_topic"),
            result.get("grounded"), result.get("reason", "")[:50],
        )
        return result
    except Exception as e:
        logger.warning("judge_qa_record failed for qa_id=%s: %s", record.get("qa_id"), e)
        return None


async def run_batch_evaluation(limit: int = 20) -> dict:
    """Evaluate recent Q&A records that haven't been judged yet.

    Returns a summary of evaluation results.
    """
    records = read_qa_records(limit=limit)
    unjudged = [r for r in records if "judge_score" not in r]

    if not unjudged:
        return {"message": "没有待评估的记录", "evaluated": 0}

    scored = []
    for rec in unjudged:
        result = await judge_qa_record(rec)
        if result:
            _log_judge(rec["qa_id"], result)
            scored.append({"qa_id": rec["qa_id"], **result})

    if not scored:
        return {"message": "评估失败", "evaluated": 0}

    avg_score = sum(r.get("score", 0) for r in scored) / len(scored)
    on_topic_rate = sum(1 for r in scored if r.get("on_topic")) / len(scored)
    grounded_rate = sum(1 for r in scored if r.get("grounded")) / len(scored)
    source_cited_rate = sum(1 for r in scored if r.get("source_cited")) / len(scored)

    return {
        "evaluated": len(scored),
        "avg_score": round(avg_score, 2),
        "on_topic_rate": round(on_topic_rate, 2),
        "grounded_rate": round(grounded_rate, 2),
        "source_cited_rate": round(source_cited_rate, 2),
        "records": scored,
    }
