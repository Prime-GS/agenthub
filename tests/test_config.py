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
        self.assertEqual((cfg.kind_for('make all').name, cfg.kind_for('make all').estimate), ('build', 2 * GIB))
        self.assertEqual([k.name for k in cfg.kinds], ['browser', 'test', 'build'])   # built in, first match wins
        self.assertEqual(cfg.kind_for('npx playwright test').name, 'browser')
        self.assertEqual(cfg.kind_for('ls -la').name, 'other')
        heavy = {'cd api && make': 'build', '/usr/bin/make -j8': 'build', './gradlew test': 'test',
                 'npm run test:unit': 'test', 'npm run build:prod': 'build', 'pnpm -r build': 'build',
                 'NODE_ENV=production npm run build': 'build', 'time cargo build --release': 'build',
                 'timeout 600 npm test': 'test', 'uv run pytest -x': 'test', 'python3 -m unittest -v': 'test',
                 "bash -c 'make all'": 'build', '(cd web; npx vitest run)': 'test',
                 'npm test 2>&1 | tail -20': 'test', 'git pull && npx playwright test': 'browser'}
        for line, kind in heavy.items():
            self.assertEqual(cfg.kind_for(line).name, kind, line)
        for line in ("git commit -m 'fix make test'", 'pip install pytest', 'rg jest src', 'which tsc', 'man make',
                     'echo make', 'cat Makefile', 'vim webpack.config.js', 'docker build .', 'maker', 'npm install'):
            self.assertEqual(cfg.kind_for(line).name, 'other', line)
        self.assertEqual(c.build({'kinds': []}, 32 * GIB).kind_for('make all').name, 'other')   # turned off
        own = c.build({'kinds': [{'name': 'e2e', 'match': 'cypress'}]}, 32 * GIB)
        self.assertEqual([k.name for k in own.kinds], ['e2e'])   # one's own list replaces the built-in one

    def test_rules(self):
        home = os.path.expanduser('~')
        cfg = c.build({'projects': [{'path': '~/work/**', 'class': 'p1'}],
                       'kinds': [{'name': 'build', 'match': r'\btsc\b', 'estimate': '2G'}]}, 32 * GIB)
        self.assertEqual(cfg.class_for_path(f'{home}/work/api')[0], 'p1')
        self.assertEqual(cfg.class_for_path(f'{home}/work')[0], 'p1')
        self.assertEqual(cfg.class_for_path(f'{home}/workshop')[0], 'p2')
        self.assertEqual((cfg.kind_for('npx tsc -p .').name, cfg.kind_for('npx tsc -p .').estimate), ('build', 2 * GIB))

    def test_m2_settings(self):
        cfg = c.build({'machine': {'aging_step': '1m', 'system_pressure': False},
                       'classes': {'p0': {'preemptible': True}, 'p3': {'preemptible': False}},
                       'pools': {'browser': 2},
                       'kinds': [{'name': 'e2e', 'match': 'playwright', 'pools': ['browser']}]}, 8 * GIB)
        self.assertEqual((cfg.aging_step, cfg.system_pressure, cfg.pools), (60.0, False, {'browser': 2}))
        self.assertEqual([cfg.classes[k].preemptible for k in c.CLASSES], [True, True, True, False])
        self.assertEqual(cfg.kind_for('npx playwright test').pools, ('browser',))
        defaults = c.build({}, 8 * GIB)
        self.assertEqual([defaults.classes[k].preemptible for k in c.CLASSES], [False, True, True, True])
        self.assertEqual((defaults.aging_step, defaults.system_pressure), (300.0, True))
        self.assertIsNone(defaults.kind_named('nope'))
        self.assertEqual(defaults.kind_named('other').estimate, GIB)

    def test_errors_name_the_place(self):
        cases = [
            ({'classes': {'p9': {}}}, 'unknown class'),
            ({'projects': [{'path': '~/x'}]}, 'projects[0]'),
            ({'projects': [{'path': '~/x', 'class': 'high'}]}, "unknown class 'high'"),
            ({'kinds': [{'name': 'k', 'match': '('}]}, 'kinds[0]: bad regular expression'),
            ({'machine': {'desktop_reserve': '40G'}}, 'leaves nothing'),
            ({'pools': {'browser': 0}}, 'pools.browser: a pool has 1..1000 units'),
            ({'pools': {'browser': 1001}}, 'pools.browser: a pool has 1..1000 units'),
            ({'pools': {'browser': '2'}}, 'pools.browser: expected a whole number'),
            ({'pools': {'browser': True}}, 'pools.browser: expected a whole number'),
            ({'pools': ['browser']}, '[pools]: expected a table'),
            ({'classes': {'p1': {'preemptible': 'no'}}}, 'classes.p1.preemptible: expected true or false'),
            ({'machine': {'system_pressure': 1}}, 'machine.system_pressure: expected true or false'),
            ({'machine': {'aging_step': 'soon'}}, 'machine.aging_step'),
            ({'kinds': [{'name': 'k', 'match': 'x', 'pools': ['gpu']}]}, "kinds[0].pools: unknown pool 'gpu'"),
            ({'kinds': [{'name': 'k', 'match': 'x', 'pools': 'gpu'}]}, 'kinds[0].pools: expected a list'),
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
