"""The control surface Live loads. Thin: it only polls the socket."""
from _Framework.ControlSurface import ControlSurface

from .commands import Commands
from .server import LineServer

PORT = 9877


class MixMatchSurface(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self.commands = Commands(self.song(), self.application().browser, self.log_message)
        try:
            self.server = LineServer(self.commands.run, port=PORT, log=self.log_message)
            self.show_message("MixMatch listening on port %d" % PORT)
        except OSError as e:
            self.server = None
            self.show_message("MixMatch: port %d busy (%s)" % (PORT, e))

    def update_display(self):
        ControlSurface.update_display(self)
        if self.server is not None:
            self.server.poll()

    def disconnect(self):
        if self.server is not None:
            self.server.close()
        ControlSurface.disconnect(self)
