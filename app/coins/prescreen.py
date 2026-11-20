"""铜币好品推荐初筛：匹配与特征提取之间，用 GLM 视觉模型判断是否进入特征提取。

提示词照录 `data/handoffs/2026-11-11-coin-grade-mimo-prompt.md` 的「初筛阶段」；
结果写回 `coin_lot_grades`：状态置 prescreen，原文 / pass_to_next / 时间落库。
"""

from __future__ import annotations

import json
import logging

from app import repository
from app.coin_grade.mimo_client import MimoClient, MimoError
from app.config import Config

logger = logging.getLogger(__name__)

PRESCREEN_SYSTEM_PROMPT = """你是一名极其严苛的拍品表面状态初筛员。你的唯一任务是“排雷”。
你需要同时拦截两类拍品：一类是“有硬伤缺陷”的，另一类是“磨损特别大”的。"""

PRESCREEN_USER_PROMPT = """【核心淘汰规则（最高优先级，必须严格执行）】

一、硬伤淘汰（满足任意一项即 reject=true）：
1. 底板、文字区、图案区出现任何明显的坑洞、凹坑、深划痕。
2. 出现大面积暗色斑点、严重锈蚀或严重遮挡。

二、过度磨损淘汰（必须同时满足以下至少两项，且程度严重，才 reject=true）：
1. 高点严重磨平：人物面部、数字核心、动物主体等关键高点完全失去立体感，变成平滑斜坡（轻微圆滑不算）。
2. 图案细节严重融合：羽毛、花环、山纹、数字内部纹理大面积糊在一起，完全无法分辨（局部模糊不算）。
3. 底板划痕严重密集：划痕深且密，明显破坏底板平整度，呈现“拉丝”或“磨砂”状（普通流通细划痕不算）。
4. 文字严重发软：主要文字笔画边缘圆钝，轮廓模糊，难以辨认（边缘微圆不算）。
5. 内齿/边缘严重磨损：正反面任意一面的内齿（边缘颗粒）大面积缺失、模糊，或与底板/文字严重融合，失去清晰的颗粒感（轻微变浅或局部模糊不算）。

【正常流通豁免条款（优先级高于淘汰规则）】
若硬币仅表现为以下特征，判定为“正常流通品相”，pass_to_next=true，绝对不得淘汰：
- 高点轻微至中度圆滑，但主体轮廓依然立体；
- 底板有细划痕或自然氧化斑点，但未形成深坑或大面积暗斑；
- 文字边缘微圆，但整体清晰可读；
- 正反面内齿轻微变浅或局部模糊，但整体颗粒感依然存在；
- 表面有自然包浆或轻微反光，但未影响整体辨识度。

【输出规则】
1. 只描述可见事实，不补脑。
2. 不写钟点方位，使用“上/下/左/右/中心/边缘/文字区/图案区/底板区”。
3. 不输出具体数字、条数、百分比、分数。
4. 输出必须是严格 JSON，不要输出多余解释。
5. 所有枚举值必须使用下方指定的中文词汇。

【输出 JSON 结构】
{
  "image_id": "",
  "image_quality": {
    "resolution_sufficient": "足够 / 勉强 / 不足",
    "lighting_issue": "无 / 反光 / 过曝 / 欠曝 / 光照不均 / 未知",
    "focus_issue": "无 / 轻微模糊 / 严重模糊 / 未知",
    "occlusion": "无 / 部分遮挡 / 严重遮挡 / 未知",
    "usable_area": "描述可判断区域，无法判断写未知"
  },
  "obverse": {
    "main_highpoint_condition": "锐利 / 轻微圆滑 / 明显圆滑 / 磨平 / 无法辨认 / 不可判断",
    "text_condition": "边缘清晰 / 边缘微圆 / 明显发软 / 模糊 / 局部缺损 / 无法辨认 / 不可判断",
    "rim_condition": "锐利 / 轻微变浅 / 局部模糊 / 大面积缺失或融合 / 不可判断",
    "visible_defects": ["划痕", "磕碰", "凹坑", "锈蚀", "污渍", "粘连", "异常光泽", "无", "不可判断"],
    "defect_locations": ["用上下左右、边缘、中心、文字区、图案区、底板区描述位置"]
  },
  "reverse": {
    "main_highpoint_condition": "锐利 / 轻微圆滑 / 明显圆滑 / 磨平 / 无法辨认 / 不可判断",
    "text_condition": "边缘清晰 / 边缘微圆 / 明显发软 / 模糊 / 局部缺损 / 无法辨认 / 不可判断",
    "rim_condition": "锐利 / 轻微变浅 / 局部模糊 / 大面积缺失或融合 / 不可判断",
    "visible_defects": ["划痕", "磕碰", "凹坑", "锈蚀", "污渍", "粘连", "异常光泽", "无", "不可判断"],
    "defect_locations": ["用上下左右、边缘、中心、文字区、图案区、底板区描述位置"]
  },
  "surface_issues": {
    "has_heavy_scratches": true / false / unknown,
    "has_obvious_dents": true / false / unknown,
    "has_highpoint_flattening": true / false / unknown,
    "has_text_blur": true / false / unknown,
    "has_corrosion_or_rust": true / false / unknown,
    "has_large_stain_or_adhesion": true / false / unknown,
    "has_abnormal_gloss": true / false / unknown,
    "has_severe_image_issue": true / false / unknown,
    "has_severe_rim_wear": true / false / unknown
  },
  "reject": true / false,
  "reject_reasons": ["必须明确写出淘汰理由，如：底板有明显凹坑、主要高点明显磨平、底板细划痕密集、正/反面内齿大面积缺失或严重融合"],
  "pass_to_next": true / false,
  "confidence": "高 / 中 / 低",
  "note": "一句话补充，禁止给出分数或档位"
}"""


