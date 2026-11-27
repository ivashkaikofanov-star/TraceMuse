"""Verify the external Context7 MCP connection used in TraceMuse development."""

import json
import re
import sys
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen


ENDPOINT = 'https://mcp.context7.com/mcp'
PROTOCOL = '2025-03-26'
MAX_RESPONSE = 512 * 1024
LIBRARY_ID = '/websites/fastapi_tiangolo'
LIBRARY_QUERY = {
    'libraryName': 'FastAPI',
    'query': 'FastAPI official Python documentation for UploadFile multipart form uploads and multiple files.',
}
DOCS_QUERY = {
    'libraryId': LIBRARY_ID,
    'query': 'How do I receive multiple files with list[UploadFile] in a multipart/form-data request and read them in bounded chunks using the UploadFile API?',
    'language': 'Python',
}


def read_message(response, request_id):
    content_type = response.headers.get_content_type()
    if content_type == 'application/json':
        body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise ValueError('Размер ответа MCP превышает лимит.')
        return json.loads(body)
    if content_type != 'text/event-stream':
        raise ValueError(f'Неподдерживаемый формат ответа MCP: {content_type}.')

    event_data = []
    remaining = MAX_RESPONSE
    while remaining > 0:
        line = response.readline(remaining + 1)
        if not line:
            break
        remaining -= len(line)
        if line.startswith(b'data:'):
            event_data.append(line[5:].strip())
        elif not line.strip() and event_data:
            message = json.loads(b'\n'.join(event_data))
            if message.get('id') == request_id:
                return message
            event_data.clear()
    raise ValueError('В потоке MCP отсутствует ответ на запрос или превышен лимит размера.')


class Context7:
    def __init__(self):
        self.headers = {
            'Accept': 'application/json, text/event-stream',
            'Content-Type': 'application/json',
        }
        self.next_id = 1

    def request(self, method, params=None):
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        notification = method == 'notifications/initialized'
        if not notification:
            payload['id'] = self.next_id
            self.next_id += 1

        request = Request(ENDPOINT, json.dumps(payload).encode('utf-8'), headers=self.headers)
        with urlopen(request, timeout=30) as response:
            session = response.headers.get('Mcp-Session-Id')
            if session:
                self.headers['Mcp-Session-Id'] = session
            if notification:
                return None
            message = read_message(response, payload['id'])
        if message.get('jsonrpc') != '2.0' or message.get('id') != payload['id']:
            raise ValueError(f'Некорректный JSON-RPC ответ на {method}.')
        if 'error' in message:
            raise ValueError(f'Сервер MCP вернул ошибку {message["error"].get("code")} для {method}.')
        return message['result']

    def tool_text(self, name, arguments):
        result = self.request('tools/call', {'name': name, 'arguments': arguments})
        if result.get('isError'):
            raise ValueError(f'Вызов инструмента {name} завершился ошибкой.')
        return '\n'.join(item['text'] for item in result.get('content', []) if item.get('type') == 'text')


def check():
    client = Context7()
    server = client.request('initialize', {
        'protocolVersion': PROTOCOL,
        'capabilities': {},
        'clientInfo': {'name': 'tracemuse-coursework', 'version': '0.2.0'},
    })
    if server['protocolVersion'] != PROTOCOL:
        raise ValueError(f'Сервер использует неподдерживаемую версию MCP {server["protocolVersion"]}.')
    client.headers['MCP-Protocol-Version'] = PROTOCOL
    client.request('notifications/initialized')

    tool_names = {tool['name'] for tool in client.request('tools/list', {})['tools']}
    if not {'resolve-library-id', 'query-docs'} <= tool_names:
        raise ValueError('Сервер MCP не объявил инструменты поиска документации.')
    libraries = client.tool_text('resolve-library-id', LIBRARY_QUERY)
    available_libraries = re.findall(r'Context7-compatible library ID:\s*(\S+)', libraries)
    if LIBRARY_ID not in available_libraries:
        raise ValueError('Поиск Context7 не вернул ожидаемый каталог официальной документации FastAPI.')

    docs = client.tool_text('query-docs', DOCS_QUERY)
    sources = sorted(set(re.findall(r'https://fastapi\.tiangolo\.com/[^\s>)`]+', docs)))
    if not sources:
        raise ValueError('В ответе Context7 нет ссылок на официальную документацию FastAPI.')
    passages = [line.strip() for line in docs.splitlines() if 'list[UploadFile]' in line or 'read(size' in line]
    if not passages:
        raise ValueError('Ответ Context7 не содержит запрошенных сведений об UploadFile.')

    return {
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'endpoint': ENDPOINT,
        'server': {key: server['serverInfo'][key] for key in ('name', 'version')},
        'protocol_version': PROTOCOL,
        'tools_list': sorted(tool_names),
        'calls': [
            {'name': 'resolve-library-id', 'arguments': LIBRARY_QUERY, 'library_id': LIBRARY_ID},
            {'name': 'query-docs', 'arguments': DOCS_QUERY, 'source_urls': sources, 'excerpts': passages[:6]},
        ],
    }


if __name__ == '__main__':
    try:
        print(json.dumps(check(), ensure_ascii=True, indent=2))
    except (URLError, OSError, ValueError, TypeError, KeyError) as error:
        print(f'Проверка MCP не пройдена: {error}', file=sys.stderr)
        sys.exit(1)
