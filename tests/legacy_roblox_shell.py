"""Exercise archived widgets explicitly without restoring them to the production shell."""

from unittest.mock import patch

from username_finder.gui import MainWindow as CurrentWindow
from username_finder.roblox_ui import RobloxPanel


class MainWindow(CurrentWindow):
    def __init__(self, *args, **kwargs):
        with patch("username_finder.gui.RoValidPanel", RobloxPanel):
            super().__init__(*args, **kwargs)
