# pip3 install transformers
"""计算 DeepSeek 的 token 数量。

用法：
    python deepseek_tokenizer.py "要统计的文本"
    python deepseek_tokenizer.py path/to/file.txt
    echo "文本" | python deepseek_tokenizer.py
"""
import argparse
import os
import sys
from pathlib import Path

# 屏蔽 transformers 的 "PyTorch was not found" 等提示，保证输出只有 token 数
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

import transformers

# tokenizer 文件与脚本放在同一目录，因此从任意位置调用都能加载
chat_tokenizer_dir = Path(__file__).resolve().parent

# 直接加载 tokenizer.json：transformers 5.x 的 AutoTokenizer 会退化成 slow
# LlamaTokenizer，导致中文等未登录字符被整段丢弃
tokenizer = transformers.PreTrainedTokenizerFast(
    tokenizer_file=str(chat_tokenizer_dir / "tokenizer.json")
)


def main() -> None:
    parser = argparse.ArgumentParser(description="计算文本的 DeepSeek token 数量")
    parser.add_argument(
        "text",
        nargs="?",
        help="要统计的文本；若是已存在的文件路径则读取该文件内容",
    )
    parser.add_argument("--ids", action="store_true", help="同时打印 token id 列表")
    args = parser.parse_args()

    if args.text is not None:
        path = Path(args.text)
        content = path.read_text(encoding="utf-8") if path.is_file() else args.text
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
    else:
        parser.error("请传入文本、文件路径，或通过管道输入")

    result = tokenizer.encode(content)
    print(len(result))
    if args.ids:
        print(result)


if __name__ == "__main__":
    main()
