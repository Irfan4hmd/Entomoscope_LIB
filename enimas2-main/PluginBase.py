import os
from PyQt5 import QtCore, uic

class PluginBase:
    """
    Base class for all plugins. 
    """
    def __init__(self):
        """Initialize plugin with application context"""
        super().__init__()
        self._name = ''
        self.window = None


    @property
    def name(self):
        """Unique name for the plugin"""
        return self._name
    
    
    def run(self, data: dict) -> dict:
        """Execute plugin logic on provided data"""
        ui_path = self.get_ui()
        self.window = uic.loadUi(ui_path)
        self.window.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowCloseButtonHint
            | QtCore.Qt.WindowMinimizeButtonHint
        )
        self.window.setFixedSize(self.window.size())
        self.bind_ui()

        self.window.show()
        return {}

    def get_ui(self) -> str:
        return os.path.join(os.path.dirname(__file__), 'plugin.ui')
    
    def bind_ui(self):
        """Bind UI elements to plugin logic"""
        raise NotImplementedError("Subclasses should implement this method")