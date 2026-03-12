# Shared mutable state accessible across modules.
# This avoids the __main__ double-import issue with app.py.

GLOBALS = {}
