"""Token-safe Telegram transport with bounded connections and IPv4-first failover."""
import http.client
import io
import json
import socket
import ssl
import time
import urllib.parse
import urllib.error


class TelegramError(Exception):
    def __init__(self, code, retry_after=None):
        self.code = int(code)
        self.retry_after = retry_after
        super().__init__('Telegram HTTP ' + str(self.code))


class NetworkError(Exception):
    pass


def connect_family(address, timeout, family):
    deadline = time.monotonic() + min(timeout, 5)
    addresses = socket.getaddrinfo(address[0], address[1], family, socket.SOCK_STREAM)
    for af, kind, proto, _, target in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = socket.socket(af, kind, proto)
        connection.settimeout(remaining)
        try:
            connection.connect(target)
            connection.settimeout(timeout)
            return connection
        except OSError:
            connection.close()
    raise TimeoutError('Telegram connection unavailable')


class TelegramConnection(http.client.HTTPSConnection):
    def __init__(self, family, timeout):
        super().__init__('api.telegram.org', timeout=timeout, context=ssl.create_default_context())
        self._create_connection = lambda address, timeout, source_address=None: connect_family(address, timeout, family)


class Telegram:
    def __init__(self, token):
        self.token = token
        self.preferred_family = socket.AF_INET

    def call(self, method, payload=None):
        if method not in {'getMe', 'getWebhookInfo', 'deleteWebhook', 'getUpdates', 'sendMessage'}:
            raise ValueError('Unsupported Telegram method')
        body = urllib.parse.urlencode(payload or {}).encode()
        alternate = socket.AF_INET6 if self.preferred_family == socket.AF_INET else socket.AF_INET
        for family in (self.preferred_family, alternate):
            connection = TelegramConnection(family, timeout=35)
            try:
                connection.request('POST', '/bot' + self.token + '/' + method, body,
                                   {'Content-Type': 'application/x-www-form-urlencoded'})
                response = connection.getresponse()
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError('Telegram response too large')
                result = json.loads(raw)
                self.preferred_family = family
                if not result.get('ok'):
                    raise TelegramError(result.get('error_code', response.status),
                                        result.get('parameters', {}).get('retry_after'))
                return result['result']
            except TelegramError:
                raise
            except (OSError, ValueError, http.client.HTTPException):
                # Fail over only transport failures. Never log request URLs or tokens.
                continue
            finally:
                connection.close()
        raise NetworkError('Telegram network unavailable') from None


def urlopen(request, *, timeout):
    """Compatibility adapter for the existing scheduled report senders only."""
    target = urllib.parse.urlsplit(request.full_url)
    if target.scheme != 'https' or target.hostname != 'api.telegram.org':
        raise ValueError('Telegram transport cannot access another host')
    for family in (socket.AF_INET, socket.AF_INET6):
        connection = TelegramConnection(family, timeout=timeout)
        try:
            headers = dict(request.header_items())
            if request.data is not None and not any(key.lower() == 'content-type' for key in headers):
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            connection.request(request.get_method(), target.path,
                               request.data, headers)
            response = connection.getresponse()
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError('Telegram response too large')
            if response.status >= 400:
                raise urllib.error.HTTPError('https://api.telegram.org/[redacted]', response.status,
                                             'Telegram rejected request', {}, io.BytesIO(raw))
            return io.BytesIO(raw)
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException):
            continue
        finally:
            connection.close()
    raise urllib.error.URLError('Telegram network unavailable') from None
