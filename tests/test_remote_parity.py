import json
import subprocess
import unittest

from tests import remote_parity


class GoldenParityTests(unittest.TestCase):
    def test_committed_worker_fixture_matches_local_dispatch(self):
        self.assertEqual(remote_parity.GOLDEN.read_text(encoding="utf-8"), remote_parity.render(),
                         "regenerate with: python3 tests/remote_parity.py")

    def test_fixtures_cover_every_published_kind_and_edge_reason(self):
        rendered = remote_parity.generate()
        main, edge = rendered["fixtures"]
        self.assertTrue(all(main["index"]["kinds"][kind] for kind in ("skills", "rules", "mcp_servers", "routines")))
        reasons = {item["reason"] for entry in edge["index"]["kinds"]["skills"] for item in entry.get("omitted", [])}
        self.assertEqual(reasons, {"non-text", "size-cap"})
        names = {case["name"] for case in edge["cases"]}
        self.assertIn("get skill with omitted and capped files", names)
        capped = next(case for case in edge["cases"] if case["name"].startswith("get skill with omitted"))
        payload = capped["expected"]["result"]["content"][0]["text"]
        self.assertTrue(payload["truncated"])

    def test_worker_returns_local_truncation_for_oversized_rule(self):
        _, edge = remote_parity.generate()["fixtures"]
        case = next(case for case in edge["cases"] if case["name"] == "get oversized rule")
        expected = case["expected"]["result"]["content"][0]["text"]
        read = """
import { readFileSync } from "node:fs";
import { itemContents } from "./remote/src/catalog.ts";
const index = JSON.parse(readFileSync(0, "utf8"));
const result = await itemContents({}, index, "rule", "oversized");
process.stdout.write(JSON.stringify(result));
"""
        result = subprocess.run(
            ["node", "--experimental-transform-types", "--input-type=module", "-e", read],
            cwd=remote_parity.ROOT, input=json.dumps(edge["index"]),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)


if __name__ == "__main__":
    unittest.main()
