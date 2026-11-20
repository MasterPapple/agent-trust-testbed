"""Testbed for the thesis "Declared authority and verified origin in LLM agent communication".

A coordinator agent (the model under test) asks a helper agent for input over an
A2A-style channel. The helper is compromised: it controls only the text of its
replies. The application signs and verifies every helper reply the same way in
every condition, and conditions A-E differ only in what the model is shown.
"""

__version__ = "0.1.0"
