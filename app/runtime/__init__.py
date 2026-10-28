"""The shared execution runtime.

One loop calls the model, runs the tools it asked for, and reports every step
as an event. Nothing else in the app owns an execution loop of its own.
"""