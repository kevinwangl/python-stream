#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import socket
import logging
import multiprocessing
from random import randint
from asyncore import dispatcher

from async import TCPClient
from source import Socket
from utils import Window, start_process, endpoint
from executor import Executor, Group, Iterator, Map


__author__ = 'tong'
__all__ = ['Queue', 'Subscribe']

logger = logging.getLogger('stream.logger')


class Queue(Executor):
    EOF = type('EOF', (object, ), {})()

    def __init__(self, batch=None, timeout=None, qsize=1000, **kwargs):
        super(Queue, self).__init__(**kwargs)
        self.qsize = qsize
        self.timeout = timeout or 2
        self.batch = batch

    def run(self, *args):
        exe, callback = args
        for item in exe:
            callback(item)
        callback(self.EOF)

    @property
    def source(self):
        import source
        pipe = multiprocessing.Queue(self.qsize)
        sc = super(Queue, self).source
        rc = Group(window=Window(self.batch, self.timeout))
        p = multiprocessing.Process(target=self.run, args=(sc | rc, pipe.put))
        p.daemon = True
        p.start()
        return source.Queue(pipe, EOF=self.EOF) | Iterator()


class Subscribe(Executor):
    def __init__(self, address=None, cache_path=None, maxsize=1024*1024, listen_num=5, **kwargs):
        self.mutex = multiprocessing.Lock()
        self.sensor = None
        self.server = None
        self.maxsize = maxsize
        self.listen_num = listen_num
        self.cache_path = cache_path or '/tmp/pystream_data_%s' % time.time()
        if hasattr(socket, 'AF_UNIX'):
            self.address = address or '/tmp/pystream_sock_%s' % time.time()
        else:
            self.address = address or ('127.0.0.1', randint(20000, 50000))
        if os.path.exists(self.cache_path):
            os.mkdir(self.cache_path)
        super(Subscribe, self).__init__(**kwargs)

    def init_sensor(self):
        server = self._source | self.producer
        server.start()

    @property
    def producer(self):
        return Sensor(self.address)

    def start(self):
        import asyncore
        if self._source and not self.sensor:
            self.sensor = start_process(self.init_sensor)
        TCPServer(self.address, self.cache_path, self.maxsize, self.listen_num)
        asyncore.loop()

    def __iter__(self):
        raise Exception('please use `[]` to choose topic')

    def __getitem__(self, item):
        class Receiver(Socket):
            def initialize(self):
                sock = super(Receiver, self).initialize()
                sock.send('0{"topic": "%s"}\n' % item)
                return sock

        s = Receiver(self.address)
        u = Map(lambda x: x)
        return s | u


class Sensor(TCPClient):
    def handle_connect(self):
        self.send('1')

    def handle_read(self):
        self.recv(3)

    def handle_write(self):
        self.message = ','.join(self.message)
        TCPClient.handle_write(self)


