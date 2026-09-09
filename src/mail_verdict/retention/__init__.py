"""
Retention: a periodic sweep that removes mail sitting in Trash or Junk
longer than an account's own period for that folder, rather than in
reaction to anything arriving -- a shape the pipeline cannot host, since
it only ever triggers on arrival (see pipeline/enqueue.py's own
docstring). sweep.py is the one sweep this package builds: one
mechanism, aged against each role's own independently configurable
setting.
"""
