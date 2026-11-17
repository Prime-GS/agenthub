"""Configuration: defaults, TOML loading, sizes and durations, project and kind rules."""
import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass, field

CLASSES = ('p0', 'p1', 'p2', 'p3')
DEFAULT_CLASS = 'p2'
DEFAULT_WEIGHTS = {'p0': 1000, 'p1': 400, 'p2': 100, 'p3': 25}
UNITS = {'': 1, 'K': 1 << 10, 'M': 1 << 20, 'G': 1 << 30, 'T': 1 << 40}
SIZE = re.compile(r'^(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?$', re.I)
DURATION = re.compile(r'^(\d+(?:\.\d+)?)\s*(ms|s|m|h)?$')
SECONDS = {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600, None: 1}
# used when the config has no `kinds` key at all: without them every command is light and an integration hook
# routes nothing. A `[[kinds]]` list of one's own replaces them; `kinds = []` turns them off. First match wins.
# The tool has to stand where a command starts: at the line start or after an operator, past `VAR=value`
# assignments, wrappers and a path — so `git commit -m 'fix make'` or `pip install pytest` stay light.
_START = (r'(?:^|[;&|(])\s*(?:\w+=\S*\s+)*'
          r'(?:(?:env|nice|nohup|time|exec|xvfb-run|timeout\s+\S+|npx|bunx|(?:uv|poetry|pipenv|pdm|hatch)\s+run'
          r'|(?:pnpm|yarn)\s+(?:exec|dlx))\s+|(?:ba|z)?sh\s+-c\s+["\']?)*(?:[\w.~-]*/)*')
_END = r'(?=$|[\s;&|)\'":])'
_NPM = r'(?:npm|pnpm|yarn|bun)(?:\s+-\S+)*\s+(?:run\s+)?'
DEFAULT_KINDS = [
    {'name': 'browser', 'estimate': '1.5G', 'match': _START + r'(?:playwright|puppeteer|cypress run)' + _END},
    {'name': 'test', 'estimate': '1.5G',
     'match': _START + r'(?:jest|vitest|pytest|mocha|rspec|phpunit|go test|cargo test|python3? -m (?:pytest|unittest)'
              r'|(?:gradlew?|mvnw?) test|' + _NPM + r'test)' + _END},
    {'name': 'build', 'estimate': '2G',   # not `docker build`: its memory is charged to the Docker daemon
     'match': _START + r'(?:tsc|webpack|rollup|vite build|next build|cargo build|go build|gradlew?|mvnw?|make'
              r'|cmake --build|ninja|nest build|' + _NPM + r'build)' + _END},
]


class ConfigError(Exception):
    pass


def parse_size(value, where, total=None):
    """Bytes from 1073741824, "512M", "4G", "1.5GiB" or "15%" (of total RAM)."""
    if isinstance(value, bool):
        raise ConfigError(f'{where}: expected a size, got {value!r}')
    size = value if isinstance(value, int) else size_from_text(str(value).strip(), total)
    if size is None or size < 0:
        raise ConfigError(f'{where}: expected a size like "512M", "4G" or "15%", got {value!r}')
    return size


def size_from_text(text, total):
    if text.endswith('%') and total is not None:
        try:
            return int(total * float(text[:-1]) / 100)
        except ValueError:
            return None
    m = SIZE.match(text)
    return int(float(m.group(1)) * UNITS[m.group(2).upper()]) if m else None


