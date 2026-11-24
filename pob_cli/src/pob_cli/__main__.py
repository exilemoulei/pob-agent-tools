"""Module entry point (`python -m pob_cli`) with zero-Click `_lua-host` fast path."""

from __future__ import annotations

import sys


def _dispatch() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "_lua-host":
        from .updater.win_lua_host import main as _lua_host_main

        raise SystemExit(_lua_host_main(sys.argv[2:]))
    from .cli import main

    main()


if __name__ == "__main__":
    _dispatch()
