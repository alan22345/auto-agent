"""Tests for agent/tools/cache.py — tool result caching."""

from agent.tools.base import ToolResult
from agent.tools.cache import ToolCache


class TestCacheHitMiss:
    def test_miss_on_empty_cache(self):
        cache = ToolCache()
        result = cache.get("glob", {"pattern": "**/*.py"})
        assert result is None

    def test_hit_after_put(self):
        cache = ToolCache()
        expected = ToolResult(output="file1.py\nfile2.py", token_estimate=10)
        cache.put("glob", {"pattern": "**/*.py"}, expected)
        result = cache.get("glob", {"pattern": "**/*.py"})
        assert result is not None
        assert result.output == expected.output

    def test_different_args_miss(self):
        cache = ToolCache()
        cache.put("glob", {"pattern": "**/*.py"}, ToolResult(output="a.py"))
        result = cache.get("glob", {"pattern": "**/*.ts"})
        assert result is None

    def test_same_tool_different_args_are_separate(self):
        cache = ToolCache()
        cache.put("grep", {"pattern": "foo"}, ToolResult(output="match1"))
        cache.put("grep", {"pattern": "bar"}, ToolResult(output="match2"))
        assert cache.get("grep", {"pattern": "foo"}).output == "match1"
        assert cache.get("grep", {"pattern": "bar"}).output == "match2"


class TestCacheableTools:
    def test_glob_is_cacheable(self):
        cache = ToolCache()
        cache.put("glob", {"pattern": "*"}, ToolResult(output="x"))
        assert cache.get("glob", {"pattern": "*"}) is not None

    def test_grep_is_cacheable(self):
        cache = ToolCache()
        cache.put("grep", {"pattern": "x"}, ToolResult(output="y"))
        assert cache.get("grep", {"pattern": "x"}) is not None

    def test_file_read_is_not_cacheable(self):
        cache = ToolCache()
        cache.put("file_read", {"file_path": "x"}, ToolResult(output="content"))
        assert cache.get("file_read", {"file_path": "x"}) is None

    def test_bash_is_not_cacheable(self):
        cache = ToolCache()
        cache.put("bash", {"command": "ls"}, ToolResult(output="files"))
        assert cache.get("bash", {"command": "ls"}) is None


class TestWriteInvalidation:
    def test_file_write_clears_cache(self):
        cache = ToolCache()
        cache.put("glob", {"pattern": "**/*.py"}, ToolResult(output="a.py"))
        assert cache.size == 1
        cache.invalidate_on_write("file_write")
        assert cache.size == 0
        assert cache.get("glob", {"pattern": "**/*.py"}) is None

    def test_file_edit_clears_cache(self):
        cache = ToolCache()
        cache.put("grep", {"pattern": "foo"}, ToolResult(output="bar"))
        cache.invalidate_on_write("file_edit")
        assert cache.size == 0

    def test_bash_clears_cache(self):
        cache = ToolCache()
        cache.put("glob", {"pattern": "*"}, ToolResult(output="x"))
        cache.invalidate_on_write("bash")
        assert cache.size == 0

    def test_read_does_not_invalidate(self):
        cache = ToolCache()
        cache.put("glob", {"pattern": "*"}, ToolResult(output="x"))
        cache.invalidate_on_write("file_read")
        assert cache.size == 1

    def test_glob_does_not_invalidate(self):
        cache = ToolCache()
        cache.put("grep", {"pattern": "x"}, ToolResult(output="y"))
        cache.invalidate_on_write("glob")
        assert cache.size == 1


class TestErrorCaching:
    def test_errors_not_cached(self):
        cache = ToolCache()
        cache.put("glob", {"pattern": "*"}, ToolResult(output="error", is_error=True))
        assert cache.get("glob", {"pattern": "*"}) is None


class TestEviction:
    def test_evicts_oldest_when_full(self):
        cache = ToolCache(max_entries=2)
        cache.put("glob", {"pattern": "a"}, ToolResult(output="1"))
        cache.put("glob", {"pattern": "b"}, ToolResult(output="2"))
        # Cache full — adding c should evict a (oldest)
        cache.put("glob", {"pattern": "c"}, ToolResult(output="3"))
        assert cache.get("glob", {"pattern": "a"}) is None
        assert cache.get("glob", {"pattern": "b"}) is not None
        assert cache.get("glob", {"pattern": "c"}) is not None

    def test_updating_existing_key_does_not_evict(self):
        cache = ToolCache(max_entries=2)
        cache.put("glob", {"pattern": "a"}, ToolResult(output="1"))
        cache.put("glob", {"pattern": "b"}, ToolResult(output="2"))
        # Update a — should not evict anything
        cache.put("glob", {"pattern": "a"}, ToolResult(output="updated"))
        assert cache.size == 2
        assert cache.get("glob", {"pattern": "a"}).output == "updated"
