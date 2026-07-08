"""Framework adapters (COMPARISON.md §6 #5).

Each adapter routes the sensitive tool call an agent framework is about to make
through the evidence gate. They are optional: importing `evidence_gate` does not
import these, so the base package carries no framework dependency.
"""
