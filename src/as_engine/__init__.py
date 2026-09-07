"""as-engine: the spec-driven core shared by jira-as and confluence-as.

Scope (spec JAS-31): the overlay applier, normalizer and index compiler; the
operation index loader; the Generic Surface; the transport; the transform
registry with the Atlassian converters isolated in one module; the guard; the
help renderer; the responder and simulation test doubles; serve mode.
"""

__version__ = "0.1.0a0"

__all__ = ["__version__"]
