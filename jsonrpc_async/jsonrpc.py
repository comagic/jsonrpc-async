import asyncio
import random
import sys
import json
import functools
import collections

import aiohttp


class JSONRPCError(Exception):
    """Root exception for all errors related to this library"""


class TransportError(JSONRPCError):
    """An error occurred while performing a connection to the server"""


class ProtocolError(JSONRPCError):
    """An error occurred while dealing with the JSON-RPC protocol"""


class Server(object):
    """A connection to a HTTP JSON-RPC server, backed by aiohttp"""

    def __init__(self, url, session=None, **post_kwargs):
        self.session = session or aiohttp.ClientSession()
        post_kwargs['headers'] = post_kwargs.get('headers', {})
        post_kwargs['headers']['Content-Type'] = post_kwargs['headers'].get('Content-Type', 'application/json')
        post_kwargs['headers']['Accept'] = post_kwargs['headers'].get('Accept', 'application/json-rpc')
        self.request = functools.partial(self.session.post, url, **post_kwargs)

    @asyncio.coroutine
    def send_request(self, method_name, is_notification, params):
        """Issue the HTTP request to the server and return the method result (if not a notification)"""
        request_body = self.serialize(method_name, params, is_notification)
        try:
            response = yield from self.request(data=request_body)
        except (aiohttp.ClientResponseError, aiohttp.ClientOSError) as exc:
            raise TransportError('Error calling method %r' % method_name, exc)

        try:
            if response.status != 200:
                raise TransportError('HTTP %d %s' % (response.status, response.reason))

            if not is_notification:
                try:
                    parsed = yield from response.json()
                except ValueError as value_error:
                    raise TransportError('Cannot deserialize response body', value_error)

                return self.parse_result(parsed)
        finally:
            yield from response.release()

    @staticmethod
    def parse_result(result):
        """Parse the data returned by the server according to the JSON-RPC spec. Try to be liberal in what we accept."""
        if not isinstance(result, dict):
            raise ProtocolError('Response is not a dictionary')
        if result.get('error'):
            code = result['error'].get('code', '')
            message = result['error'].get('message', '')
            raise ProtocolError(code, message, result)
        elif 'result' not in result:
            raise ProtocolError('Response without a result field')
        else:
            return result['result']

    @staticmethod
    def dumps(data):
        """Override this method to customize the serialization process (eg. datetime handling)"""
        return json.dumps(data)

    def serialize(self, method_name, params, is_notification):
        """Generate the raw JSON message to be sent to the server"""
        data = {'jsonrpc': '2.0', 'method': method_name}
        if params:
            data['params'] = params
        if not is_notification:
            # some JSON-RPC servers complain when receiving str(uuid.uuid4()). Let's pick something simpler.
            data['id'] = random.randint(1, sys.maxsize)
        return self.dumps(data)

    def __getattr__(self, method_name):
        return Method(self.__request, method_name)

    def __request(self, method_name, args=None, kwargs=None):
        """Perform the actual RPC call. If _notification=True, send a notification and don't wait for a response"""
        is_notification = kwargs.pop('_notification', False)
        if args and kwargs:
            raise ProtocolError('JSON-RPC spec forbids mixing arguments and keyword arguments')

        # from the specs:
        # "If resent, parameters for the rpc call MUST be provided as a Structured value.
        #  Either by-position through an Array or by-name through an Object."
        if len(args) == 1 and isinstance(args[0], collections.Mapping):
            args = dict(args[0])

        return self.send_request(method_name, is_notification, args or kwargs)


class Method(object):
    def __init__(self, request_method, method_name):
        if method_name.startswith("_"):  # prevent rpc-calls for private methods
            raise AttributeError("invalid attribute '%s'" % method_name)
        self.__request_method = request_method
        self.__method_name = method_name

    def __getattr__(self, method_name):
        if method_name.startswith("_"):  # prevent rpc-calls for private methods
            raise AttributeError("invalid attribute '%s'" % method_name)
        return Method(self.__request_method, "%s.%s" % (self.__method_name, method_name))

    def __call__(self, *args, **kwargs):
        return self.__request_method(self.__method_name, args, kwargs)