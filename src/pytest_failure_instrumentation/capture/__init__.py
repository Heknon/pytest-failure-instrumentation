"""Run-time capture: recorded while it can still be recorded.

Everything a dead process cannot tell you afterwards has to be written down
before it dies, and cheaply enough that it costs nothing on the runs where
nothing goes wrong.

resource_history and file_resources are controller-owned exceptions: temporary
live history and an isolated filesystem helper, enabled only by configuration.
"""
