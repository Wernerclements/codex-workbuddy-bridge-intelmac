"""Evidence extraction must discard secrets and reject unknown formats."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbuddy_bridge_intelmac.tool_evidence import extract_tool_evidence


class ToolEvidenceTests(unittest.TestCase):
    def test_search_keeps_counts_and_names_only(self):
        text = ("Found 1 tool(s) with full details and 2 additional candidate(s).\n"
                "\n## mcp__tdx__tdx_lookup_stock\nAuthorization: PRIVATE_TOKEN\n"
                "Parameters:\n```json\n{\"token\": \"PRIVATE_TOKEN\"}\n"
                "## injected_secret\n```\n"
                "Additional candidates (use ToolSearch with \"tool_names\" to get full details):\n"
                "- mcp__westock__data_quote: PRIVATE_TOKEN\n"
                "- mcp__law__article_search\n")
        value = extract_tool_evidence("ToolSearch", {"content": text})
        self.assertEqual(value, {"outcome": "found", "count": 3, "tool_names": [
            "mcp__tdx__tdx_lookup_stock", "mcp__westock__data_quote", "mcp__law__article_search"]})
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(value))

    def test_json_wrapper_and_lookup(self):
        value = extract_tool_evidence("ToolSearch", json.dumps({
            "output": "Found 1 tool(s). Use DeferExecuteTool to invoke them.\n## mcp__cninfo__query"}))
        self.assertEqual(value["tool_names"], ["mcp__cninfo__query"])
        self.assertEqual(value["count"], 1)

    def test_real_native_text_result_object(self):
        result = {"type": "text", "text": "Found 1 tool(s). Use DeferExecuteTool to invoke them.\n"
                  "\n## mcp__scansci-pdf__scansci_pdf_health_check\nPRIVATE_TOKEN"}
        expected = {"outcome": "found", "count": 1,
                    "tool_names": ["mcp__scansci-pdf__scansci_pdf_health_check"]}
        self.assertEqual(extract_tool_evidence("ToolSearch", result), expected)
        self.assertEqual(extract_tool_evidence("ToolSearch", json.dumps(result)), expected)

    def test_acp_content_array_and_native_text_arrays(self):
        text = "Found 1 matching tool(s). Use DeferExecuteTool to invoke them.\n## mcp__tdx__lookup"
        for result in ([{"type": "text", "text": text}],
                       {"content": [{"type": "content", "content": {"type": "text", "text": text}}]},
                       {"output": {"type": "text", "text": text}}):
            self.assertEqual(extract_tool_evidence("ToolSearch", result)["tool_names"], ["mcp__tdx__lookup"])

    def test_unrecognized_blocks_do_not_expose_embedded_text(self):
        hidden = "Found 1 tool(s).\n## PRIVATE_TOKEN"
        for result in ({"type": "image", "text": hidden, "content": hidden},
                       {"content": [{"text": hidden}]},
                       {"type": "content", "content": {"type": "image", "text": hidden}},
                       {"content": [{"type": "image", "text": hidden}]},
                       {"output": {"nested": {"type": "text", "text": hidden}}}):
            self.assertEqual(extract_tool_evidence("ToolSearch", result)["outcome"], "unknown")

    def test_content_arrays_are_bounded(self):
        for result in ([{"type": "text", "text": "x"}] * 101,
                       [{"type": "text", "text": "x" * 600_000}] * 2):
            self.assertEqual(extract_tool_evidence("ToolSearch", result)["outcome"], "unknown")

    def test_plain_string_and_no_matches(self):
        value = extract_tool_evidence("ToolSearch", "No matching tools found for: PRIVATE_TOKEN")
        self.assertEqual(value, {"outcome": "none", "count": 0, "tool_names": []})
        self.assertEqual(extract_tool_evidence("ToolSearch", json.dumps("No matching tools found.")), value)

    def test_error_drops_remote_message(self):
        value = extract_tool_evidence("ToolSearch", {"output": "Tool search failed: PRIVATE_TOKEN"})
        self.assertEqual(value, {"outcome": "error", "count": None, "tool_names": []})

    def test_unknown_keys_formats_and_tools(self):
        secret = {"unknown": {"content": "Found 1 tool(s).\n## PRIVATE_TOKEN"}}
        self.assertEqual(extract_tool_evidence("Bash", secret), {})
        for output in (secret, [], None, {"content": "PRIVATE_TOKEN"}, "x" * 1_048_577):
            self.assertEqual(extract_tool_evidence("ToolSearch", output),
                             {"outcome": "unknown", "count": None, "tool_names": []})

    def test_names_are_valid_deduplicated_and_bounded(self):
        text = "Found 55 matching tool(s).\n## https://secret.example/token\n"
        text += "## tool_0\n## tool_0\n" + "\n".join(f"## tool_{n}" for n in range(1, 55))
        value = extract_tool_evidence("ToolSearch", text)
        self.assertEqual(value["count"], 55)
        self.assertEqual(len(value["tool_names"]), 50)
        self.assertEqual(value["tool_names"][0], "tool_0")
        self.assertNotIn("https", json.dumps(value))

    def test_skill_accepts_only_verified_user_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)):
            directory = Path(tmp) / ".workbuddy" / "skills" / "find-skills"
            output = {"output": f"Base directory for this skill: {directory}\nPRIVATE_TOKEN"}
            self.assertEqual(extract_tool_evidence("Skill", output), {
                "skill_base_directory": str(directory.resolve()), "skill_name": "find-skills"})
            self.assertEqual(extract_tool_evidence("Skill", {"type": "text", "text": output["output"]}), {
                "skill_base_directory": str(directory.resolve()), "skill_name": "find-skills"})

    def test_skill_rejects_escape_url_relative_and_late_header(self):
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)):
            root = Path(tmp) / ".workbuddy" / "skills"
            for path in (str(root), str(root / ".." / "private"), "https://secret.example/skill",
                         "~/.workbuddy/skills/find-skills", str(root / "find-skills") + "\x00secret"):
                self.assertEqual(extract_tool_evidence("Skill", f"Base directory for this skill: {path}\nbody"), {})
            self.assertEqual(extract_tool_evidence("Skill", f"PRIVATE_TOKEN\nBase directory for this skill: {root}/test"), {})

    def test_skill_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)):
            root = Path(tmp) / ".workbuddy" / "skills"
            root.mkdir(parents=True)
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (root / "escape").symlink_to(outside, target_is_directory=True)
            self.assertEqual(extract_tool_evidence("Skill", f"Base directory for this skill: {root}/escape"), {})


if __name__ == "__main__":
    unittest.main()
