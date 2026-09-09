"""
Retention: periodic sweeps that remove mail on a schedule rather than in
reaction to anything arriving -- a shape the pipeline cannot host, since
it only ever triggers on arrival (see pipeline/enqueue.py's own
docstring). sweep.py is the one sweep this package builds today: Trash
older than an account's own configured age, permanently removed.
"""
