#!/usr/bin/env python2

import uuid
import sys
import base64
import urllib

from twisted.internet.protocol import Protocol, ClientFactory, Factory, ServerFactory
from twisted.internet import reactor, protocol, threads, defer
from twisted.internet.task import LoopingCall
from twisted.web import server
from twisted.web.resource import Resource
from twisted.web.util import redirectTo
from twisted.python import log

from common.config import (HEARTBEAT_COMMAND, HEARTBEAT_INTERVAL, 
                            SERVER_TIMEOUT, WRITE_IMAGE_LOOP_INTERVAL)
from common.rpc import JsonRPCMixin


BUFFER = {}
SPAWNED_SERVERS = {}
CONNECTED_CLIENTS = []

STREAMING_SERVER_PORT = 9000
HTTP_SERVER_PORT = 8080


def get_stream_servers(user):
    global SPAWNED_SERVERS
    srvs = dict()
    for k,v in SPAWNED_SERVERS.iteritems():
        if v["client_secret"] == user:
            srvs[k] = SPAWNED_SERVERS[k]
    return srvs


def authenticate_client(client_secret):
    """
    Authenticate the streaming client by client_secret.
    """
    global CONNECTED_CLIENTS
    
    CONNECTED_CLIENTS.append(client_secret)
    return defer.succeed(client_secret)



def get_user_from_request(request):
    """
    Get the user based on the twisted HTTP request object.
    We will be sharing session data.
    """

    global CONNECTED_CLIENTS

    if request.path and len(request.path.split("/")) > 1 and request.path.split("/")[1] in CONNECTED_CLIENTS:
        log.msg("succeed")
        return defer.succeed(request.path.split("/")[1])
    else:
        return defer.fail(403)



def render_html(inside_content = None):
    return """
    <!DOCTYPE html>
    <html>
        <head>
        </head>
        <body>
        %(content)s
        </body>
    </html>""" % {
        "content": inside_content,
    }


class StreamImageServerProtocol(JsonRPCMixin):

    timeout_tick_loop = None
    client_secret = None


    def __init__(self, uuid):
        self.uuid = uuid
        self.timeout = SERVER_TIMEOUT


    def connectionMade(self):
        log.msg("Client connected. Sending authentication request.")
        # Authenticate client
        self.transport.write(self.pack("authenticate", self.uuid))
        # Wait for authentication response


    def on_authenticate(self, payload = None):
        log.msg("Recieved client authentication info.")
        # Dummy authentication
        self.client_secret = payload
        # Authenticate Client
        d = authenticate_client(self.client_secret)
        d.addCallback(self.client_auth_done)
        d.addErrback(self.client_auth_failed)


    def on_heartbeat(self, payload = None):
        log.msg("Recieved a HEARTBEAT from the client. Sending it back.")
        self.transport.write(self.pack(HEARTBEAT_COMMAND, self.uuid))
        self.timeout = SERVER_TIMEOUT


    def on_image(self, payload = None):
        # Decode the image and store it in the buffer
        d = threads.deferToThread(base64.decodestring, payload)
        d.addCallback(self.write_to_buffer)


    def client_auth_done(self, auth_result):
        if auth_result == self.client_secret:
            log.msg("Client authenticated successfully.")
            # Client is authenticated. Send that message and stack the worker.
            self.transport.write(self.pack("authenticated", self.uuid))
            self.store_server_reference()
            # Start the timeout countdown. If we don't get any heartbeat in a meantime, delete the reference.
            self.timeout_tick_loop = LoopingCall(self.tick_timeout)
            self.timeout_tick_loop.start(1, now = True)
        else:
            log.msg("Client authentication failed.")
            self.transport.write(self.pack("forbidden", self.uuid))


    def client_auth_failed(self, err):
        log.msg("Client authentication failed.")
        log.err(err)
        self.transport.write(self.pack("forbidden", self.uuid))


    def store_server_reference(self):
        global SPAWNED_SERVERS
        SPAWNED_SERVERS[self.uuid]= {
                        "worker": self,
                        "client_secret": self.client_secret,
                    }


    def write_to_buffer(self, image_bytes):
        global BUFFER
        BUFFER[self.uuid] = image_bytes


    def send_start_stream(self):
        self.transport.write(self.pack("start_stream", self.uuid))


    def send_stop_stream(self):
        self.transport.write(self.pack("stop_stream", self.uuid))


    def tick_timeout(self):
        # Ticks every second. Once the timer ticks to 0, we delete the reference to the worker.
        self.timeout -= 1
        if self.timeout == 0:
            self.remove_server_reference()
            self.clear_buffer()
            self.stop_timeout_tick_loop()
            self.timeout = SERVER_TIMEOUT


    def stop_timeout_tick_loop(self):
        if self.timeout_tick_loop:
            self.timeout_tick_loop.stop()


    def remove_server_reference(self):
        # Removes the reference to the worker from the pool
        global SPAWNED_SERVERS
        try:
            del SPAWNED_SERVERS[self.uuid]
        except KeyError:
            pass


    def clear_buffer(self):
        # Close and remove the buffer for this worker
        global BUFFER
        try:
            del BUFFER[self.uuid]
        except KeyError:
            pass



