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
        {'title': 'claude', 'location': 'vsplit'},
        {'title': 'terminal', 'location': 'hsplit'},
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


def parse_session_file(session_file_path):
    """
    Parse a kitty session .conf file and convert to internal panes format.

    Returns dict with 'tab_title' and 'panes' keys, or None if parsing fails.
    """
    try:
        path = Path(os.path.expanduser(session_file_path))
        if not path.exists():
            return None

        with open(path) as f:
            lines = f.readlines()

        tab_title = None
        panes = []
        current_cwd = None

        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            if line.startswith('new_tab '):
                tab_title = line[8:].strip()
            elif line.startswith('cd '):
                current_cwd = os.path.expanduser(line[3:].strip())
            elif line.startswith('launch'):
                # Parse launch command
                args = line[6:].strip()

                # Extract location if present
                location = None
                if '--location=vsplit' in args:
                    location = 'vsplit'
                    args = args.replace('--location=vsplit', '').strip()
                elif '--location=hsplit' in args:
                    location = 'hsplit'
                    args = args.replace('--location=hsplit', '').strip()

                # The remaining args are the command
                pane = {}
                if location:
                    pane['location'] = location
                if current_cwd:
                    pane['cwd'] = current_cwd
                if args:
                    pane['command'] = args

                panes.append(pane)

        result = {}
        if tab_title:
            result['tab_title'] = tab_title
        if panes:
            result['panes'] = panes

        return result if result else None

    except Exception:
        return None


def resolve_config(worktree_path, repo_root):
    # Check for local .kitty-worktree.toml first
    for base in [worktree_path, repo_root]:
        if not base:
            continue
        cfg = Path(base) / '.kitty-worktree.toml'
        if cfg.exists():
            data = load_toml(cfg)
            if data:
                # Check if it references a session_file
                if 'session_file' in data:
                    parsed = parse_session_file(data['session_file'])
                    if parsed:
                        return parsed
                return data

    # Check central config
    central = Path.home() / '.config' / 'kitty' / 'worktree.toml'
    if central.exists():
        config = load_toml(central)
        if config:
            # Check matched sessions
            for session in config.get('sessions', []):
                if re.search(session.get('match', ''), worktree_path):
                    # Check if session references a session_file
                    if 'session_file' in session:
                        parsed = parse_session_file(session['session_file'])
                        if parsed:
                            return parsed
                    return session
            # Check default config
            if 'default' in config:
                default_config = config['default']
                # Check if default references a session_file
                if 'session_file' in default_config:
                    parsed = parse_session_file(default_config['session_file'])
                    if parsed:
                        return parsed
                return default_config

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
    if ch == 'j':
        return 'down'
    if ch == 'k':
        return 'up'
    if ch == '/':
        return 'search'
    return ch


