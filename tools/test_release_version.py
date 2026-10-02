"""Run with: python3 -m unittest discover -s tools -p test_release_version.py"""

import unittest

from release_version import parse, release_state, select


class ReleaseVersionTests(unittest.TestCase):
    def test_versions_and_retries(self):
        self.assertEqual(select("main", {}, "a"), ("v0.1.0", "latest", False))
        self.assertEqual(select("develop", {}, "a"), ("v0.1.0-dev.1", "dev", False))
        tags = {"v0.1.0": "a", "v0.1.1-dev.1": "b", "unrelated": "z"}
        self.assertEqual(select("main", tags, "a"), ("v0.1.0", "latest", True))
        self.assertEqual(select("main", tags, "c"), ("v0.1.1", "latest", False))
        self.assertEqual(select("develop", tags, "c"), ("v0.1.1-dev.2", "dev", False))
        self.assertEqual(select("develop", tags, "b"), ("v0.1.1-dev.1", "dev", True))
        tags["v0.1.1"] = "c"
        self.assertEqual(select("develop", tags, "d"), ("v0.1.2-dev.1", "dev", False))
        self.assertEqual(select("main", {"v0.1.9": "a", "v0.1.10": "b"}, "c")[0], "v0.1.11")
        self.assertIsNone(parse("v0.1.0-other.1"))
        self.assertIsNone(parse("v0.1.0-dev.1-extra"))

    def test_completed_releases_and_channel_rollback(self):
        releases = [{"tag_name": "v0.1.0", "draft": False},
                    {"tag_name": "v0.1.1-dev.2", "draft": False},
                    {"tag_name": "v0.1.2", "draft": True}]
        self.assertEqual(release_state("v0.1.0", releases), (True, True))
        self.assertEqual(release_state("v0.1.1", releases), (False, True))
        self.assertEqual(release_state("v0.1.1-dev.1", releases), (False, False))
        releases.append({"tag_name": "v0.1.1", "draft": False})
        self.assertEqual(release_state("v0.1.0", releases), (True, False))
        self.assertIsNone(parse("v0.1.0-dev.0"))
        self.assertIsNone(parse("v00.1.0"))


if __name__ == "__main__":
    unittest.main()
