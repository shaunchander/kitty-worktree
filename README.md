<h1 align="center">🌲 kitty-worktree</h1>
<p align="center">Jump between git worktrees in kitty with per-repo session layouts.</p>

- ✅ fuzzy picker to switch between worktrees
- ✅ auto-creates tabs with configured pane layouts (splits, editors, shells)
- ✅ focuses existing tab if the worktree is already open
- ✅ per-repo config via `.kitty-worktree.toml` or centralized config
- ✅ template variables for branch, repo name, path
- ✅ zero dependencies — just Python 3.11+ and kitty

## Getting started

Symlink the kitten into your kitty config:

```bash
ln -sf /path/to/worktree.py ~/.config/kitty/worktree.py
```

Add a keybinding in `kitty.conf`:

```conf
map ctrl+shift+w kitten worktree.py
```

🎉 Press `ctrl+shift+w` in any git repo with worktrees and pick one — kitty opens a new tab with your configured layout.

## Configuration

kitty-worktree looks for config in this order:

1. `.kitty-worktree.toml` in the worktree directory
2. `.kitty-worktree.toml` in the repo root
3. `~/.config/kitty/worktree.toml` (centralized, pattern-matched)
4. Built-in default (nvim + shell split)

### Per-repo config

Drop a `.kitty-worktree.toml` in your repo root:

```toml
tab_title = "Backend ({branch})"

[[panes]]
title = "editor"
command = "nvim"

[[panes]]
title = "tests"
location = "hsplit"

[[panes]]
title = "server"
location = "vsplit"
```

### Centralized config

Use `~/.config/kitty/worktree.toml` to define sessions matched by repo path:

```toml
[[sessions]]
match = "candid-api"
tab_title = "Backend ({basename})"

[[sessions.panes]]
title = "editor"
command = "nvim"

[[sessions.panes]]
title = "tests"
location = "hsplit"

[[sessions.panes]]
title = "server"
location = "vsplit"


# Fallback for unmatched repos
[default]
tab_title = "{repo} ({basename})"

[[default.panes]]
title = "editor"
command = "nvim"

[[default.panes]]
title = "shell"
location = "hsplit"
```

### Template variables

| Variable | Value |
|---|---|
| `{branch}` | Current branch name |
| `{repo}` | Repository name |
| `{path}` | Full worktree path |
| `{basename}` | Worktree directory name |

### Pane options

| Key | Description |
|---|---|
| `title` | Pane title |
| `command` | Command to run (e.g. `nvim`) |
| `location` | Split type: `hsplit`, `vsplit` (first pane creates the tab) |
| `cwd` | Working directory (defaults to worktree path, supports templates) |

## Requirements

- [kitty](https://sw.kovidgoyal.net/kitty/) with remote control enabled
- Python 3.11+
- git
