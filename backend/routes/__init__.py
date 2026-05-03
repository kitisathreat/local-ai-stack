"""Route handlers split out of main.py.

Each module in here defines an APIRouter that main.py mounts via
`app.include_router(...)`. Handlers reach back into application state
via the lazy-import pattern:

    from .. import main as backend_main
    cfg = backend_main.state.config

This avoids a top-level circular import (main imports the route
modules; route modules access main.state at request time, after
main has finished defining state).
"""
