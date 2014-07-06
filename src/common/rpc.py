try:
    import simplejson as json
except ImportError:
    import json

from twisted.internet.protocol import Protocol


MESSAGE_BOUNDARY = "---jsonrpcprotocolboundary---"


class JsonRPCMixin(Protocol):
    """ 
    Provides interface for passing and handling messages holding commands
    and payload data.
    """

    _buffer = ""
    _busyReceiving = False

    def dataReceived(self, data):
        """
        Protocol.dataReceived.
        Translates bytes into lines, and calls messageRecieved
        """
        if self._busyReceiving:
            self._buffer += data
            return

        try:
            self._busyReceiving = True
            self._buffer += data
            while self._buffer:
                try:
                    message, self._buffer = self._buffer.split(
                        MESSAGE_BOUNDARY, 1)
                except ValueError:
                    return
                else:
                    why = self.messageReceived(message)
                    if (why or self.transport and
                        self.transport.disconnecting):
                        return why
        finally:
            self._busyReceiving = False


    def messageReceived(self, message):
        self.handle_data(*self.unpack(message))

    def handle_data(self, command = None, payload = None):
        """
        Calls the instance method with name on_[command] with argument payload
        """

        method_name = "on_%s" % command

        if command and hasattr(self, method_name) and callable(getattr(self, method_name)):
            return getattr(self, method_name)(payload)


    def pack(self, command = None, payload = None):
        """
        Packs a command and payload into a json object.
        """
        return json.dumps(
                {
                    "command": command,
                    "payload": payload,
                }
            ) + MESSAGE_BOUNDARY


    def unpack(self, data):
        """
        Unpacks a json object - message, into a command and a payload.
        """
        print data
        dat = json.loads(data)
        try:
            return (dat["command"], dat["payload"],)
        except KeyError:
            return None, None,