def create_worktree(name, repo_root):
    """Create a new git worktree."""
    if not name or not name.strip():
        return None

    # Sanitize the name
    name = name.strip().replace(' ', '-')

    # Determine worktree location - sibling to repo root
    repo_path = Path(repo_root)
    worktree_path = repo_path.parent / name

    # Create the worktree with a new branch
    try:
        result = subprocess.run(
            ['git', 'worktree', 'add', str(worktree_path), '-b', name],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return {'path': str(worktree_path), 'branch': name}
        else:
            # If branch exists, try without -b
            result = subprocess.run(
                ['git', 'worktree', 'add', str(worktree_path), name],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return {'path': str(worktree_path), 'branch': name}
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass

    return None


def run_create_prompt(repo_root):
    """Show prompt to create a new worktree when none exist."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    query = ''
    prev_handler = signal.signal(signal.SIGWINCH, _on_resize)

    try:
        tty.setraw(fd)
        sys.stdout.write('\033[?25h')  # Show cursor

        while True:
            term_width = os.get_terminal_size().columns

            lines = [
                'No worktrees found',
                '',
                f'Create new worktree: {query}',
                '',
                'enter create  esc cancel'
            ]

            max_line = max(len(line) for line in lines)
            left_margin = max(0, (term_width - max_line) // 2)

            out = ['\033[2J\033[H\r\n']
            out.append(f'{" " * left_margin}\033[1mNo worktrees found\033[0m\r\n\r\n')
            out.append(f'{" " * left_margin}Create new worktree: \033[7m{query}\033[0m\r\n\r\n')
            out.append(f'{" " * left_margin}\033[2menter create  esc cancel\033[0m')
            sys.stdout.write(''.join(out))
            sys.stdout.flush()

            key = read_key(fd)
            if key == 'resize':
                continue
            elif key == 'enter':
                if query.strip():
                    result = create_worktree(query, repo_root)
                    return result
            elif key == 'escape':
                return None
            elif key == 'backspace':
                query = query[:-1]
            elif len(key) == 1 and key.isprintable() and key not in ('j', 'k'):
                query += key
    finally:
        sys.stdout.write('\033[?25h\033[2J\033[H')
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        signal.signal(signal.SIGWINCH, prev_handler)


def run_picker(worktrees, repo_root):
    if len(worktrees) == 1:
        return worktrees[0]

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    # Calculate max widths for alignment
    max_name = max(len(Path(w['path']).name) for w in worktrees) if worktrees else 20

    def fmt(w):
        if w.get('_create'):
            return '\033[2m[Create new worktree...]\033[0m'
        name = Path(w['path']).name.ljust(max_name)
        branch = w.get('branch', '???')
        return f'{name}  →  {branch}'

    # Add create option at the top
    create_entry = {'_create': True}
    all_entries = [create_entry] + worktrees
    entries = [(w, fmt(w)) for w in all_entries]

    selected = 0
    query = ''
    search_mode = False
    prev_handler = signal.signal(signal.SIGWINCH, _on_resize)

    try:
        tty.setraw(fd)
        sys.stdout.write('\033[?25l')

        while True:
            # If selected item is the create option and enter is pressed, show create prompt
            if search_mode:
                filtered = [(w, d) for w, d in entries if query.lower() in d.lower()]
            else:
                filtered = list(entries)

            selected = max(0, min(selected, len(filtered) - 1))

            # Calculate centering
            if search_mode:
                header = f'Search: {query}'
                help_text = 'j/k navigate  enter select  esc exit search'
            else:
                header = 'Worktrees'
                help_text = 'j/k navigate  / search  enter select  esc cancel'

            lines = [header, '']
            lines.extend(d.replace('\033[2m', '').replace('\033[0m', '') for _, d in filtered)
            lines.append('')
            lines.append(help_text)

            max_line = max(len(line.replace('\033[2m', '').replace('\033[0m', '').replace('\033[7m', '').replace('\033[1m', '')) for line in lines)
            term_width = os.get_terminal_size().columns
            left_margin = max(0, (term_width - max_line) // 2)

            out = ['\033[2J\033[H']
            if search_mode:
                out.append(f'{" " * left_margin}Search: \033[7m{query}\033[0m\r\n\r\n')
            else:
                out.append(f'{" " * left_margin}\033[1m{header}\033[0m\r\n\r\n')

            for i, (_, d) in enumerate(filtered):
                if i == selected:
                    out.append(f'{" " * left_margin}\033[7m {d} \033[0m\r\n')
                else:
                    out.append(f'{" " * left_margin} {d}\r\n')
            out.append(f'\r\n{" " * left_margin}\033[2m{help_text}\033[0m')
            sys.stdout.write(''.join(out))
            sys.stdout.flush()

            key = read_key(fd)
            if key == 'resize':
                continue
            elif key == 'search':
                if not search_mode:
                    search_mode = True
                    query = ''
            elif key == 'up' or key == 'down':
                if key == 'up':
                    selected = max(0, selected - 1)
                else:
                    selected = min(len(filtered) - 1, selected + 1)
            elif key == 'enter' and filtered:
                chosen = filtered[selected][0]
                if chosen.get('_create'):
                    # Show create prompt
                    sys.stdout.write('\033[?25h')  # Show cursor
                    sys.stdout.flush()
                    new_worktree = run_create_prompt(repo_root)
                    if new_worktree:
                        return new_worktree
                    sys.stdout.write('\033[?25l')  # Hide cursor again
                else:
                    return chosen
            elif key == 'escape':
                if search_mode:
                    search_mode = False
                    query = ''
                else:
                    return None
            elif key == 'backspace' and search_mode:
                query = query[:-1]
            elif search_mode and len(key) == 1 and key.isprintable():
                query += key
    finally:
        sys.stdout.write('\033[?25h\033[2J\033[H')
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        signal.signal(signal.SIGWINCH, prev_handler)


# ── Entry Point ──────────────────────────────────────────────────────────


def main(args):
    if tomllib is None:
        import time
        sys.stderr.write('\nkitty-worktree requires Python 3.11+ (tomllib).\n')
        sys.stderr.write('Update kitty to 0.26 or later.\n\n')
        sys.stderr.flush()
        time.sleep(2)
        return None

    repo_root = get_repo_root()
    if not repo_root:
        import time
        sys.stderr.write('\nNot in a git repo.\n\n')
        sys.stderr.flush()
        time.sleep(2)
        return None

    worktrees = get_worktrees()
    # If only the main worktree exists, show create prompt
    if len(worktrees) <= 1:
        selected = run_create_prompt(repo_root)
    else:
        selected = run_picker(worktrees, repo_root)

    if not selected:
        return None

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
        # Support session_picker.py variable names for compatibility
        'FOLDER_NAME': Path(wt_path).name,
        'PROJECT_DIR': wt_path,
    }

    tab_title = expand_vars(config.get('tab_title', '{repo} ({branch})'), variables)

    # Focus existing tab if already open
    try:
        tabs_data = json.loads(boss.call_remote_control(None, ('ls',)))
        for os_win in tabs_data:
            for tab in os_win.get('tabs', []):
                if tab.get('title') == tab_title:
                    boss.call_remote_control(None, ('focus-tab', '--match', f'title:^{re.escape(tab_title)}$'))
                    return
    except Exception:
        pass

    # Create tab with panes from config
    panes = config.get('panes', DEFAULT_SESSION['panes'])
    tab_window = None

    for i, pane in enumerate(panes):
        title = expand_vars(pane.get('title', ''), variables)
        command = pane.get('command')
        cwd = expand_vars(pane.get('cwd', wt_path), variables)

        cmd = ['launch', '--cwd', cwd]
        if i == 0:
            cmd += ['--type', 'tab', '--tab-title', tab_title]
        else:
            location = pane.get('location', 'hsplit')
            cmd += ['--location', location]
        if title:
            cmd += ['--title', title]
        if command:
            # Expand variables in command first
            expanded_cmd = expand_vars(command, variables)
            # Use shlex.split to properly handle quoted arguments
            cmd += shlex.split(expanded_cmd)

        window_id = boss.call_remote_control(tab_window, tuple(cmd))
        if i == 0 and window_id is not None:
            tab_window = boss.window_id_map.get(window_id)
            layout = config.get('layout', 'splits')
            boss.call_remote_control(tab_window, ('goto-layout', layout))
