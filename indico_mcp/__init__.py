"""Model Context Protocol tools for Indico."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('indico-plugin-mcp')
except PackageNotFoundError:
    __version__ = '0.1.0'
