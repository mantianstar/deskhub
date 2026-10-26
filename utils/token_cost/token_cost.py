# 多轮任务 token 成本估算（测试版）
"""按「每轮累计上下文」估算一次任务的 token 成本。

用法：
    python3 utils/token_cost/token_cost.py utils/token_cost/sample_task.json
    python3 utils/token_cost/token_cost.py sample.json --context-now 70000 --requests 11 --total 670000
    python3 utils/token_cost/token_cost.py --usage usage.jsonl

真实 usage 模式（推荐）：
    每次请求都能拿到接口返回的 `usage` 时，直接把响应贴成 JSONL（一行一次请求），
    脚本取每行的 `usage.prompt_tokens / usage.completion_tokens` 出表，不再反推底座。
    也接受平铺对象 `{"prompt_tokens": .., "completion_tokens": ..}` 或 JSON 数组。
    行里带 `action / object`（代理写入）时多出「动作 / 对象」两列；旧日志缺这两列留 `-`。

校准（推荐先用一次）：
    平台侧显示「当前上下文 / 新增请求数 / 总 tokens」时，用这三个锚点反推每请求的固定底座与增长，
    后续估算以反推值为准；不传时按流水内容或默认 20000 计算。

流水格式（JSON）：
{
  "system": "系统提示文本（可选）",
  "tools": "工具定义文本（可选）",
  "rounds": [
    {"new": ["user: ...", "tool: ..."], "output": "assistant: ..."}
  ]
}

口径：
- 第 N 轮输入 = system + tools + 前 N-1 轮全部消息 + 第 N 轮新增消息（上下文每轮重发）。
- 总输入 = 各轮输入之和；总输出 = 各轮输出之和；总成本 = 两者相加。
- 分词的准确度取决于流水文本是否与平台实际发送内容一致。
"""
import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKENIZER_PATH = ROOT / "utils" / "deepseek_v4_tokenizer" / "deepseek_tokenizer.py"
DEFAULT_FIXED_TOKENS = 19570  # 2026-10-26 首个真实请求实测（system + 工具/技能定义 + memory），约占 1M 窗口 2%
_OBJECT_PREVIEW = 30  # 对象列只留短标题；旧日志里的长文本也一并压掉


