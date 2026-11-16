import os
import tempfile
import unittest

from agenthub import config as c

GIB = 1 << 30


class SizeTest(unittest.TestCase):
    def test_units_and_percent(self):
        self.assertEqual(c.parse_size('512M', 'x'), 512 << 20)
        self.assertEqual(c.parse_size('1.5GiB', 'x'), int(1.5 * GIB))
        self.assertEqual(c.parse_size(4096, 'x'), 4096)
        self.assertEqual(c.parse_size('25%', 'x', total=8 * GIB), 2 * GIB)

    def test_bad_size_names_the_place(self):
        for bad in ('lots', '', '-1G', True, '5%', -5):
            with self.assertRaises(c.ConfigError) as e:
                c.parse_size(bad, 'f.toml: machine.desktop_reserve')
            self.assertIn('machine.desktop_reserve', str(e.exception))

    def test_durations(self):
        self.assertEqual(c.parse_duration('5m', 'x'), 300)
        self.assertEqual(c.parse_duration('500ms', 'x'), 0.5)
        self.assertEqual(c.parse_duration(3, 'x'), 3)
        for bad in ('soon', -1):
            with self.assertRaises(c.ConfigError):
                c.parse_duration(bad, 'x')
        with self.assertRaises(c.ConfigError):
            c.parse_size('-5%', 'x', total=8 * GIB)


class BuildTest(unittest.TestCase):
    def test_defaults(self):
        cfg = c.build({}, 32 * GIB)
        self.assertEqual(cfg.desktop_reserve, int(32 * GIB * 0.15))
        self.assertEqual(cfg.agent_budget, 32 * GIB - cfg.desktop_reserve)
        self.assertEqual(cfg.classes['p0'].memory_low, 2 * GIB)
        self.assertEqual([cfg.classes[k].cpu_weight for k in c.CLASSES], [1000, 400, 100, 25])
        self.assertEqual(cfg.class_for_path('/nowhere'), ('p2', 'default'))
        self.assertEqual(cfg.kind_for('make all'), ('other', GIB))

    def test_rules(self):
        home = os.path.expanduser('~')
        cfg = c.build({'projects': [{'path': '~/work/**', 'class': 'p1'}],
                       'kinds': [{'name': 'build', 'match': r'\btsc\b', 'estimate': '2G'}]}, 32 * GIB)
        self.assertEqual(cfg.class_for_path(f'{home}/work/api')[0], 'p1')
        self.assertEqual(cfg.class_for_path(f'{home}/work')[0], 'p1')
        self.assertEqual(cfg.class_for_path(f'{home}/workshop')[0], 'p2')
        self.assertEqual(cfg.kind_for('npx tsc -p .'), ('build', 2 * GIB))

    def test_errors_name_the_place(self):
        cases = [
            ({'classes': {'p9': {}}}, 'unknown class'),
            ({'projects': [{'path': '~/x'}]}, 'projects[0]'),
            ({'projects': [{'path': '~/x', 'class': 'high'}]}, "unknown class 'high'"),
            ({'kinds': [{'name': 'k', 'match': '('}]}, 'kinds[0]: bad regular expression'),
            ({'machine': {'desktop_reserve': '40G'}}, 'leaves nothing'),
        ]
        for data, text in cases:
            with self.subTest(text=text), self.assertRaises(c.ConfigError) as e:
                c.build(data, 32 * GIB, 'cfg.toml')
            self.assertIn(text, str(e.exception))
            self.assertIn('cfg.toml', str(e.exception))

    def test_load_missing_and_broken_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(c.load(os.path.join(d, 'none.toml'), 8 * GIB).source, '(defaults)')
            path = os.path.join(d, 'bad.toml')
            with open(path, 'w') as f:
                f.write('[machine]\ndesktop_reserve = \n')
            with self.assertRaises(c.ConfigError) as e:
                c.load(path, 8 * GIB)
            self.assertIn(path, str(e.exception))
            self.assertIn('line 2', str(e.exception))


if __name__ == '__main__':
    unittest.main()
