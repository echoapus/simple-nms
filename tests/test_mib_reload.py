import os
import sys
import tempfile
import unittest
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "simplenms"))
from collectors.snmp_listener import MibResolver, _PYSNMP_OK


@unittest.skipUnless(_PYSNMP_OK, "pysnmp is required")
class MibReloadTest(unittest.TestCase):
    def test_upload_and_replace_mib(self):
        from pysnmp.entity import engine

        mib = '''TEST-VENDOR-MIB DEFINITIONS ::= BEGIN
IMPORTS enterprises, OBJECT-TYPE, Integer32 FROM SNMPv2-SMI;
testVendor OBJECT IDENTIFIER ::= { enterprises 9926 }
%s OBJECT-TYPE SYNTAX Integer32 MAX-ACCESS read-only STATUS current
    DESCRIPTION "Test value" ::= { testVendor 1 }
END
'''
        oid = "1.3.6.1.4.1.9926.1"
        with tempfile.TemporaryDirectory() as directory:
            resolver = MibResolver(engine.SnmpEngine(), mib_dirs=[directory], mib_modules=["SNMPv2-MIB"])
            self.assertEqual(resolver.resolve(oid), oid)
            path = os.path.join(directory, "TEST-VENDOR-MIB.txt")
            with open(path, "w") as f:
                f.write(mib % "firstValue")
            resolver.reload_discovered_mibs(directory, "TEST-VENDOR-MIB")
            self.assertEqual(resolver.resolve(oid), "TEST-VENDOR-MIB::firstValue")
            with open(path, "w") as f:
                f.write(mib % "secondValue")
            resolver.reload_discovered_mibs(directory, "TEST-VENDOR-MIB")
            self.assertEqual(resolver.resolve(oid), "TEST-VENDOR-MIB::secondValue")

            with open(path, "w") as f:
                f.write(mib.replace("SNMPv2-SMI", "MISSING-VENDOR-MIB") % "brokenValue")
            with self.assertRaisesRegex(ValueError, "MISSING-VENDOR-MIB"):
                resolver.reload_discovered_mibs(directory, "TEST-VENDOR-MIB")
            self.assertEqual(resolver.resolve(oid), "TEST-VENDOR-MIB::secondValue")

    def test_build_does_not_block_resolution(self):
        from pysnmp.entity import engine

        with tempfile.TemporaryDirectory() as directory:
            resolver = MibResolver(engine.SnmpEngine(), mib_dirs=[directory], mib_modules=["SNMPv2-MIB"])
            oid = "1.3.6.1.4.1.9926.1"
            resolver.resolve(oid)
            started, release = threading.Event(), threading.Event()

            def slow_build(*args):
                started.set()
                release.wait(5)
                raise ValueError("test")

            def reload():
                try:
                    resolver.reload_discovered_mibs(directory, "TEST-VENDOR-MIB")
                except ValueError:
                    pass

            with patch("collectors.snmp_listener.MibResolver", side_effect=slow_build):
                worker = threading.Thread(target=reload)
                worker.start()
                self.assertTrue(started.wait(2))
                try:
                    self.assertTrue(resolver._lock.acquire(timeout=1))
                    resolver._lock.release()
                    self.assertEqual(resolver.resolve(oid), oid)
                finally:
                    release.set()
                    worker.join(5)


if __name__ == "__main__":
    unittest.main()
