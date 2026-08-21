import unittest

from code_cn_bridge.server import _filter_inactive_github_tools


def _github_namespace():
    return {
        "type": "namespace",
        "name": "mcp__codex_apps__github",
        "tools": [{"type": "function", "name": "create_pull_request"}],
    }


def _body(text, *history):
    return {
        "input": [
            *history,
            {"type": "message", "role": "user", "content": text},
        ],
        "tools": [
            {"type": "function", "name": "exec_command"},
            _github_namespace(),
        ],
    }


class GitHubToolRoutingTests(unittest.TestCase):
    def test_ordinary_local_coding_turn_defers_github_namespace(self):
        routed = _filter_inactive_github_tools(_body("修改这段代码并运行测试"))

        self.assertEqual([tool["name"] for tool in routed["tools"]], ["exec_command"])

    def test_local_git_commit_does_not_require_github_connector(self):
        routed = _filter_inactive_github_tools(_body("运行 git status 然后提交修改"))

        self.assertEqual([tool["name"] for tool in routed["tools"]], ["exec_command"])

    def test_local_release_build_does_not_require_github_connector(self):
        routed = _filter_inactive_github_tools(_body("打包发布版让我测试"))

        self.assertEqual([tool["name"] for tool in routed["tools"]], ["exec_command"])

    def test_explicit_remote_request_retains_github_namespace(self):
        original = _body("把改动 push 并创建 PR 到 GitHub")

        self.assertIs(_filter_inactive_github_tools(original), original)

    def test_existing_github_tool_turn_keeps_namespace_available(self):
        call = {
            "type": "function_call",
            "namespace": "mcp__codex_apps__github",
            "name": "get_repo",
            "call_id": "call_repo",
            "arguments": "{}",
        }
        original = _body("继续处理", call)

        self.assertIs(_filter_inactive_github_tools(original), original)


if __name__ == "__main__":
    unittest.main()
