"""The control surface Live loads. Thin: it only polls the socket."""
from _Framework.ControlSurface import ControlSurface

from . import commands as commands_module
from .server import LineServer


def _fresh_commands():
    """The installed commands.py, also when Live re-creates the surface for another set: the module imported
    at Live's start would otherwise stay in use until Live restarts."""
    import importlib

    try:
        return importlib.reload(commands_module)
    except Exception:
        return commands_module

PORT = 47317


class MARASurface(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self.commands = _fresh_commands().Commands(self.song(), self.application().browser, self.log_message)
        try:
            self.server = LineServer(self._handle, port=PORT, log=self.log_message)
            self.show_message("MARA listening on port %d" % PORT)
        except OSError as e:
            self.server = None
            self.show_message("MARA: port %d busy (%s)" % (PORT, e))

    def _handle(self, req):
        """«reload» re-imports the commands, so a changed script works without restarting Live."""
        if req.get("cmd") == "reload":
            self.commands = _fresh_commands().Commands(self.song(), self.application().browser, self.log_message)
            return {"reloaded": True}
        return self.commands.run(req)

    def update_display(self):
        ControlSurface.update_display(self)
        if self.server is not None:
            self.server.poll()

    def disconnect(self):
        if self.server is not None:
            self.server.close()
        ControlSurface.disconnect(self)
