"""Self-hosted Proton Workflow Connector."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("proton-workflow-connector")
except PackageNotFoundError:  # source checkout without an installed dist
    __version__ = "0.0.0+unknown"
