import sys
import traceback
from PyQt5.QtWidgets import QApplication, QMessageBox

def global_except_hook(exc_type, exc_value, exc_traceback):
    """Ensure that the application exits gracefully."""
    error_msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    app = QApplication.instance() or QApplication(sys.argv)
    dlg = QMessageBox()
    dlg.setIcon(QMessageBox.Critical)
    dlg.setWindowTitle("Application Error")
    dlg.setText("An unexpected error occurred. The application will now exit.")
    dlg.setDetailedText(error_msg)
    dlg.exec_()

    app.quit()
    
    sys.exit(1)