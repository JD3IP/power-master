"""Power Master: Solar optimisation and control system."""

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("power-master")
except Exception:
    __version__ = "dev"
