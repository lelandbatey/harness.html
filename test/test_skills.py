"""skills.py: the built-in singlefile-harness skill must load (its text goes through str.format, so any
literal brace in it has to be escaped, or loading raises KeyError)."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

from sfh import skills  # noqa: E402


class BuiltinSkillTest(unittest.TestCase):
    def test_builtin_skill_loads(self):
        with tempfile.TemporaryDirectory() as cwd:
            text = skills.load(skills.BUILTIN_NAME, cwd, {}, "/x/harness.html")
        self.assertIsNotNone(text)
        self.assertIn("sh '/x/harness.html' settings", text)
        self.assertIn(skills.BUILTIN_DESCRIPTION, text)
        self.assertIn('model_overrides ({provider id or "*": {model id: {"context_window": int, "efforts": [levels]}}}', text)
        self.assertNotIn("{html}", text)
        self.assertNotIn("{{", text)


if __name__ == "__main__":
    unittest.main()
