"""Attribute pytest-xdist worker failures that would otherwise leave no trace.

A worker that is killed, crashes natively, stalls, or disagrees about which
tests exist produces almost nothing today: xdist reports ``node down: Not
properly terminated``, and a stall or a collection mismatch reports nothing at
all. This records what a dying process cannot say afterwards, and raises one
hook per incident with an owner attached.
"""

__version__ = "0.1.0"