def parse_prescreen(text: str | None) -> bool | None:
    """模型返回 → `pass_to_next`；非严格 JSON 或缺字段返回 None（不猜测）。"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("pass_to_next")
    return value if isinstance(value, bool) else None


def prescreen_one(
    config: Config,
    grade_id: int,
    *,
    client: MimoClient | None = None,
) -> bool:
    """单条初筛：按 `coin_lot_grades.id` 取图调用模型并写回；失败只记错误。"""
    stage = config.coin_grade.prescreen
    if not stage.base_url or not stage.model or not stage.api_key:
        logger.info("初筛：coin_grade.prescreen 未配置，跳过")
        return False
    target = repository.get_prescreen_target(grade_id)
    if target is None:
        logger.warning("初筛：coin_lot_grades.id=%s 不存在", grade_id)
        return False
    images = list(target["image_urls"] or [])
    if not images:
        repository.mark_coin_lot_prescreen_error(grade_id, "拍品没有图片")
        return False

    owned = client is None
    vision = client or MimoClient(
        base_url=stage.base_url,
        api_key=stage.api_key,
        model=stage.model,
        temperature=stage.temperature,
        timeout_seconds=stage.timeout_seconds,
    )
    try:
        raw = vision.grade(
            system=PRESCREEN_SYSTEM_PROMPT,
            user=PRESCREEN_USER_PROMPT,
            images=images[:2],
        )
    except MimoError as exc:
        repository.mark_coin_lot_prescreen_error(grade_id, str(exc))
        logger.warning("初筛：记录 %s 调用失败：%s", grade_id, exc)
        return False
    except Exception as exc:  # 单条失败不影响后续
        repository.mark_coin_lot_prescreen_error(grade_id, f"{type(exc).__name__}: {exc}")
        logger.warning("初筛：记录 %s 调用异常：%s", grade_id, exc)
        return False
    finally:
        if owned:
            vision.close()

    passed = parse_prescreen(raw)
    if passed is None:
        repository.mark_coin_lot_prescreen_error(grade_id, "返回无法解析 pass_to_next")
        return False
    logger.info("初筛结果：%s", raw)
    repository.save_coin_lot_prescreen(
        grade_id, raw=raw, passed=passed, model=stage.model
    )
    return True
