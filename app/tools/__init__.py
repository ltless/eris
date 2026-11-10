"""The tools, one module each.

Every module declares its own `Tool` and ends with its own `register()`;
`builtin.py` decides which of them a chat run gets. The registry normalizes a
call's arguments once, so tool modules receive a dict — `normalize_call` in
`app.models.providers` keeps its own job at the transport edge, where a
model's server hands the arguments over.
"""