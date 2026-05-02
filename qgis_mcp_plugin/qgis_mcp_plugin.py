import os
import io
import sys
import json
import socket
import threading
import traceback
from qgis.core import *
from qgis.gui import *
from qgis.PyQt.QtCore import QObject, pyqtSignal, QTimer, Qt, QSize
from qgis.PyQt.QtWidgets import QAction, QDockWidget, QVBoxLayout, QLabel, QPushButton, QSpinBox, QWidget, QApplication
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.utils import active_plugins

class QgisMCPServer(QObject):
    """Server class to handle socket connections and execute QGIS commands"""
    
    # Signals for UI status updates
    client_connected = pyqtSignal()
    client_disconnected = pyqtSignal()
    message_received = pyqtSignal(str)   # command type
    message_sent = pyqtSignal()
    server_error = pyqtSignal(str)       # error message
    
    def __init__(self, host='localhost', port=8765, iface=None):
        super().__init__()
        self.host = host
        self.port = port
        self.iface = iface
        self.running = False
        self.socket = None
        self.client = None
        self.buffer = b''
        self.timer = None
        self._executing = False
        self.last_error = None
    
    def start(self):
        """Start the server"""
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            self.socket.setblocking(False)
            
            # Create a timer to process server operations
            self.timer = QTimer()
            self.timer.timeout.connect(self.process_server)
            self.timer.start(100)  # 100ms interval
            
            QgsMessageLog.logMessage(f"QGIS MCP server started on {self.host}:{self.port}", "QGIS MCP")
            return True
        except Exception as e:
            err = str(e)
            QgsMessageLog.logMessage(f"Failed to start server: {err}", "QGIS MCP", Qgis.Critical)
            self.last_error = err
            self.server_error.emit(err)
            self.stop()
            return False
            
    def stop(self):
        """Stop the server"""
        self.running = False
        
        if self.timer:
            self.timer.stop()
            self.timer = None
            
        if self.socket:
            self.socket.close()
        if self.client:
            self.client.close()
            
        self.socket = None
        self.client = None
        QgsMessageLog.logMessage("QGIS MCP server stopped", "QGIS MCP")
    
    def process_server(self):
        """Process server operations (called by timer)"""
        if not self.running or self._executing:
            return
            
        try:
            # Accept new connections
            if not self.client and self.socket:
                try:
                    self.client, address = self.socket.accept()
                    self.client.setblocking(False)
                    QgsMessageLog.logMessage(f"Connected to client: {address}", "QGIS MCP")
                    self.client_connected.emit()
                except BlockingIOError:
                    pass  # No connection waiting
                except Exception as e:
                    QgsMessageLog.logMessage(f"Error accepting connection: {str(e)}", "QGIS MCP", Qgis.Warning)
                
            # Process existing connection
            if self.client:
                try:
                    # Try to receive data
                    try:
                        data = self.client.recv(8192)
                        if data:
                            self.buffer += data
                            # Try to process complete messages
                            try:
                                # Attempt to parse the buffer as JSON
                                command = json.loads(self.buffer.decode('utf-8'))
                                # If successful, clear the buffer and process command
                                self.buffer = b''
                                cmd_type = command.get("type", "unknown")
                                self.message_received.emit(cmd_type)
                                response = self.execute_command(command)
                                response_json = json.dumps(response)
                                self.client.sendall(response_json.encode('utf-8'))
                                self.message_sent.emit()
                            except json.JSONDecodeError:
                                # Incomplete data, keep in buffer
                                pass
                        else:
                            # Connection closed by client
                            QgsMessageLog.logMessage("Client disconnected", "QGIS MCP")
                            self.client.close()
                            self.client = None
                            self.buffer = b''
                            self.client_disconnected.emit()
                    except BlockingIOError:
                        pass  # No data available
                    except Exception as e:
                        QgsMessageLog.logMessage(f"Error receiving data: {str(e)}", "QGIS MCP", Qgis.Warning)
                        self.client.close()
                        self.client = None
                        self.buffer = b''
                        self.client_disconnected.emit()
                        
                except Exception as e:
                    QgsMessageLog.logMessage(f"Error with client: {str(e)}", "QGIS MCP", Qgis.Warning)
                    if self.client:
                        self.client.close()
                        self.client = None
                    self.buffer = b''
                    self.client_disconnected.emit()
                    
        except Exception as e:
            QgsMessageLog.logMessage(f"Server error: {str(e)}", "QGIS MCP", Qgis.Critical)

    def execute_command(self, command):
        """Execute a command"""
        self._executing = True
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})
            
            handlers = {
                "ping": self.ping,
                "get_qgis_info": self.get_qgis_info,
                "load_project": self.load_project,
                "get_project_info": self.get_project_info,
                "execute_code": self.execute_code,
                "add_vector_layer": self.add_vector_layer,
                "add_raster_layer": self.add_raster_layer,
                "get_layers": self.get_layers,
                "remove_layer": self.remove_layer,
                "zoom_to_layer": self.zoom_to_layer,
                "zoom_to": self.zoom_to,
                "get_layer_features": self.get_layer_features,
                "execute_processing": self.execute_processing,
                "save_project": self.save_project,
                "render_map": self.render_map,
                "create_new_project": self.create_new_project,
            }
            
            handler = handlers.get(cmd_type)
            if handler:
                try:
                    QgsMessageLog.logMessage(f"Executing handler for {cmd_type}", "QGIS MCP")
                    result = handler(**params)
                    QgsMessageLog.logMessage(f"Handler execution complete", "QGIS MCP")
                    return {"status": "success", "result": result}
                except Exception as e:
                    QgsMessageLog.logMessage(f"Error in handler: {str(e)}", "QGIS MCP", Qgis.Critical)
                    traceback.print_exc()
                    return {"status": "error", "message": str(e)}
            else:
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
                
        except Exception as e:
            QgsMessageLog.logMessage(f"Error executing command: {str(e)}", "QGIS MCP", Qgis.Critical)
            traceback.print_exc()
            return {"status": "error", "message": str(e)}
        finally:
            self._executing = False
    
    # Command handlers
    def ping(self, **kwargs):
        """Simple ping command"""
        return {"pong": True}
    
    def get_qgis_info(self, **kwargs):
        """Get basic QGIS information"""
        return {
            "qgis_version": Qgis.version(),
            "profile_folder": QgsApplication.qgisSettingsDirPath(),
            "plugins_count": len(active_plugins)
        }
    
    def get_project_info(self, **kwargs):
        """Get information about the current QGIS project"""
        project = QgsProject.instance()
        
        # Get basic project information
        info = {
            "filename": project.fileName(),
            "title": project.title(),
            "layer_count": len(project.mapLayers()),
            "crs": project.crs().authid(),
            "layers": []
        }
        
        # Add basic layer information (limit to 10 layers for performance)
        layers = list(project.mapLayers().values())
        for i, layer in enumerate(layers):
            if i >= 10:  # Limit to 10 layers
                break
                
            layer_info = {
                "id": layer.id(),
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": layer.isValid() and project.layerTreeRoot().findLayer(layer.id()).isVisible()
            }
            info["layers"].append(layer_info)
        
        return info
    
    def _get_layer_type(self, layer):
        """Helper to get layer type as string"""
        if layer.type() == QgsMapLayer.VectorLayer:
            return f"vector_{layer.geometryType()}"
        elif layer.type() == QgsMapLayer.RasterLayer:
            return "raster"
        else:
            return str(layer.type())
    
    def execute_code(self, code, timeout=300, **kwargs):
        """Execute arbitrary PyQGIS code in a worker thread to keep UI responsive.
        
        The code runs in a background thread while the main thread pumps events
        to prevent QGIS from showing 'Not Responding'. A timeout (default 300s)
        prevents infinite hangs.
        """
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        
        # Shared state for the worker thread
        result = {}
        done_event = threading.Event()
        
        def _worker():
            try:
                sys.stdout = stdout_capture
                sys.stderr = stderr_capture
                
                namespace = {
                    "qgis": Qgis,
                    "QgsProject": QgsProject,
                    "iface": self.iface,
                    "QgsApplication": QgsApplication,
                    "QgsVectorLayer": QgsVectorLayer,
                    "QgsRasterLayer": QgsRasterLayer,
                    "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem
                }
                
                exec(code, namespace)
                
                sys.stdout = original_stdout
                sys.stderr = original_stderr
                
                result["value"] = {
                    "executed": True,
                    "stdout": stdout_capture.getvalue(),
                    "stderr": stderr_capture.getvalue()
                }
            except Exception as e:
                error_traceback = traceback.format_exc()
                sys.stdout = original_stdout
                sys.stderr = original_stderr
                
                result["value"] = {
                    "executed": False,
                    "error": str(e),
                    "traceback": error_traceback,
                    "stdout": stdout_capture.getvalue(),
                    "stderr": stderr_capture.getvalue()
                }
            finally:
                done_event.set()
        
        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        
        # Pump events while waiting so QGIS UI stays responsive
        elapsed = 0.0
        interval = 0.05  # 50ms
        while not done_event.is_set():
            QApplication.processEvents()
            done_event.wait(interval)
            elapsed += interval
            if elapsed >= timeout:
                sys.stdout = original_stdout
                sys.stderr = original_stderr
                return {
                    "executed": False,
                    "error": f"Execution timed out after {timeout} seconds",
                    "stdout": stdout_capture.getvalue(),
                    "stderr": stderr_capture.getvalue()
                }
        
        return result.get("value", {"executed": False, "error": "Unknown error"})
    
    def add_vector_layer(self, path, name=None, provider="ogr", **kwargs):
        """Add a vector layer to the project"""
        if not name:
            name = os.path.basename(path)
            
        # Create the layer
        layer = QgsVectorLayer(path, name, provider)
        
        if not layer.isValid():
            raise Exception(f"Layer is not valid: {path}")
        
        # Add to project
        QgsProject.instance().addMapLayer(layer)
        
        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "feature_count": layer.featureCount()
        }
    
    def add_raster_layer(self, path, name=None, provider="gdal", **kwargs):
        """Add a raster layer to the project"""
        if not name:
            name = os.path.basename(path)
            
        # Create the layer
        layer = QgsRasterLayer(path, name, provider)
        
        if not layer.isValid():
            raise Exception(f"Layer is not valid: {path}")
        
        # Add to project
        QgsProject.instance().addMapLayer(layer)
        
        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": "raster",
            "width": layer.width(),
            "height": layer.height()
        }
    
    def get_layers(self, **kwargs):
        """Get all layers in the project"""
        project = QgsProject.instance()
        layers = []
        
        for layer_id, layer in project.mapLayers().items():
            layer_info = {
                "id": layer_id,
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": project.layerTreeRoot().findLayer(layer_id).isVisible()
            }
            
            # Add type-specific information
            if layer.type() == QgsMapLayer.VectorLayer:
                layer_info.update({
                    "feature_count": layer.featureCount(),
                    "geometry_type": layer.geometryType()
                })
            elif layer.type() == QgsMapLayer.RasterLayer:
                layer_info.update({
                    "width": layer.width(),
                    "height": layer.height()
                })
                
            layers.append(layer_info)
        
        return layers
    
    def remove_layer(self, layer_id, **kwargs):
        """Remove a layer from the project"""
        project = QgsProject.instance()
        
        if layer_id in project.mapLayers():
            project.removeMapLayer(layer_id)
            return {"removed": layer_id}
        else:
            raise Exception(f"Layer not found: {layer_id}")
    
    def zoom_to_layer(self, layer_id, **kwargs):
        """Zoom to a layer's extent"""
        project = QgsProject.instance()
        
        if layer_id in project.mapLayers():
            layer = project.mapLayer(layer_id)
            self.iface.setActiveLayer(layer)
            self.iface.zoomToActiveLayer()
            return {"zoomed_to": layer_id}
        else:
            raise Exception(f"Layer not found: {layer_id}")

    def zoom_to(self, mode, layer_id=None, attribute_name=None, attribute_value=None,
                feature_id=None, scale=None, clear_selection=True, **kwargs):
        """Unified zoom tool using safe iface actions.

        Modes:
            layer    - zoom to a layer extent
            feature  - find a feature by attribute or id, select it, zoom to selection
            selected - zoom to currently selected features
            full     - zoom to all layers
            scale    - set map scale (keeps center)
        """
        project = QgsProject.instance()
        canvas = self.iface.mapCanvas()

        def _canvas_info():
            ext = canvas.extent()
            return {
                "extent": [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()],
                "scale": canvas.scale(),
            }

        if mode == "full":
            self.iface.zoomFull()
            return {"mode": "full", "status": "ok", **_canvas_info()}

        if mode == "scale":
            if scale is None:
                raise Exception("scale parameter is required for mode 'scale'")
            canvas.zoomScale(scale)
            return {"mode": "scale", "status": "ok", **_canvas_info()}

        # Modes that need a layer
        if mode in ("layer", "feature", "selected") and layer_id:
            if layer_id not in project.mapLayers():
                raise Exception(f"Layer not found: {layer_id}")
            layer = project.mapLayer(layer_id)
            self.iface.setActiveLayer(layer)

        if mode == "layer":
            if not layer_id:
                raise Exception("layer_id is required for mode 'layer'")
            self.iface.zoomToActiveLayer()
            return {"mode": "layer", "layer": layer_id, "status": "ok", **_canvas_info()}

        if mode == "selected":
            self.iface.actionZoomToSelected().trigger()
            return {"mode": "selected", "status": "ok", **_canvas_info()}

        if mode == "feature":
            if not layer_id:
                raise Exception("layer_id is required for mode 'feature'")
            layer = project.mapLayer(layer_id)
            if not isinstance(layer, QgsVectorLayer):
                raise Exception(f"Layer {layer_id} is not a vector layer")

            # Find features
            if feature_id is not None:
                feat = layer.getFeature(feature_id)
                if not feat.isValid():
                    raise Exception(f"Feature id {feature_id} not found")
                matched_ids = [feat.id()]
            elif attribute_name and attribute_value is not None:
                field_idx = layer.fields().indexOf(attribute_name)
                if field_idx < 0:
                    raise Exception(f"Field '{attribute_name}' not found in layer")
                request = QgsFeatureRequest().setFilterExpression(
                    f'"{attribute_name}" = \'{attribute_value}\''
                )
                matched_ids = [f.id() for f in layer.getFeatures(request)]
                if not matched_ids:
                    raise Exception(
                        f"No features found where {attribute_name} = '{attribute_value}'"
                    )
            else:
                raise Exception(
                    "feature mode requires feature_id or attribute_name + attribute_value"
                )

            if clear_selection:
                layer.removeSelection()
            layer.select(matched_ids)
            self.iface.setActiveLayer(layer)
            self.iface.actionZoomToSelected().trigger()

            result = {
                "mode": "feature",
                "layer": layer_id,
                "matched_count": len(matched_ids),
                "selected_ids": matched_ids,
                "status": "ok",
                **_canvas_info(),
            }
            if len(matched_ids) > 1:
                result["warning"] = "Multiple features matched"
            return result

        raise Exception(f"Unknown zoom mode: {mode}")
    
    def get_layer_features(self, layer_id, limit=10, **kwargs):
        """Get features from a vector layer"""
        project = QgsProject.instance()
        
        if layer_id in project.mapLayers():
            layer = project.mapLayer(layer_id)
            
            if layer.type() != QgsMapLayer.VectorLayer:
                raise Exception(f"Layer is not a vector layer: {layer_id}")
            
            features = []
            for i, feature in enumerate(layer.getFeatures()):
                if i >= limit:
                    break
                    
                # Extract attributes
                attrs = {}
                for field in layer.fields():
                    attrs[field.name()] = feature.attribute(field.name())
                
                # Extract geometry if available
                geom = None
                if feature.hasGeometry():
                    geom = {
                        "type": feature.geometry().type(),
                        "wkt": feature.geometry().asWkt(precision=4)
                    }
                
                features.append({
                    "id": feature.id(),
                    "attributes": attrs,
                    "geometry": geom
                })
            
            return {
                "layer_id": layer_id,
                "feature_count": layer.featureCount(),
                "features": features,
                "fields": [field.name() for field in layer.fields()]
            }
        else:
            raise Exception(f"Layer not found: {layer_id}")
    
    def execute_processing(self, algorithm, parameters, **kwargs):
        """Execute a processing algorithm"""
        try:
            import processing
            result = processing.run(algorithm, parameters)
            return {
                "algorithm": algorithm,
                "result": {k: str(v) for k, v in result.items()}  # Convert values to strings for JSON
            }
        except Exception as e:
            raise Exception(f"Processing error: {str(e)}")
    
    def save_project(self, path=None, **kwargs):
        """Save the current project"""
        project = QgsProject.instance()
        
        if not path and not project.fileName():
            raise Exception("No project path specified and no current project path")
        
        save_path = path if path else project.fileName()
        if project.write(save_path):
            return {"saved": save_path}
        else:
            raise Exception(f"Failed to save project to {save_path}")
    
    def load_project(self, path, **kwargs):
        """Load a project"""
        project = QgsProject.instance()
        
        if project.read(path):
            self.iface.mapCanvas().refresh()
            return {
                "loaded": path,
                "layer_count": len(project.mapLayers())
            }
        else:
            raise Exception(f"Failed to load project from {path}")
    
    def create_new_project(self, path, **kwargs):
        """
        Creates a new QGIS project and saves it at the specified path.
        If a project is already loaded, it clears it before creating the new one.
        
        :param project_path: Full path where the project will be saved
                            (e.g., 'C:/path/to/project.qgz')
        """
        project = QgsProject.instance()
        
        if project.fileName():
            project.clear()
        
        project.setFileName(path)
        self.iface.mapCanvas().refresh()
        
        # Save the project
        if project.write():
            return {
                "created": f"Project created and saved successfully at: {path}",
                "layer_count": len(project.mapLayers())
            }
        else:
            raise Exception(f"Failed to save project to {path}")
    
    def render_map(self, path, width=800, height=600, **kwargs):
        """Render the current map view to an image"""
        try:
            # Create map settings
            ms = QgsMapSettings()
            
            # Set layers to render
            layers = list(QgsProject.instance().mapLayers().values())
            ms.setLayers(layers)
            
            # Set map canvas properties
            rect = self.iface.mapCanvas().extent()
            ms.setExtent(rect)
            ms.setOutputSize(QSize(width, height))
            ms.setBackgroundColor(QColor(255, 255, 255))
            ms.setOutputDpi(96)
            
            # Create the render
            render = QgsMapRendererParallelJob(ms)
            
            # Start rendering
            render.start()
            render.waitForFinished()
            
            # Get the image and save
            img = render.renderedImage()
            if img.save(path):
                return {
                    "rendered": True,
                    "path": path,
                    "width": width,
                    "height": height
                }
            else:
                raise Exception(f"Failed to save rendered image to {path}")
                
        except Exception as e:
            raise Exception(f"Render error: {str(e)}")


