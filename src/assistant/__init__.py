"""Local AI assistant - resident daemon.

Plane A. The daemon is always on; only the models cycle. See ARCHITECTURE.md.

Nothing in this package imports a model runtime. That is deliberate and is the
reason the 24/7 promise costs ~300 MB of RAM and no VRAM: the tray, the
hotkey, the policy gate, the VRAM arbiter, the audit chain and the workspace
manifests all work with the model dead, and the model is spawned only when
there is something to reason about.
"""

__version__ = "0.3.0"
