import os
from pathlib import Path
import tempfile
import unittest

from generate_core_variants import BASE_SHA256, INLINE_SHA256, BRANCH, digest, variants, write_variants

SOURCE = Path(os.environ['DS_CORE_SOURCE_FILE']).resolve(strict=True)


class CoreGenerationTests(unittest.TestCase):
    def test_exact_source_produces_the_reviewed_pair(self):
        result = variants(SOURCE.read_bytes())
        self.assertEqual(digest(result['base-private-core.cpp']), BASE_SHA256)
        self.assertEqual(digest(result['inline-private-core.cpp']), INLINE_SHA256)
        self.assertEqual(result['inline-private-core.cpp'].replace(BRANCH.encode(), b'', 1),
                         result['base-private-core.cpp'])

    def test_changed_source_is_rejected_before_output(self):
        with self.assertRaisesRegex(ValueError, 'source SHA256 mismatch'):
            variants(SOURCE.read_bytes() + b'\n')

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / 'base-private-core.cpp'
            target.write_bytes(b'existing evidence')
            with self.assertRaisesRegex(ValueError, 'paths must be new'):
                write_variants(SOURCE, Path(scratch))
            self.assertEqual(target.read_bytes(), b'existing evidence')
            self.assertFalse((Path(scratch) / 'inline-private-core.cpp').exists())

    def test_disk_outputs_match_exact_source_hashes(self):
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            result = write_variants(SOURCE, output)
            self.assertEqual({name: digest((output / name).read_bytes()) for name in result}, result)


if __name__ == '__main__':
    unittest.main()