class QgisMCPDockWidget(QDockWidget):
    """Dock widget for the QGIS MCP plugin"""
    closed = pyqtSignal()
    
    # Style constants for the status indicator
    INDICATOR_STYLE = (
        "border-radius: 8px; min-width: 16px; max-width: 16px; "
        "min-height: 16px; max-height: 16px; border: 1px solid #555;"
    )
    COLOR_GREY = f"background-color: #888; {INDICATOR_STYLE}"
    COLOR_GREEN = f"background-color: #4CAF50; {INDICATOR_STYLE}"
    COLOR_YELLOW = f"background-color: #FFC107; {INDICATOR_STYLE}"
    COLOR_RED = f"background-color: #E53935; {INDICATOR_STYLE}"
    
    def __init__(self, iface):
        super().__init__("QGIS MCP")
        self.iface = iface
        self.server = None
        self._flash_timer = QTimer()
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self.setup_ui()
    
    def setup_ui(self):
        """Set up the dock widget UI"""
        # Create widget and layout
        widget = QWidget()
        layout = QVBoxLayout()
        widget.setLayout(layout)
        
        # Add port selection
        layout.addWidget(QLabel("Server Port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setMinimum(1024)
        self.port_spin.setMaximum(65535)
        self.port_spin.setValue(8765)
        layout.addWidget(self.port_spin)
        
        # Add server control buttons
        self.start_button = QPushButton("Start Server")
        self.start_button.clicked.connect(self.start_server)
        layout.addWidget(self.start_button)
        
        self.stop_button = QPushButton("Stop Server")
        self.stop_button.clicked.connect(self.stop_server)
        self.stop_button.setEnabled(False)
        layout.addWidget(self.stop_button)
        
        # Status row: indicator light + label
        from qgis.PyQt.QtWidgets import QHBoxLayout
        status_layout = QHBoxLayout()
        
        self.indicator = QLabel()
        self.indicator.setStyleSheet(self.COLOR_GREY)
        status_layout.addWidget(self.indicator)
        
        self.status_label = QLabel("Server: Stopped")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        layout.addLayout(status_layout)
        
        # Last activity label
        self.activity_label = QLabel("")
        layout.addWidget(self.activity_label)
        
        # Add to dock widget
        self.setWidget(widget)
    
    def start_server(self):
        """Start the server"""
        if not self.server:
            port = self.port_spin.value()
            self.server = QgisMCPServer(port=port, iface=self.iface)
            self.server.client_connected.connect(self._on_client_connected)
            self.server.client_disconnected.connect(self._on_client_disconnected)
            self.server.message_received.connect(self._on_message_received)
            self.server.message_sent.connect(self._on_message_sent)
            self.server.server_error.connect(self._on_server_error)

        port = self.server.port
        if self.server.start():
            self.status_label.setText(f"Server: Running on port {self.server.port}")
            self.indicator.setStyleSheet(self.COLOR_GREEN)
            self.activity_label.setText("Waiting for client...")
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.port_spin.setEnabled(False)
        else:
            # Failure path: surface error in the status row
            err = getattr(self.server, "last_error", "") or "unknown error"
            self.status_label.setText(f"Server: Failed on port {port}")
            self.indicator.setStyleSheet(self.COLOR_RED)
            self.activity_label.setText(err)
            self.activity_label.setToolTip(err)
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.port_spin.setEnabled(True)
            self.server = None
    
    def stop_server(self):
        """Stop the server"""
        if self.server:
            self.server.stop()
            self.server = None
            
        self.status_label.setText("Server: Stopped")
        self.indicator.setStyleSheet(self.COLOR_GREY)
        self.activity_label.setText("")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.port_spin.setEnabled(True)
    
    # --- Status indicator slots ---
    
    def _on_client_connected(self):
        self.indicator.setStyleSheet(self.COLOR_GREEN)
        self.activity_label.setText("Client connected")
    
    def _on_client_disconnected(self):
        self.indicator.setStyleSheet(self.COLOR_GREEN)
        self.activity_label.setText("Client disconnected — waiting...")
    
    def _on_message_received(self, cmd_type):
        self.indicator.setStyleSheet(self.COLOR_YELLOW)
        self.activity_label.setText(f"⟵ {cmd_type}")
    
    def _on_message_sent(self):
        self.activity_label.setText(self.activity_label.text() + "  ✔")
        self._flash_timer.start(400)

    def _on_server_error(self, message):
        self.indicator.setStyleSheet(self.COLOR_RED)
        self.status_label.setText("Server: Error")
        self.activity_label.setText(message)
        self.activity_label.setToolTip(message)
    
    def _end_flash(self):
        if self.server and self.server.running:
            self.indicator.setStyleSheet(self.COLOR_GREEN)

    def closeEvent(self, event):
        """Stop server on dock close"""
        self._flash_timer.stop()
        self.stop_server()
        self.closed.emit()
        super().closeEvent(event)


class QgisMCPPlugin:
    """Main plugin class for QGIS MCP"""
    
    def __init__(self, iface):
        self.iface = iface
        self.dock_widget = None
        self.action = None
    
    def initGui(self):
        """Initialize GUI"""
        # Create action
        self.action = QAction(
            "QGIS MCP",
            self.iface.mainWindow()
        )
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)
        
        # Add to plugins menu and toolbar
        self.iface.addPluginToMenu("QGIS MCP", self.action)
        self.iface.addToolBarIcon(self.action)
    
    def toggle_dock(self, checked):
        """Toggle the dock widget"""
        if checked:
            # Create dock widget if it doesn't exist
            if not self.dock_widget:
                self.dock_widget = QgisMCPDockWidget(self.iface)
                self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock_widget)
                # Connect close event
                self.dock_widget.closed.connect(self.dock_closed)
            else:
                # Show existing dock widget
                self.dock_widget.show()
        else:
            # Hide dock widget
            if self.dock_widget:
                self.dock_widget.hide()
    
    def dock_closed(self):
        """Handle dock widget closed"""
        self.action.setChecked(False)
    
    def unload(self):
        """Unload plugin"""
        # Stop server if running
        if self.dock_widget:
            self.dock_widget.stop_server()
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget = None
            
        # Remove plugin menu item and toolbar icon
        self.iface.removePluginMenu("QGIS MCP", self.action)
        self.iface.removeToolBarIcon(self.action)


# Plugin entry point
def classFactory(iface):
    return QgisMCPPlugin(iface)
