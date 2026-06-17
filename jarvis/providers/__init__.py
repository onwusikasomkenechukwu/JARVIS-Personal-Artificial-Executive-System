"""Provider state-reads — trusted, decorrelated ground-truth channels.

A provider's own assertions about state (labels, existence, headers the provider set)
are trusted-ish ground truth when read through an authenticated channel. Message
*content* is not, and is never read here. See gmail_state for the first such channel.
"""
