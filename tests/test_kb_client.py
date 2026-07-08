"""
法律知识库客户端 — 单元测试
"""
import pytest
from app.kb_client import format_search_results


class TestFormatSearchResults:
    """测试检索结果格式化"""

    def test_empty_results(self):
        assert format_search_results({}) == ""
        assert format_search_results({"tokenHits": [], "vectorHits": []}) == ""

    def test_basic_formatting(self):
        result = {
            "mode": "hybrid",
            "vectorHits": [
                {"file": "法律/民法典.md", "content": "第一条 为了保护民事主体的合法权益...", "vectorScore": 0.95},
            ],
            "tokenHits": [],
        }
        output = format_search_results(result)
        assert "相关法律法规参考（来自知识库）" in output
        assert "法律/民法典.md" in output
        assert "第一条 为了保护民事主体的合法权益" in output

    def test_deduplication_by_file(self):
        """同文件的多条命中应去重"""
        result = {
            "vectorHits": [
                {"file": "合同法.md", "content": "第一条...", "vectorScore": 0.9},
            ],
            "tokenHits": [
                {"file": "合同法.md", "content": "第二条...", "score": 0.8},
            ],
        }
        output = format_search_results(result)
        # 只应出现一次合同法.md
        assert output.count("合同法.md") == 1

    def test_score_sorting_vector_first(self):
        """向量检索结果应排在前面"""
        result = {
            "vectorHits": [
                {"file": "A.md", "content": "high score", "vectorScore": 0.99},
            ],
            "tokenHits": [
                {"file": "B.md", "content": "low score", "score": 0.5},
            ],
        }
        output = format_search_results(result)
        assert output.index("A.md") < output.index("B.md")

    def test_max_chars_truncation(self):
        """超过 max_chars 应截断"""
        long_content = "测试内容" * 500  # ~2500 chars
        result = {
            "vectorHits": [
                {"file": "长文件.md", "content": long_content, "vectorScore": 0.9},
            ]
        }
        output = format_search_results(result, max_chars=500)
        assert len(output) <= 550  # 允许标题开销
        assert "截断" in output

    def test_max_chars_unlimited(self):
        """max_chars=0 应不截断"""
        content = "测试" * 1000
        result = {
            "vectorHits": [
                {"file": "f.md", "content": content, "vectorScore": 0.9},
            ]
        }
        output = format_search_results(result, max_chars=0)
        assert "截断" not in output

    def test_empty_content_skipped(self):
        """空内容的命中应跳过"""
        result = {
            "vectorHits": [
                {"file": "empty.md", "content": "", "vectorScore": 0.5},
                {"file": "good.md", "content": "有效内容", "vectorScore": 0.9},
            ],
        }
        output = format_search_results(result)
        assert "good.md" in output
        assert "empty.md" not in output