class TCPServer(dispatcher):
    def __init__(self, address, path, maxsize=1024*1024, listen_num=5, archive_size=1024*1024*1024):
        dispatcher.__init__(self)
        socket_af = socket.AF_UNIX if isinstance(address, basestring) else socket.AF_INET
        self.create_socket(socket_af, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind(address)
        self.listen(listen_num)
        self.size = maxsize
        self.archive_size = archive_size
        self.path = path
        self.data = {}
        self.files = {}

    def location(self, topic):
        filenum, fp = self.files[topic]
        path = os.path.join(self.path, topic)
        item = self.data[topic]
        filesize = os.path.getsize(os.path.join(path, str(filenum)))
        if filesize + sum([len(_) for _ in item]) > self.archive_size:
            fp.close()
            fp = open(os.path.join(path, str(filenum + 1)), 'a')
        fp.write('\n'.join(item) + '\n')
        self.data[topic] = []

    def topic(self, name):
        if name in self.data:
            return self.data[name]
        self.data[name] = []
        path = os.path.join(self.path, name)
        if not os.path.exists(path):
            os.makedirs(path)
            open(os.path.join(path, '0'), 'w').close()
        filenum = max([int(_) for _ in os.listdir(path)])
        self.files[name] = (filenum, open(os.path.join(path, str(filenum)), 'a'))
        return self.data[name]

    def handle_accept(self):
        pair = self.accept()
        if pair is not None:
            sock, addr = pair
            htype = None
            logger.info('server connect to %s(%s), pid: %s' % (addr, sock.fileno(), os.getpid()))
            counter = 0
            while not htype:
                try:
                    htype = sock.recv(1)
                except socket.error:
                    time.sleep(1)
                    counter += 1
                if counter > 5:
                    break
            logger.info('server connect to %s(%s), type: %s' % (addr, sock.fileno(), htype))
            if htype == '1':
                PutHandler(self, sock)
            elif htype == '0':
                GetHandler(self, sock)
            else:
                sock.close()

    def handle_error(self):
        logger.error('server socket %s error' % str(self.addr))
        self.handle_close()

    def handle_expt(self):
        logger.error('server socket %s error: unhandled incoming priority event' % str(self.addr))

    def handle_close(self):
        logger.info('server socket %s close' % str(self.addr))
        self.close()


class Handler(dispatcher):
    def __init__(self, server, *args):
        dispatcher.__init__(self, *args)
        self.server = server
        self.message = ''

    def handle_error(self, e=None):
        logger.error('server handler socket %s error: %s' % (str(self.addr), e))
        self.handle_close()

    def handle_expt(self):
        logger.error('server handler socket %s error: unhandled incoming priority event' % str(self.addr))

    def handle_close(self):
        logger.info('server(%s) socket %s close' % (self.__class__.__name__, self.addr))
        self.close()

    def handle_read(self):
        while True:
            try:
                msg = self.recv(512)
                if not msg:
                    break
                self.message += msg
            except socket.error, e:
                if e.errno == 35:
                    break
                else:
                    raise

        if '\n' not in self.message:
            data = ''
        else:
            data, self.message = self.message.rsplit('\n', 1)

        try:
            for item in data.split('\n'):
                if item:
                    self.send(self.handle(item) + '\n')
        except Exception, e:
            self.handle_error(e)


class PutHandler(Handler):
    def handle(self, data):
        topic, data = data.split(',', 1)
        self.server.topic(topic).append(data)
        if sys.getsizeof(self.server.data[topic]) >= self.server.size:
            self.server.location(topic)
        return '200'


class GetHandler(Handler):
    def __init__(self, server, *args):
        Handler.__init__(self, server, *args)
        self.topic = None
        self.number = -1
        self.blocksize = 0
        self.offset = 0
        self.fp = None

    def handle(self, data):
        data = json.loads(data)
        self.topic = data['topic']
        if data.get('number'):
            self.use(data['number'])
        if data.get('offset'):
            self.offset = data['offset']
            self.fp.seek(self.offset)
        return ''

    def use(self, number):
        self.number = number
        filename = os.path.join(self.server.path, self.topic, str(self.number))
        if self.fp:
            self.fp.close()
        self.fp = open(filename)
        self.blocksize = os.path.getsize(filename)
        self.offset = 0

    def handle_write(self):
        from sendfile import sendfile
        self.offset += sendfile(self.socket.fileno(), self.fp.fileno(), self.offset, self.blocksize)

    def writable(self):
        if not self.topic:
            return False
        if self.fp and self.blocksize > self.offset:
            return True
        if self.fp:
            self.blocksize = endpoint(self.fp)
            if self.blocksize > self.offset:
                return True

        pathname = os.path.join(self.server.path, str(self.topic))
        if os.path.exists(pathname):
            nums = sorted([int(_) for _ in os.listdir(pathname) if int(_) > self.number])
        else:
            nums = []

        if nums:
            self.use(nums[0])
            return True
        if self.topic in self.server.data and self.server.data[self.topic]:
            self.server.location(self.topic)
        return False
