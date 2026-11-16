import unittest

from agenthub import config as c
from agenthub.install import unit_quote, units

GIB = 1 << 30


class UnitsTest(unittest.TestCase):
    def test_slices_carry_class_weights_and_budget(self):
        cfg = c.build({'machine': {'desktop_reserve': '2G'}}, 8 * GIB)
        out = units('agenthubtest', cfg, python='/usr/bin/python3', root='/src/agenthub')
        self.assertEqual(sorted(out), ['agenthubtest-p0.slice', 'agenthubtest-p1.slice', 'agenthubtest-p2.slice',
                                       'agenthubtest-p3.slice', 'agenthubtest.service', 'agenthubtest.slice',
                                       'agenthubtest.socket'])
        self.assertIn(f'MemoryHigh={6 * GIB}\nMemoryMax={int(6 * GIB * 1.1)}\n', out['agenthubtest.slice'])
        # budget × 1.1 would pass the RAM: capped 512 MiB under it
        capped = units('agenthubtest', c.build({'machine': {'desktop_reserve': '600M'}}, 8 * GIB))
        self.assertIn(f'MemoryMax={8 * GIB - (512 << 20)}\n', capped['agenthubtest.slice'])
        # a reserve under 512 MiB: the cap would fall below the budget, so the budget wins
        tiny = units('agenthubtest', c.build({'machine': {'desktop_reserve': '256M'}}, 8 * GIB))
        budget = 8 * GIB - (256 << 20)
        self.assertIn(f'MemoryHigh={budget}\nMemoryMax={budget}\n', tiny['agenthubtest.slice'])
        self.assertIn('CPUWeight=1000\nMemoryLow=2147483648', out['agenthubtest-p0.slice'])
        self.assertIn('CPUWeight=25\n', out['agenthubtest-p3.slice'])
        self.assertIn('ListenStream=%t/agenthubtest.sock\nSocketMode=0600', out['agenthubtest.socket'])
        self.assertIn('RestartPreventExitStatus=78', out['agenthubtest.service'])

    def test_paths_are_quoted_for_systemd(self):
        self.assertEqual(unit_quote('/a b/%h "q"'), '"/a b/%%h \\"q\\""')
        out = units('agenthubtest', c.build({}, 8 * GIB), python='/opt/py 3/python3', root='/src/my repo')
        self.assertIn('ExecStart="/opt/py 3/python3" -P -m agenthub hub', out['agenthubtest.service'])
        self.assertIn('Environment="PYTHONPATH=/src/my repo" AGENTHUB_PREFIX=agenthubtest', out['agenthubtest.service'])


if __name__ == '__main__':
    unittest.main()
