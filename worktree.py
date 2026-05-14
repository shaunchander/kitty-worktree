"""
kitty-worktree: Jump between git worktrees with per-repo session layouts.

Install:
    ln -sf /path/to/worktree.py ~/.config/kitty/worktree.py

Usage in kitty.conf:
    map ctrl+shift+w kitten worktree.py
"""

import json
import os
import re
import select as _select
import shlex
import signal
import subprocess
import sys
import termios
import tty
from pathlib import Path

try:
    import tomllib
except ImportError:
    tomllib = None


# ── Git ──────────────────────────────────────────────────────────────────


def get_worktrees():
    try:
        result = subprocess.run(
            ['git', 'worktree', 'list', '--porcelain'],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []

    worktrees = []
    current = {}
    for line in result.stdout.splitlines():
        if line.startswith('worktree '):
            if current:
                worktrees.append(current)
            current = {'path': line[9:]}
        elif line.startswith('branch '):
            current['branch'] = line[7:].removeprefix('refs/heads/')
        elif line == 'bare':
            current['bare'] = True
        elif line == 'detached':
            current['branch'] = '(detached)'
    if current:
        worktrees.append(current)

    return [w for w in worktrees if not w.get('bare')]


def get_repo_root():
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--git-common-dir'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            common = Path(result.stdout.strip()).resolve()
            if common.name == '.git':
                return str(common.parent)
            return str(common)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


# ── Config ───────────────────────────────────────────────────────────────


DEFAULT_SESSION = {
    'tab_title': '{repo} ({basename})',
    'panes': [
        {'title': 'editor', 'command': 'nvim'},
        {'title': 'shell', 'location': 'hsplit'},
    ],
}


def load_toml(path):
    if tomllib is None:
        return None
    try:
        with open(path, 'rb') as f:
            return tomllib.load(f)
    except Exception:
        return None


def resolve_config(worktree_path, repo_root):
    for base in [worktree_path, repo_root]:
        if not base:
            continue
        cfg = Path(base) / '.kitty-worktree.toml'
        if cfg.exists():
            data = load_toml(cfg)
            if data:
                return data

    central = Path.home() / '.config' / 'kitty' / 'worktree.toml'
    if central.exists():
        config = load_toml(central)
        if config:
            for session in config.get('sessions', []):
                if re.search(session.get('match', ''), worktree_path):
                    return session
            if 'default' in config:
                return config['default']

    return DEFAULT_SESSION


def expand_vars(text, variables):
    def replace(match):
        return variables.get(match.group(1), match.group(0))
    return re.sub(r'\{(\w+)\}', replace, text)


# ── Picker ───────────────────────────────────────────────────────────────

_resized = False


def _on_resize(sig, frame):
    global _resized
    _resized = True


def read_key(fd):
    global _resized
    try:
        ch = os.read(fd, 1).decode('utf-8', errors='replace')
    except OSError:
        if _resized:
            _resized = False
            return 'resize'
        raise
    if ch == '\x1b':
        r, _, _ = _select.select([fd], [], [], 0.05)
        if r:
            seq = os.read(fd, 2).decode('utf-8', errors='replace')
            return {'[A': 'up', '[B': 'down'}.get(seq, 'escape')
        return 'escape'
    if ch in ('\r', '\n'):
        return 'enter'
    if ch in ('\x7f', '\x08'):
        return 'backspace'
    if ch in ('\x03', '\x04'):
        return 'escape'
    return ch


def run_picker(worktrees):
    if len(worktrees) == 1:
        return worktrees[0]

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    max_branch = max(len(w.get('branch', '???')) for w in worktrees)
    home = str(Path.home())

    def fmt(w):
        branch = w.get('branch', '???').ljust(max_branch)
        path = w['path'].replace(home, '~')
        return f'{branch}  {path}'

    entries = [(w, fmt(w)) for w in worktrees]
    selected = 0
    query = ''
    prev_handler = signal.signal(signal.SIGWINCH, _on_resize)

    try:
        tty.setraw(fd)
        sys.stdout.write('\033[?25l')

        while True:
            filtered = (
                [(w, d) for w, d in entries if query.lower() in d.lower()]
                if query else list(entries)
            )
            selected = max(0, min(selected, len(filtered) - 1))

            out = ['\033[2J\033[H']
            out.append(f'  Worktree: {query}\r\n\r\n')
            for i, (_, d) in enumerate(filtered):
                if i == selected:
                    out.append(f'  \033[7m {d} \033[0m\r\n')
                else:
                    out.append(f'   {d}\r\n')
            out.append(f'\r\n\033[2m  up/down navigate  enter select  esc cancel\033[0m')
            sys.stdout.write(''.join(out))
            sys.stdout.flush()

            key = read_key(fd)
            if key == 'resize':
                continue
            elif key == 'up':
                selected = max(0, selected - 1)
            elif key == 'down':
                selected = min(len(filtered) - 1, selected + 1)
            elif key == 'enter' and filtered:
                return filtered[selected][0]
            elif key == 'escape':
                return None
            elif key == 'backspace':
                query = query[:-1]
            elif len(key) == 1 and key.isprintable():
                query += key
    finally:
        sys.stdout.write('\033[?25h\033[2J\033[H')
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        signal.signal(signal.SIGWINCH, prev_handler)


# ── Entry Point ──────────────────────────────────────────────────────────


def main(args):
    if tomllib is None:
        print('kitty-worktree requires Python 3.11+ (tomllib).')
        print('Update kitty to 0.26 or later.')
        input('Press Enter to close...')
        return None

    worktrees = get_worktrees()
    if not worktrees:
        print('Not in a git repo, or no worktrees found.')
        input('Press Enter to close...')
        return None

    selected = run_picker(worktrees)
    if not selected:
        return None

    repo_root = get_repo_root()
    return json.dumps({
        'path': selected['path'],
        'branch': selected.get('branch', 'unknown'),
        'repo_root': repo_root,
    })


# ── Result Handler (runs in kitty's process) ─────────────────────────────


def handle_result(args, answer, target_window_id, boss):
    if not answer:
        return

    data = json.loads(answer)
    wt_path = data['path']
    branch = data['branch']
    repo_root = data['repo_root']
    repo_name = Path(repo_root).name.removesuffix('.git') if repo_root else Path(wt_path).name

    config = resolve_config(wt_path, repo_root)
    variables = {
        'branch': branch,
        'repo': repo_name,
        'path': wt_path,
        'basename': Path(wt_path).name,
    }

    tab_title = expand_vars(config.get('tab_title', '{repo} ({branch})'), variables)
    w = boss.window_id_map.get(target_window_id)
    if not w:
        return

    # Focus existing tab if already open
    escaped = re.escape(tab_title)
    try:
        boss.call_remote_control(w, ('focus-tab', '--match', f'title:^{escaped}$'))
        return
    except Exception:
        pass

    # Create tab with panes from config
    panes = config.get('panes', DEFAULT_SESSION['panes'])
    for i, pane in enumerate(panes):
        title = expand_vars(pane.get('title', ''), variables)
        command = pane.get('command')
        cwd = expand_vars(pane.get('cwd', wt_path), variables)

        cmd = ['launch', '--cwd', cwd]
        if i == 0:
            cmd += ['--type=tab', '--tab-title', tab_title]
        else:
            location = pane.get('location', 'hsplit')
            cmd.append(f'--location={location}')
        if title:
            cmd += ['--title', title]
        if command:
            cmd += shlex.split(expand_vars(command, variables))

        boss.call_remote_control(w, tuple(cmd))