def parse_duration(value, where):
    """Seconds from 30, "500ms", "10s", "5m" or "1h"."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < 0:
            raise ConfigError(f'{where}: expected a duration like "10s" or "5m", got {value!r}')
        return float(value)
    m = DURATION.match(str(value).strip())
    if not m:
        raise ConfigError(f'{where}: expected a duration like "10s" or "5m", got {value!r}')
    return float(m.group(1)) * SECONDS[m.group(2)]


def format_size(n):
    if n is None:
        return '-'
    sign, n = ('-', -n) if n < 0 else ('', n)
    if n >= 1 << 30:
        return f'{sign}{n / (1 << 30):.1f} GiB'
    return f'{sign}{n / (1 << 20):.0f} MiB'


@dataclass
class ClassSpec:
    name: str
    cpu_weight: int
    memory_low: int | None = None
    preemptible: bool = True


@dataclass
class ProjectRule:
    pattern: str
    cls: str

    def matches(self, path):
        base = self.pattern[:-3] if self.pattern.endswith('/**') else None
        return fnmatch.fnmatchcase(path, self.pattern) or (base is not None and path == base)


@dataclass
class Kind:
    name: str
    pattern: re.Pattern | None
    estimate: int
    pools: tuple = ()


@dataclass
class Config:
    total_ram: int
    desktop_reserve: int
    agent_budget: int
    cpu_weight: int = 80
    default_estimate: int = 1 << 30
    idle_exit: float = 600.0
    aging_step: float = 300.0
    system_pressure: bool = True
    classes: dict = field(default_factory=dict)
    pools: dict = field(default_factory=dict)
    projects: list = field(default_factory=list)
    kinds: list = field(default_factory=list)
    source: str = '(defaults)'

    def class_for_path(self, path):
        """(class, why) for a session working in `path`."""
        path = os.path.realpath(path or '/')
        for rule in self.projects:
            if rule.matches(path):
                return rule.cls, f'project rule {rule.pattern}'
        return DEFAULT_CLASS, 'default'

    def kind_for(self, cmd):
        """The first kind whose rule matches the command line; `other` with the global default when none does."""
        for kind in self.kinds:
            if kind.pattern.search(cmd):
                return kind
        return Kind('other', None, self.default_estimate)

    def kind_named(self, name):
        if name == 'other':
            return Kind('other', None, self.default_estimate)
        for kind in self.kinds:
            if kind.name == name:
                return kind
        return None


def total_memory():
    with open('/proc/meminfo') as f:
        for line in f:
            if line.startswith('MemTotal:'):
                return int(line.split()[1]) * 1024
    raise ConfigError('/proc/meminfo has no MemTotal')


def default_path():
    explicit = os.environ.get('AGENTHUB_CONFIG')
    if explicit:
        return explicit
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'agenthub', 'config.toml')


def load(path=None, total=None):
    """Config from `path` (default: $AGENTHUB_CONFIG or ~/.config/agenthub/config.toml); a missing file means defaults."""
    path = path or default_path()
    total = total or total_memory()
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
        source = path
    except FileNotFoundError:
        data, source = {}, '(defaults)'
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{path}: {e}') from e
    except OSError as e:
        raise ConfigError(f'{path}: {e.strerror}') from e
    return build(data, total, source)


def expect(value, kind, where, what):
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise ConfigError(f'{where}: expected {what}, got {value!r}')
    return value


def weight(value, where):
    """A cgroup CPU weight: an integer from 1 to 10000."""
    if not 1 <= expect(value, int, where, 'an integer weight') <= 10000:
        raise ConfigError(f'{where}: a weight is 1..10000, got {value}')
    return value


def build(data, total, source='(defaults)'):
    where = source
    machine = expect(data.get('machine', {}), dict, f'{where}: [machine]', 'a table')
    reserve = parse_size(machine.get('desktop_reserve', '15%'), f'{where}: machine.desktop_reserve', total)
    budget_value = machine.get('agent_budget', 'auto')
    budget = total - reserve if budget_value == 'auto' else parse_size(budget_value, f'{where}: machine.agent_budget', total)
    if budget <= 0:
        raise ConfigError(f'{where}: machine.agent_budget is {budget} bytes — desktop_reserve leaves nothing for agents')
    cfg = Config(
        total_ram=total,
        desktop_reserve=reserve,
        agent_budget=budget,
        cpu_weight=weight(machine.get('cpu_weight', 80), f'{where}: machine.cpu_weight'),
        default_estimate=parse_size(machine.get('default_estimate', '1G'), f'{where}: machine.default_estimate', total),
        idle_exit=parse_duration(machine.get('idle_exit', '10m'), f'{where}: machine.idle_exit'),
        aging_step=parse_duration(machine.get('aging_step', '5m'), f'{where}: machine.aging_step'),
        system_pressure=expect(machine.get('system_pressure', True), bool, f'{where}: machine.system_pressure',
                               'true or false'),
        source=source,
    )
    classes = expect(data.get('classes', {}), dict, f'{where}: [classes]', 'a table')
    unknown = set(classes) - set(CLASSES)
    if unknown:
        raise ConfigError(f'{where}: classes: unknown class {sorted(unknown)[0]!r}, expected one of {", ".join(CLASSES)}')
    for name in CLASSES:
        spec = expect(classes.get(name, {}), dict, f'{where}: classes.{name}', 'a table')
        low = spec.get('memory_low', '2G' if name == 'p0' else None)
        cfg.classes[name] = ClassSpec(
            name=name,
            cpu_weight=weight(spec.get('cpu_weight', DEFAULT_WEIGHTS[name]), f'{where}: classes.{name}.cpu_weight'),
            memory_low=parse_size(low, f'{where}: classes.{name}.memory_low', total) if low is not None else None,
            preemptible=expect(spec.get('preemptible', name != 'p0'), bool, f'{where}: classes.{name}.preemptible',
                               'true or false'),
        )
    for name, size in expect(data.get('pools', {}), dict, f'{where}: [pools]', 'a table').items():
        if not 1 <= expect(size, int, f'{where}: pools.{name}', 'a whole number of units') <= 1000:
            raise ConfigError(f'{where}: pools.{name}: a pool has 1..1000 units, got {size}')
        cfg.pools[name] = size
    for i, rule in enumerate(expect(data.get('projects', []), list, f'{where}: projects', 'a list of [[projects]]')):
        at = f'{where}: projects[{i}]'
        if not isinstance(rule, dict) or 'path' not in rule or 'class' not in rule:
            raise ConfigError(f'{at}: needs both "path" and "class"')
        expect(rule['path'], str, f'{at}.path', 'a path')
        if rule['class'] not in CLASSES:
            raise ConfigError(f'{at}: unknown class {rule["class"]!r}')
        cfg.projects.append(ProjectRule(os.path.expanduser(rule['path']), rule['class']))
    for i, kind in enumerate(expect(data.get('kinds', DEFAULT_KINDS), list, f'{where}: kinds', 'a list of [[kinds]]')):
        at = f'{where}: kinds[{i}]'
        if not isinstance(kind, dict) or 'name' not in kind or 'match' not in kind:
            raise ConfigError(f'{at}: needs both "name" and "match"')
        expect(kind['name'], str, f'{at}.name', 'a name')
        expect(kind['match'], str, f'{at}.match', 'a regular expression')
        try:
            pattern = re.compile(kind['match'])
        except re.error as e:
            raise ConfigError(f'{at}: bad regular expression: {e}') from e
        estimate = parse_size(kind.get('estimate', cfg.default_estimate), f'{at}.estimate', total)
        pools = expect(kind.get('pools', []), list, f'{at}.pools', 'a list of pool names')
        for pool in pools:
            if pool not in cfg.pools:
                raise ConfigError(f'{at}.pools: unknown pool {pool!r}; declare it under [pools]')
        cfg.kinds.append(Kind(kind['name'], pattern, estimate, tuple(pools)))
    return cfg