class StreamImageServerFactory(ServerFactory):

    uuid = None

    def buildProtocol(self, addr):
        log.msg("Building the protocol.")
        self.uuid = str(uuid.uuid4())
        return StreamImageServerProtocol(self.uuid)



class StreamImageHttpResource(Resource):

    isLeaf = True



    def render_GET(self, request):
        d = get_user_from_request(request)
        d.addCallback(self.render_response_on_auth, request)
        d.addErrback(self.auth_failed, request,)
        return server.NOT_DONE_YET


    def response_finished(self, err, write_image_loop, worker):
        self.send_stop_stream(worker)
        write_image_loop.stop()


    def send_start_stream(self, worker):
        worker.send_start_stream()


    def send_stop_stream(self, worker):
        worker.send_stop_stream()


    def write_image_to_response(self, worker, request):
        global BUFFER
        uuid = worker.uuid
        try:
            request.write("--mjpegboundary\r\n")
            request.write("Content-Type: image/jpeg\r\n")
            request.write("Content-length: "+str(len(BUFFER[uuid]))+"\r\n\r\n" )
            request.write( BUFFER[uuid] )
            request.write("\r\n\r\n")
        except KeyError:
            pass

    def render_response_on_auth(self, user, request):

        if user:
            stream_servers = get_stream_servers(user)
            if not stream_servers:
                request.setResponseCode(404)
                request.write("No streaming servers found. Check your streaming clients.")
                request.finish()

            if request.args.get("s", None):
                uuid = request.args.get("s")[0]
                if uuid in stream_servers.keys():
                    worker = stream_servers[uuid]["worker"]
                    self.send_start_stream(worker)
                    request.setHeader("Content-type", "multipart/x-mixed-replace;boundary=--mjpegboundary")
                    write_image_loop = LoopingCall(self.write_image_to_response, worker, request)
                    write_image_loop.start(WRITE_IMAGE_LOOP_INTERVAL, now = True)
                    request.notifyFinish().addErrback(self.response_finished, write_image_loop, worker)
                else:
                    request.setResponseCode(404)
                    request.write("Streaming server not found!")
                    request.finish()
            else:
                # TODO - Render a page with all available servers
                request.write(render_html(
                    " ".join([('<a href="?s=%s" class="btn btn-default btn-info" target="streaming_iframe">Client %s</a>' % (key, stream_servers.keys().index(key))) for key in stream_servers.keys()]))
                    )
                request.write('<br/><br/><center><iframe name="streaming_iframe" style="border: 0 none;height:400px; width:500px;overflow:hidden"></iframe><center>')
                request.finish()
        else:
            request.setResponseCode(403)
            request.write("Please log in!")
            request.finish()

    def auth_failed(self, err, request):
        request.setResponseCode(403)
        request.write("Auth Failed. Please log in! %s" % err)
        request.finish()


def main(*args, **kwargs):

    reactor.listenTCP(STREAMING_SERVER_PORT, StreamImageServerFactory())
    reactor.listenTCP(HTTP_SERVER_PORT, server.Site(StreamImageHttpResource()))
    log.msg("Starting the streaming(%s) and http(%s) server",
        STREAMING_SERVER_PORT, HTTP_SERVER_PORT)
    log.startLogging(sys.stdout)
    reactor.run()


if __name__ == "__main__":
    main()

