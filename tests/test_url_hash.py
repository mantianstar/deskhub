"""URL 规范化边界穷举（plan §13.1 用例 1）。

跑法：`.venv/bin/python -m pytest tests/test_url_hash.py -q`
（用 `-m pytest` 而不是 `pytest`，保证项目根目录在 sys.path 上。）

这里全是纯函数，不访问网络 —— 归一化错一条，日报就会出现重复内容。
"""

from __future__ import annotations

import pytest

from app.fetcher import canonicalize, url_hash


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # host 小写 + 去 www. + 去 fragment
        ("https://www.Example.com/Post/1#section", "https://example.com/Post/1"),
        ("https://BLOG.example.com/a#x", "https://blog.example.com/a"),
        # scheme 统一 https
        ("http://example.com/a", "https://example.com/a"),
        # 路径去尾斜杠（非根路径）
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/a/b/", "https://example.com/a/b"),
        # 合并重复斜杠
        ("https://example.com//a///b//", "https://example.com/a/b"),
        # 根路径：`https://example.com` 与 `https://example.com/` 归一成同一个
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        # 跟踪参数全去掉，且大小写不敏感
        (
            "https://example.com/a?utm_source=rss&utm_medium=x&fbclid=1&gclid=2&ref=3&source=4&spm=5&from=6",
            "https://example.com/a",
        ),
        ("https://example.com/a?UTM_Source=x&SOURCE=y", "https://example.com/a"),
        # 非跟踪参数保留（不能为了去重把正常参数也删了）
        ("https://example.com/a?id=7&utm_source=x", "https://example.com/a?id=7"),
        ("https://example.com/a?x=&y=1", "https://example.com/a?x=&y=1"),
        # query 按 key 排序（重复 key 按值排序）
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        ("https://example.com/a?tag=b&tag=a", "https://example.com/a?tag=a&tag=b"),
        # 默认端口去掉，非默认端口保留
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "https://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        # 两侧空白
        ("  https://example.com/a  ", "https://example.com/a"),
        # 协议相对写法
        ("//example.com/a", "https://example.com/a"),
        # IPv6 字面量
        ("https://[2001:DB8::1]:443/a", "https://[2001:db8::1]/a"),
        ("https://[2001:db8::1]:8443/a", "https://[2001:db8::1]:8443/a"),
    ],
)
def test_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://example.com/a",
        "example.com/a",  # 没有 scheme 也没有 host
        "javascript:alert(1)",
        "https://a.com:abc/x",  # 端口不是数字
    ],
)
def test_canonicalize_rejects_unusable_url(raw: str) -> None:
    with pytest.raises(ValueError):
        canonicalize(raw)


def test_hash_same_for_same_article_in_different_dress() -> None:
    """同一篇文章带不同跟踪参数 / 不同 scheme / 带 fragment，去重键必须一致。"""
    variants = [
        "https://www.infoq.cn/article/abc?utm_source=rss&utm_medium=article",
        "http://infoq.cn/article/abc/",
        "https://infoq.cn/article/abc#comments",
        "https://INFOQ.CN/article/abc?spm=1",
    ]
    assert len({url_hash(url) for url in variants}) == 1


def test_hash_differs_for_different_articles() -> None:
    a = "https://www.infoq.cn/article/abc"
    b = "https://www.infoq.cn/article/abd"
    c = "https://www.infoq.cn/article/abc?page=2"
    assert url_hash(a) != url_hash(b)
    assert url_hash(a) != url_hash(c)


def test_hash_is_sha256_hex() -> None:
    digest = url_hash("https://example.com/a")
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
