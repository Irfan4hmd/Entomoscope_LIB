import os
import cv2
import numpy as np
import math
import logging
from numpy import average
from math import radians
from PyQt5.QtWidgets import QLabel
from PyQt5.QtGui import QFont, QPainter, QPen, QPolygonF, QColor, QImage, QPixmap
from PyQt5.QtCore import Qt, QRect, QPointF, pyqtSignal
from PyQt5 import uic
from OBB.obb_measurement_final import MeasurementUtils

logger = logging.getLogger("StaticLabel")

class InteractiveStaticLabel(QLabel):
    zoom_changed = pyqtSignal()  # Signal to notify when zoom changes
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()
        self.cv_img = None
        self.qt_img = None

        self.rectangle_points = None
        self.original_points = None

        self.current_rect = None
        self.drawing_enabled = False

        self.show_rectangle = True
        self.dragging = False
        self.resizing = False
        self.resize_cornor = None
        self.init_corners = None

        self._swapped = False  # Flag to track if the rectangle has been swapped

        self.angle = 0
        self.previous_angle = 0
        self.width_mm = None
        self.height_mm = None
        self.fixed_mm = 1
        self.calibrated_mm_per_pixel = 1
        
        # Zoom and pan properties
        self.zoom_factor = 1.0
        self.min_zoom = 0.1  # Allow zooming down to 10%
        self.max_zoom = 5.0
        self.zoom_step = 0.1
        self.fit_to_window = True  # Start with fit-to-window mode

    def set_image(self, img, viewport_size=None):
        h, w, ch = img.shape
        bytes_per_line = ch * w
        convert_to_Qt_format = QImage(img, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
        if convert_to_Qt_format.isNull():
            raise RuntimeError("Failed to convert image to QImage format. The image might be empty or not in the expected format.")
        qt_img = QPixmap.fromImage(convert_to_Qt_format)
        self.qt_img = qt_img
        self.original_pixmap = qt_img  # Store original for zoom operations
        
        # If viewport size is provided, calculate fit-to-window zoom
        if viewport_size and self.fit_to_window:
            self.calculate_fit_to_window_zoom(viewport_size)
        else:
            self.zoom_factor = 1.0
        
        self.update_display()
    
    def calculate_fit_to_window_zoom(self, viewport_size):
        """Calculate zoom factor to fit image in viewport"""
        if self.qt_img is None:
            return
        
        img_width = self.qt_img.width()
        img_height = self.qt_img.height()
        
        # Subtract minimal padding (just a few pixels for safety)
        viewport_width = viewport_size.width() - 10
        viewport_height = viewport_size.height() - 10
        
        # Calculate scale factors for both dimensions
        width_scale = viewport_width / img_width
        height_scale = viewport_height / img_height
        
        # Use the smaller scale to ensure entire image fits
        fit_zoom = min(width_scale, height_scale, 1.0)  # Don't zoom in beyond 100%
        
        # Ensure it's within our zoom limits
        self.zoom_factor = max(self.min_zoom, min(fit_zoom, self.max_zoom))

    def update_display(self):
        """Update the display with current zoom and pan settings"""
        if self.qt_img is None:
            return
        
        # Always use the ORIGINAL pixmap as the base for scaling
        original_width = self.qt_img.width()
        original_height = self.qt_img.height()
        
        # Scale the pixmap according to zoom factor
        scaled_width = int(original_width * self.zoom_factor)
        scaled_height = int(original_height * self.zoom_factor)
        
        scaled_pixmap = self.qt_img.scaled(
            scaled_width, 
            scaled_height, 
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        
        self.setPixmap(scaled_pixmap)
        self.resize(scaled_pixmap.size())  # Resize label to fit the scaled pixmap
        self.update()
    
    def reset_zoom(self):
        """Reset zoom to fit-to-window mode"""
        self.fit_to_window = True
        # This will be recalculated when update_display is called with viewport info
        self.zoom_factor = 1.0
        self.update_display()
    
    def zoom_in(self):
        """Zoom in by zoom_step"""
        self.fit_to_window = False  # Disable fit mode when manually zooming
        if self.zoom_factor < self.max_zoom:
            self.zoom_factor = min(self.zoom_factor + self.zoom_step, self.max_zoom)
            self.update_display()
            return True
        return False
    
    def zoom_out(self):
        """Zoom out by zoom_step"""
        self.fit_to_window = False  # Disable fit mode when manually zooming
        if self.zoom_factor > self.min_zoom:
            self.zoom_factor = max(self.zoom_factor - self.zoom_step, self.min_zoom)
            self.update_display()
            return True
        return False
    
    def wheelEvent(self, event):
        """Handle mouse wheel for zooming (Ctrl+Wheel)"""
        if event.modifiers() == Qt.ControlModifier:
            delta = event.angleDelta().y()
            changed = False
            if delta > 0:
                changed = self.zoom_in()
            else:
                changed = self.zoom_out()
            
            if changed:
                self.zoom_changed.emit()  # Notify UI to update zoom label
            
            event.accept()
        else:
            super().wheelEvent(event)
    
    def get_image_coordinates(self, widget_pos):
        """Convert widget coordinates to image coordinates accounting for zoom"""
        if self.pixmap() is None:
            return None
        
        # Get the actual displayed pixmap size
        pixmap = self.pixmap()
        label_width = self.width()
        label_height = self.height()
        pixmap_width = pixmap.width()
        pixmap_height = pixmap.height()
        
        # Calculate the offset where the pixmap is drawn (centered in label)
        x_offset = (label_width - pixmap_width) / 2
        y_offset = (label_height - pixmap_height) / 2
        
        # Convert to pixmap coordinates
        pixmap_x = widget_pos.x() - x_offset
        pixmap_y = widget_pos.y() - y_offset
        
        # Convert to original image coordinates (accounting for zoom)
        if self.qt_img:
            image_x = pixmap_x / self.zoom_factor
            image_y = pixmap_y / self.zoom_factor
            return QPointF(image_x, image_y)
        
        return QPointF(pixmap_x, pixmap_y)
    
    def get_widget_coordinates(self, image_pos):
        """Convert image coordinates to widget coordinates accounting for zoom"""
        if self.pixmap() is None or self.qt_img is None:
            return None
        
        # Apply zoom to image coordinates
        pixmap_x = image_pos.x() * self.zoom_factor
        pixmap_y = image_pos.y() * self.zoom_factor
        
        # Get the actual displayed pixmap size
        pixmap = self.pixmap()
        label_width = self.width()
        label_height = self.height()
        pixmap_width = pixmap.width()
        pixmap_height = pixmap.height()
        
        # Calculate the offset where the pixmap is drawn (centered in label)
        x_offset = (label_width - pixmap_width) / 2
        y_offset = (label_height - pixmap_height) / 2
        
        # Convert to widget coordinates
        widget_x = pixmap_x + x_offset
        widget_y = pixmap_y + y_offset
        
        return QPointF(widget_x, widget_y)


    def mousePressEvent(self, event):
        try:
            if event.button() == Qt.LeftButton:
                pos = event.pos()
                if self.rectangle_points and self.show_rectangle:
                    # Check if the mouse is inside the rectangle for dragging
                    polygon = QPolygonF(self.rectangle_points)
                    if polygon.containsPoint(pos, Qt.FillRule.OddEvenFill):
                        self.dragging = True
                        self.offset = pos
                        return
                    # Check if the mouse is near the corners for resizing
                    margin = 10
                    for i, point in enumerate(self.rectangle_points):
                        if abs(point.x() - pos.x()) < margin and abs(point.y() - pos.y()) < margin:
                            self.resizing = True
                            self.resize_corner = i
                            self.init_corners  = np.array([[pt.x(), pt.y()] for pt in self.rectangle_points], dtype="float32")
                            return

                # If the mouse is not inside the rectangle, start drawing a new rectangle
                if self.drawing_enabled:
                    self.current_rect    = QRect(pos,pos)
        except Exception as e:
            logger.debug(f"Error in mousePressEvent: {e}")

    def mouseMoveEvent(self, event):
        try:
            pos = event.pos()
            
            if self.dragging and self.rectangle_points:
                # Drag the selected rectangle
                dx = pos.x() - self.offset.x()
                dy = pos.y() - self.offset.y()
                self.rectangle_points = [QPointF(p.x() + dx, p.y() + dy) for p in self.rectangle_points]
                self.offset = pos
                self.update()

            elif self.resizing and self.rectangle_points is not None:
    
                i = self.resize_corner
                fixed_index = (i + 2) % 4
                adj_index1  = (i + 1) % 4
                adj_index2  = (i - 1) % 4

                init_corners = self.init_corners
                if init_corners is None or len(init_corners) != 4:
                    logger.debug("init_corners is not set or has an invalid length.")
                    return
                A_orig = init_corners[i].copy()
                fixed_corner = init_corners[fixed_index].copy()

                #vector
                v = init_corners[adj_index1] - A_orig
                w = init_corners[adj_index2] - A_orig

                A_new = np.array([float(pos.x()), float(pos.y())], dtype=np.float32)

                delta = fixed_corner - A_new  
                alpha = np.dot(delta, v) / (np.dot(v, v) + 1e-8)
                beta  = np.dot(delta, w) / (np.dot(w, w) + 1e-8)
                B_new = A_new + alpha * v
                D_new = A_new + beta  * w

                new_corners = init_corners.copy()
                new_corners[i] = A_new
                new_corners[adj_index1]  = B_new
                new_corners[adj_index2]  = D_new
                new_corners[fixed_index] = fixed_corner

                new_corners = MeasurementUtils.order_points(new_corners)
                try:
                    new_box = cv2.minAreaRect(new_corners)
                except Exception as e:
                    logger.debug(f"Error in cv2.minAreaRect: {e}")
                    return
                new_center_disp, new_size_disp, new_angle = new_box  
                #print(new_angle)                          

                # previous code
                if new_size_disp[0] < new_size_disp[1]:
                    new_size_disp = (new_size_disp[1], new_size_disp[0])
                    new_angle += 90
                #print(new_angle)
                new_angle = self.normalize_angle(new_angle)
                
                try:
                    pts = cv2.boxPoints(((new_center_disp[0], new_center_disp[1]),(new_size_disp[0], new_size_disp[1]), new_angle)) 
                except Exception as e:
                    logger.debug(f"Error in cv2.boxPoints: {e}")
                    return 
                #pts = MeasurementUtils.order_points(pts)
                self.rectangle_points = [QPointF(x, y) for x, y in pts]

                self.update()

            elif self.current_rect and self.drawing_enabled:
                # Draw the current rectangle
                self.current_rect.setBottomRight(pos)
                self.update()
        except Exception as e:
            logger.debug(f"Error in mouseMoveEvent: {e}")

    def normalize_angle(self, angle):
        """
        Normalize an angle to be within the range [-90, 90) degrees.
        """
        while angle < -90:
            angle += 180
        while angle >= 90:
            angle -= 180
        return angle

    def mouseReleaseEvent(self, event):
        try:
            if event.button() == Qt.LeftButton:
                if self.resizing and self.rectangle_points:
                    self.original_points = self.rectangle_points.copy()
                    self.previous_angle += self.angle
                    self.angle = 0
                if self.dragging and self.rectangle_points:
                    self.original_points = self.rectangle_points.copy()
                    self.previous_angle += self.angle
                    self.angle = 0
                if self.drawing_enabled and self.current_rect is not None:
                    # Finalize the rectangle drawing
                    self.rectangle_points = [self.current_rect.topLeft(), self.current_rect.topRight(), self.current_rect.bottomRight(), self.current_rect.bottomLeft()]
                    self.current_rect = None
                    self.show_rectangle = True

                    if self.cv_img is not None:
                        img_height, img_width = self.cv_img.shape[:2]
                        label_width = self.width()
                        label_height = self.height()
                        self.scale = min(label_width / img_width, label_height / img_height)
                        self.display_width = img_width * self.scale
                        self.display_height = img_height * self.scale
                        self.offset_x = (label_width - self.display_width) / 2
                        self.offset_y = (label_height - self.display_height) / 2


                    self.update()

                self.dragging = False
                self.resizing = False
                self.drawing_enabled = False
                self.resize_cornor = None
                self.init_corners = None
                self.update()
        except Exception as e:
            logger.debug(f"Error in mouseReleaseEvent: {e}")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        if self.show_rectangle and self.rectangle_points:
            self.drawing_enabled = False
            pen = QPen(QColor("#FF9800"), 4, Qt.SolidLine)
            painter.setPen(pen)
            polygon = QPolygonF(self.rectangle_points)
            painter.drawPolygon(polygon)

            handles = []
            for pt in polygon:
                handles.append(pt)
                x = int(pt.x()) - 5
                y = int(pt.y()) - 5
                painter.drawEllipse(x, y, 10, 10)

        elif self.drawing_enabled and self.current_rect:
            pen = QPen(QColor("#009688"), 4, Qt.SolidLine)
            painter.setPen(pen)
            painter.drawRect(self.current_rect.normalized())

        # Draw the measurement result in mm
        if self.rectangle_points and self.width_mm and self.height_mm:
            pen = QPen(QColor("#0A3CE3"), 2, Qt.SolidLine)
            painter.setPen(pen)

            # Find the longest and shortest edge for drawing the result
            edge_lengths = []
            edges = []
            for i in range(len(self.rectangle_points)):
                p1 = self.rectangle_points[i]
                p2 = self.rectangle_points[(i + 1) % len(self.rectangle_points)]    
                edge_length = MeasurementUtils.euclidean_distance([p1.x(), p1.y()], [p2.x(), p2.y()])
                edge_lengths.append(edge_length)
                edges.append((p1, p2))
            
            max_length_index = edge_lengths.index(max(edge_lengths))
            min_length_index = edge_lengths.index(min(edge_lengths))

            def draw_arrow(p_from, p_to):
                """Draw arrow at the end of the line between p_from to p_to"""
                arrow_size = 10
                arrow_angle = math.radians(30)
                dx = p_to.x() - p_from.x()
                dy = p_to.y() - p_from.y()
                line_angle = math.atan2(dy, dx)

                angle1 = line_angle + math.pi - arrow_angle
                angle2 = line_angle + math.pi + arrow_angle

                p3 = QPointF(
                    p_to.x() + math.cos(angle1) * arrow_size,
                    p_to.y() + math.sin(angle1) * arrow_size
                )
                p4 = QPointF(
                    p_to.x() + math.cos(angle2) * arrow_size,
                    p_to.y() + math.sin(angle2) * arrow_size
                )

                painter.drawLine(p_to, p3)
                painter.drawLine(p_to, p4)

            def draw_text_outside(p1,p2,text,color):
                #mid_point = QPointF((p1.x() + p2.x()) / 2, (p1.y() + p2.y()) / 2)
                dx = p2.x() - p1.x()
                dy = p2.y() - p1.y()
                angle_rad = math.atan2(dy, dx)
                angle_deg = math.degrees(angle_rad)

                mid_x = (p1.x() + p2.x()) / 2
                mid_y = (p1.y() + p2.y()) / 2
                painter.save()
                painter.translate(mid_x, mid_y)
                painter.rotate(angle_deg)

                offset = 25
                painter.translate(0, -offset)

                pen.setColor(color)
                painter.setPen(pen)

                font = QFont('Arial',16)
                font.setBold(True)
                painter.setFont(font)
                metrics = painter.fontMetrics()
                text_width = metrics.horizontalAdvance(text)
                text_height = metrics.height()

                painter.drawText(
                    QPointF(-text_width/2, text_height/2),
                    text
                )
                painter.restore()

            pen = QPen(QColor("#0A3CE3"), 4)
            painter.setPen(pen)
            p1, p2 = edges[max_length_index]
            painter.drawLine(p1, p2)
            draw_arrow(p1, p2)
            draw_arrow(p2, p1)
            draw_text_outside(p1, p2, f"{self.height_mm:.2f} mm", QColor("#0A3CE3"))

            pen = QPen(QColor("#00B448"), 4)
            painter.setPen(pen)
            p1, p2 = edges[min_length_index]
            painter.drawLine(p1, p2)
            draw_arrow(p1, p2)
            draw_arrow(p2, p1)
            draw_text_outside(p1, p2, f"{self.width_mm:.2f} mm", QColor("#00B448"))

            self.show_rectangle = False

    def rotate_polygon(self, angle):
        """Rotate the polygon """
        if not self.rectangle_points:
            return
        
        if not self.original_points:
            self.original_points = self.rectangle_points.copy()


        self.angle = (self.angle + angle) % 360
        #print(f"self.angle = {self.angle}")
        # Handle jumping rectangle issue
        current_angle = (self.previous_angle + self.angle) % 360
        #print(f"current angle = {current_angle}")
        if abs(current_angle -45) <= 0.1 or abs(current_angle -315) <= 0.1:
            delta = 0.2 if angle > 0 else -0.2
            self.angle += delta
        center = QPolygonF(self.original_points).boundingRect().center()
        radians_angle = radians(self.angle)
        cos_angle = np.cos(radians_angle)
        sin_angle = np.sin(radians_angle)
        
        rotated_points = []
        for point in self.original_points:
            dx = point.x() - center.x()
            dy = point.y() - center.y()
            new_x = center.x() + dx * cos_angle - dy * sin_angle
            new_y = center.y() + dx * sin_angle + dy * cos_angle    
            rotated_points.append(QPointF(new_x, new_y))

        self.rectangle_points = rotated_points
        self.update()
    
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.rotate_polygon(-3)
        elif event.key() == Qt.Key_Right:
            self.rotate_polygon(3)
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.rectangle_points and self.cv_img is not None:
            # get the original image size and the label size
            img_height, img_width = self.cv_img.shape[:2]
            label_width  = self.width()
            label_height = self.height()

            # recalculate the scale and offset values
            new_scale = min(label_width / img_width, label_height / img_height)
            self.display_width = img_width * new_scale
            self.display_height = img_height * new_scale
            new_offset_x = (label_width - self.display_width) / 2
            new_offset_y = (label_height - self.display_height) / 2
            scale_ratio  = new_scale / self.scale if self.scale else 1

            # Scale the rectangle points based on the new scale and offset
            self.rectangle_points = [
                QPointF(
                    (point.x() - self.offset_x) * scale_ratio + new_offset_x,
                    (point.y() - self.offset_y) * scale_ratio + new_offset_y
                )
                for point in self.rectangle_points
            ]
            
            self.offset_x = new_offset_x
            self.offset_y = new_offset_y
            self.scale = new_scale

            self.original_points = self.rectangle_points.copy()

            self.update()

    def save_annotated_image(self, output_path):
        if not self.pixmap():
            return

        label_size = self.size()
        pixmap = self.pixmap()
        pixmap_size = pixmap.size()

        scaled_pixmap = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        offset_x = (label_size.width() - scaled_pixmap.width()) // 2
        offset_y = (label_size.height() - scaled_pixmap.height()) // 2

        full_pixmap = self.grab()

        image_region = QRect(offset_x, offset_y, scaled_pixmap.width(), scaled_pixmap.height())
        image_only   = full_pixmap.copy(image_region)

        base_path, ext = os.path.splitext(output_path)
        annotated_path = f"{base_path}_annotated{ext}"
        image_only.save(annotated_path)

    def clear_rectangle(self):
        """Clears the drawn rectangle."""
        self.rectangle_points = None
        self.current_rect = None
        self.original_points = None
        self.angle = 0
        self.previous_angle = 0
        self.width_mm = None
        self.height_mm = None
        self.drawing_enabled = False
        self.dragging = False
        self.resizing = False    
        self.show_rectangle = True
        self.resize_cornor = None
        self.update()