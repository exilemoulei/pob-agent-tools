"""Errors shared by the CLI and daemon request handlers."""


class DaemonRequestError(RuntimeError):
    """A valid request cannot be completed because of its arguments or build state."""
