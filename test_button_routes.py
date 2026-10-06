import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class ButtonRouteTests(unittest.TestCase):
    def test_every_discord_button_has_a_worker_route(self):
        """Pythonが表示する全ボタンをCloudflare Workerが受け取れることを保証する。"""
        python_source = (ROOT / "discord_bot.py").read_text(encoding="utf-8")
        worker_source = (ROOT / "worker.js").read_text(encoding="utf-8")
        worker_prefixes = set(re.findall(r'\.startsWith\("([^"]+)"\)', worker_source))

        buttons = []
        for node in ast.walk(ast.parse(python_source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                is_button = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "button"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "ui"
                )
                if not is_button:
                    continue
                values = {keyword.arg: keyword.value for keyword in decorator.keywords}
                label_node = values.get("label")
                custom_id_node = values.get("custom_id")
                label = ast.literal_eval(label_node) if label_node is not None else node.name
                self.assertIsNotNone(custom_id_node, f"ボタン「{label}」にcustom_idがありません")
                self.assertIsInstance(
                    custom_id_node, ast.Constant,
                    f"ボタン「{label}」のcustom_idは固定文字列にしてください",
                )
                buttons.append((label, custom_id_node.value))

        self.assertGreater(len(buttons), 0)
        for label, custom_id in buttons:
            self.assertTrue(
                any(custom_id.startswith(prefix) for prefix in worker_prefixes),
                f"ボタン「{label}」({custom_id}) をWorkerが処理できません",
            )


if __name__ == "__main__":
    unittest.main()
