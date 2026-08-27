from pathlib import Path
import unittest


class LineEndingTests(unittest.TestCase):
    def test_source_files_are_normalized_to_lf(self):
        root = Path(__file__).resolve().parents[1]
        roots = (root / "app", root / "tests", root / "scripts", root / "docs")
        suffixes = {".py", ".md", ".txt", ".ps1", ".json", ".yml", ".yaml"}
        offenders = []
        for folder in roots:
            for path in folder.rglob("*"):
                if path.is_file() and path.suffix.lower() in suffixes and b"\r\n" in path.read_bytes():
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [], f"CRLF source files: {offenders}")


if __name__ == "__main__":
    unittest.main()
