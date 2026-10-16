"""Deterministic evidence gathering.

Code, not the model, decides what to read. Every read runs as the tenant's
read-only identity, every item is sanitised before it can reach a prompt, and
every item carries a citation key the model must use.
"""
