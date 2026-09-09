"""as-engine: the spec-driven core shared by jira-as and confluence-as.

Scope (spec JAS-31): the overlay applier, normalizer and index compiler; the
operation index loader; the Generic Surface; the transport; the transform
registry with the Atlassian converters isolated in one module; the guard; the
help renderer; the responder and simulation test doubles; serve mode.
"""

__version__ = "0.1.0a0"

__all__ = ["SocketTransport", "__version__", "fake_sidecar"]


def __getattr__(name: str):
    if name == "SocketTransport":
        from .socket_transport import SocketTransport

        return SocketTransport
    if name == "fake_sidecar":
        from .serve import fake_sidecar

        return fake_sidecar
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
