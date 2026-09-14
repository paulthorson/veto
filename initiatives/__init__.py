"""Annual-roadmap initiative packages (Initiatives 00-12).

Each ``iNN`` subpackage is owned by its initiative's build team with a
disjoint file scope. Shared cross-initiative schema contracts live in
the owning initiative's ``contracts.py``; consumers build against the
contract, not against another team's in-flight files.
"""