def load_tokenizer():
    spec = importlib.util.spec_from_file_location("ds_tokenizer", TOKENIZER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.tokenizer


def count(tokenizer, text: str) -> int:
    return len(tokenizer.encode(text or ""))


def load_usage_rows(path: Path) -> list[dict[str, int]]:
    """读真实 usage 流水：JSON 数组，或每行一个 JSON；元素可直接是响应体（取 usage 字段）。"""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        items = json.loads(text)
    else:
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    rows = []
    for item in items:
        usage = item.get("usage") if isinstance(item, dict) else None
        source = usage if isinstance(usage, dict) else item
        if not isinstance(source, dict):
            continue
        rows.append(
            {
                "action": str(source.get("action") or ""),
                "object": str(source.get("object") or ""),
                "prompt_tokens": int(source.get("prompt_tokens") or 0),
                "completion_tokens": int(source.get("completion_tokens") or 0),
            }
        )
    return rows


def print_usage_table(rows: list[dict[str, int]]) -> None:
    """按真实 usage 出成本表：输入就是该次请求的 prompt，不反推底座。"""
    print("| 请求 | 动作 | 对象 | 输入 | 输出 | 成本 |")
    print("|---|---|---|---|---|---|")
    total_input = total_output = 0
    for index, row in enumerate(rows, start=1):
        total_input += row["prompt_tokens"]
        total_output += row["completion_tokens"]
        print(
            f"| {index} | {_cell(row.get('action'))} | {_object_cell(row.get('object'))} | "
            f"{row['prompt_tokens']} | {row['completion_tokens']} | "
            f"{row['prompt_tokens'] + row['completion_tokens']} |"
        )
    print(f"| **合计** | - | - | **{total_input}** | **{total_output}** | **{total_input + total_output}** |")


def _cell(text: str | None, limit: int | None = None) -> str:
    """表格单元格：空值留 `-`，竖线转义、换行压平；超出 limit 的截断，避免撑坏 Markdown 表。"""
    text = " ".join(str(text or "").split()).replace("|", "\\|")
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "-"


def _object_cell(text: str | None) -> str:
    """对象列只留标识：绝对路径转成仓库相对路径（不会截掉文件名），其余截到 30 字。"""
    text = str(text or "")
    if text.startswith("/"):
        path = Path(text)
        try:
            return _cell(str(path.relative_to(ROOT)))
        except ValueError:
            return _cell(path.name)
    return _cell(text, _OBJECT_PREVIEW)


def main() -> None:
    parser = argparse.ArgumentParser(description="按轮次估算任务的 token 成本")
    parser.add_argument("transcript", nargs="?", help="流水 JSON 路径（估算模式）")
    parser.add_argument("--usage", help="真实 usage 流水：JSONL / JSON 数组，每行含 prompt_tokens 与 completion_tokens，可直接贴 DeepSeek 响应")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON 汇总")
    parser.add_argument(
        "--fixed-tokens",
        type=int,
        help="system + tools 的固定开销；不传则用流水里的 system/tools 实测，缺失时取 19570（本机实测底座）",
    )
    parser.add_argument("--context-now", type=int, help="平台显示的当前上下文 tokens")
    parser.add_argument("--context-start", type=int, help="这批请求开始前的上下文 tokens（可选，用于算偏差）")
    parser.add_argument("--requests", type=int, help="平台显示的新增请求数")
    parser.add_argument("--total", type=int, help="平台显示的总 tokens")
    args = parser.parse_args()

    if args.usage:
        print_usage_table(load_usage_rows(Path(args.usage)))
        return
    if not args.transcript:
        parser.error("需要流水 JSON 路径，或用 --usage 传真实 usage 流水")

    data = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    tokenizer = load_tokenizer()

    fixed = "\n".join(filter(None, [data.get("system"), data.get("tools")]))
    if args.fixed_tokens is not None:
        fixed_tokens = args.fixed_tokens
    elif fixed:
        fixed_tokens = count(tokenizer, fixed)
    else:
        fixed_tokens = DEFAULT_FIXED_TOKENS

    history = ""
    rows = []
    total_input = total_output = 0
    for index, item in enumerate(data.get("rounds", []), start=1):
        added = "\n".join(item.get("new", []))
        history = f"{history}\n{added}" if added else history
        input_tokens = fixed_tokens + count(tokenizer, history)
        output_tokens = count(tokenizer, item.get("output", ""))
        total_input += input_tokens
        total_output += output_tokens
        rows.append(
            {
                "round": index,
                "new_tokens": count(tokenizer, added),
                "context_tokens": input_tokens,
                "output_tokens": output_tokens,
                "round_cost": input_tokens + output_tokens,
            }
        )

    print(f"固定开销（system + tools）：{fixed_tokens} tokens")
    print("| 轮 | 新增输入 | 累计上下文（输入） | 输出 | 轮成本 |")
    print("|---|---|---|---|---|")
    for row in rows:
        print(
            f"| {row['round']} | {row['new_tokens']} | {row['context_tokens']} | "
            f"{row['output_tokens']} | {row['round_cost']} |"
        )
    print(f"| **合计** | - | **{total_input}** | **{total_output}** | **{total_input + total_output}** |")

    if args.json:
        print(
            json.dumps(
                {"rounds": rows, "total_input": total_input, "total_output": total_output,
                 "total": total_input + total_output},
                ensure_ascii=False,
            )
        )

    print_calibration(args)


def print_calibration(args) -> None:
    if None in (args.context_now, args.requests, args.total):
        return
    requests, total, context_now = args.requests, args.total, args.context_now
    average = total / requests
    print()
    print(f"平台锚点：{requests} 次请求 / 总计 {total} / 当前上下文 {context_now}")

    if args.context_start is not None:
        start = args.context_start
        growth = (context_now - start) / (requests - 1) if requests > 1 else 0
        predicted = requests * (start + context_now) / 2
        base_total = start * requests
        print(f"反推：每请求平均 {round(average)}；每请求增长 ≈{round(growth)}")
        print(f"线性预测 {round(predicted)}，实际 {total}，偏差 {total - round(predicted):+d}（{(total - predicted) / total:+.0%}）")
        print(
            f"结构：底座 {round(base_total)}（{base_total / total:.0%}）；"
            f"对话增长 {round(total - base_total)}（{1 - base_total / total:.0%}）"
        )
        return

    base = 2 * average - context_now
    base_total = base * requests
    print(f"反推：每请求平均 {round(average)}；起始底座 {round(base)}（缺起始读数，为估算）")
    print(
        f"结构：固定底座 {round(base_total)}（{base_total / total:.0%}）；"
        f"对话增长 {round(total - base_total)}（{1 - base_total / total:.0%}）"
    )


if __name__ == "__main__":
    main()
