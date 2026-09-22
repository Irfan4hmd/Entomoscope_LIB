import os
import importlib.util
from PyQt5.QtWidgets import QAction, QStyle
from PyQt5.QtGui import QIcon
from PyQt5 import uic, QtCore

class PluginManager:
    def __init__(self, mainwindow, plugin_folder, plugin_menu, PluginBase):
        self.mainwindow = mainwindow
        self.plugin_folder = plugin_folder
        self.pluginMenu = plugin_menu
        self.PluginBase = PluginBase
        self.plugins = {}

    def load_plugins(self):
        if not os.path.exists(self.plugin_folder):
            os.makedirs(self.plugin_folder)
        for entry in os.scandir(self.plugin_folder):
            if entry.is_dir():
                py_files = [f for f in os.listdir(entry.path) if f.endswith('.py')]
                if not py_files:
                    continue
                plugin_py = os.path.join(entry.path, py_files[0])
                if os.path.isfile(plugin_py):
                    spec = importlib.util.spec_from_file_location(entry.name, plugin_py)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if hasattr(module, 'Plugin'):
                        cls = getattr(module, 'Plugin')
                        if issubclass(cls, self.PluginBase):
                            instance = cls()
                            #instance.init({'app': self.mainwindow})
                            self.plugins[instance.name] = instance

    def populate_plugins_menu(self):
        """ Dynamically add one QAction per plugin"""
        self.pluginMenu.clear()
        for name, plugin in self.plugins.items():
            act = QAction(name, self.mainwindow)
            act.setIcon(QIcon(self.mainwindow.style().standardIcon(QStyle.SP_DirIcon)))
            act.triggered.connect(lambda _, nm=name: self.launch_plugin(nm))
            self.pluginMenu.addAction(act)

    def launch_plugin(self, plugin_name):
        """ Show plugin UI when clicked"""
        plugin = self.plugins.get(plugin_name)
        if not plugin:
            return
        plugin.run({'input': None})