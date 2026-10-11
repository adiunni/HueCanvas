"""OpenSSL DTLS 1.2 PSK transport. Credentials remain in process memory."""
import ctypes as C
import ctypes.util
import os
from pathlib import Path
import select
import socket
import struct
import sys
import time
import threading

PSKCallback = C.CFUNCTYPE(C.c_uint, C.c_void_p, C.c_char_p, C.c_void_p, C.c_uint, C.c_void_p, C.c_uint)


def library_path():
    configured = os.getenv('HUE_OPENSSL_LIB')
    if configured: return configured
    for path in ('/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib', '/usr/local/opt/openssl@3/lib/libssl.3.dylib'):
        if Path(path).is_file(): return path
    return ctypes.util.find_library('ssl')


class DTLSTransport:
    def __init__(self):
        self.lib = None; self.ctx = None; self.ssl = None; self.socket = None; self.callback = None
        self.lock = threading.RLock()

    def _load(self):
        path = library_path()
        if not path: raise ValueError('Hue streaming requires OpenSSL with DTLS-PSK support')
        try: self.lib = C.CDLL(path)
        except OSError as exc: raise ValueError('Cannot load OpenSSL for Hue streaming') from exc
        signatures = {
            'DTLS_client_method': (C.c_void_p, []),
            'SSL_CTX_new': (C.c_void_p, [C.c_void_p]),
            'SSL_CTX_free': (None, [C.c_void_p]),
            'SSL_CTX_ctrl': (C.c_long, [C.c_void_p, C.c_int, C.c_long, C.c_void_p]),
            'SSL_CTX_set_cipher_list': (C.c_int, [C.c_void_p, C.c_char_p]),
            'SSL_CTX_set_psk_client_callback': (None, [C.c_void_p, PSKCallback]),
            'SSL_new': (C.c_void_p, [C.c_void_p]), 'SSL_free': (None, [C.c_void_p]),
            'BIO_new_dgram': (C.c_void_p, [C.c_int, C.c_int]),
            'BIO_ctrl': (C.c_long, [C.c_void_p, C.c_int, C.c_long, C.c_void_p]),
            'SSL_set_bio': (None, [C.c_void_p, C.c_void_p, C.c_void_p]),
            'SSL_connect': (C.c_int, [C.c_void_p]),
            'SSL_get_error': (C.c_int, [C.c_void_p, C.c_int]),
            'SSL_ctrl': (C.c_long, [C.c_void_p, C.c_int, C.c_long, C.c_void_p]),
            'SSL_write': (C.c_int, [C.c_void_p, C.c_void_p, C.c_int]),
        }
        try:
            for name, (result, args) in signatures.items():
                fn = getattr(self.lib, name); fn.restype = result; fn.argtypes = args
        except AttributeError as exc: raise ValueError('This OpenSSL library lacks DTLS-PSK support') from exc

    def connect(self, ip, identity, client_key):
        with self.lock:
            self._connect(ip, identity, client_key)

    def _connect(self, ip, identity, client_key):
        if len(client_key) != 32:
            raise ValueError('Set HUE_CLIENT_KEY to the 32-character clientkey matching HUE_APPLICATION_KEY')
        try: secret = bytes.fromhex(client_key)
        except ValueError: raise ValueError('HUE_CLIENT_KEY must be hexadecimal') from None
        identity = identity.encode('ascii') + b'\0'
        self._load()
        try:
            self.ctx = self.lib.SSL_CTX_new(self.lib.DTLS_client_method())
            if not self.ctx: raise ValueError('Cannot create DTLS context')
            if not self.lib.SSL_CTX_ctrl(self.ctx, 123, 0xFEFD, None) or not self.lib.SSL_CTX_ctrl(self.ctx, 124, 0xFEFD, None):
                raise ValueError('Cannot configure DTLS 1.2')
            if not self.lib.SSL_CTX_set_cipher_list(self.ctx, b'PSK-AES128-GCM-SHA256'):
                raise ValueError('DTLS PSK cipher unavailable')
            def psk_callback(ssl, hint, output_identity, max_identity, output_key, max_key):
                if len(identity)>max_identity or len(secret)>max_key: return 0
                C.memmove(output_identity, identity, len(identity)); C.memmove(output_key, secret, len(secret))
                return len(secret)
            self.callback = PSKCallback(psk_callback)
            self.lib.SSL_CTX_set_psk_client_callback(self.ctx, self.callback)
            family = socket.AF_INET6 if ':' in ip else socket.AF_INET
            self.socket = socket.socket(family, socket.SOCK_DGRAM)
            self.socket.connect((ip, 2100)); self.socket.setblocking(False)
            self.ssl = self.lib.SSL_new(self.ctx)
            if not self.ssl: raise ValueError('Cannot create DTLS session')
            bio = self.lib.BIO_new_dgram(self.socket.fileno(), 0)
            if not bio: raise ValueError('Cannot create DTLS datagram transport')
            self.lib.SSL_set_bio(self.ssl, bio, bio)
            # OpenSSL's datagram BIO needs the peer of the connected UDP socket.
            if family == socket.AF_INET:
                prefix = bytes([16, family]) if sys.platform == 'darwin' else struct.pack('H', family)
                addr = prefix + struct.pack('!H', 2100) + socket.inet_pton(family, ip) + bytes(8)
            else:
                prefix = bytes([28, family]) if sys.platform == 'darwin' else struct.pack('H', family)
                addr = prefix + struct.pack('!H',2100) + bytes(4) + socket.inet_pton(family,ip) + bytes(4)
            peer = C.create_string_buffer(addr)
            if self.lib.BIO_ctrl(bio, 32, 0, peer) <= 0: raise ValueError('Cannot configure DTLS peer')
            deadline = time.monotonic() + 8
            while True:
                result = self.lib.SSL_connect(self.ssl)
                if result == 1: return
                error = self.lib.SSL_get_error(self.ssl, result)
                if error not in (2, 3): raise ValueError('Hue DTLS handshake failed. Check the matching application key/client key and area ownership.')
                if time.monotonic()>deadline: raise ValueError('Hue DTLS handshake timed out on UDP port 2100')
                select.select([self.socket] if error==2 else [], [self.socket] if error==3 else [], [], .05)
                self.lib.SSL_ctrl(self.ssl, 74, 0, None)
        except Exception:
            self.close(); raise

    def send(self, data):
        with self.lock:
            self._send(data)

    def _send(self, data):
        if not self.ssl: raise ValueError('Hue stream is not connected')
        buffer = C.create_string_buffer(data)
        result = self.lib.SSL_write(self.ssl, buffer, len(data))
        if result != len(data): raise ValueError('Hue DTLS frame write failed; streaming stopped')

    def close(self):
        with self.lock:
            self._close()

    def _close(self):
        if self.ssl:
            self.lib.SSL_free(self.ssl); self.ssl = None
        if self.ctx:
            self.lib.SSL_CTX_free(self.ctx); self.ctx = None
        if self.socket:
            self.socket.close(); self.socket = None
        self.callback = None
