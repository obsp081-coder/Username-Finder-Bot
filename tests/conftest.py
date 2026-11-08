import os

# Native Windows rendering is exercised only by the explicit, short smoke scripts.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
