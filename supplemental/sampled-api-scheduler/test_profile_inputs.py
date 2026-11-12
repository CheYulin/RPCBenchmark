import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from exact_profile import ExactProfile
class Inputs(unittest.TestCase):
 def test_bad_digest_and_arch_fail_before_optional_BCC_import(self):
  with patch('exact_profile.platform.machine',return_value='x86_64'):
   for digest in [None,'','x'*64,'a'*63]:
    with self.subTest(digest=digest),self.assertRaises(ValueError):ExactProfile('/unused','/unused','/unused',digest)
  with patch('exact_profile.platform.machine',return_value='aarch64'),self.assertRaisesRegex(RuntimeError,'x86_64'):
   ExactProfile('/unused','/unused','/unused','0'*64)
 def test_changed_binary_fails_before_output_or_BCC(self):
  with tempfile.TemporaryDirectory()as d,patch('exact_profile.platform.machine',return_value='x86_64'):
   binary=Path(d)/'driver';binary.write_bytes(b'fixture');out=Path(d)/'out'
   with self.assertRaisesRegex(RuntimeError,'driver bytes'):ExactProfile(out,'/unused',binary,'0'*64)
   self.assertFalse(out.exists())
if __name__=='__main__':unittest.main()
