"""Who is calling, and what they may reach.

- tokens.py validates the bearer token a request carries.
- principal.py turns its claims into the user it acts as.
- middleware.py is the gate in front of /api and /mcp that does both.
- scope.py is the one access check every read and write goes through.
- ownership.py maps accounts to users; bootstrap.py keeps the owner in line
  with configuration.
"""
