import os
import asyncio
import uuid
import struct
import socket
import logging
from aiohttp import web

# ==========================================
# 1. 全局配置
# ==========================================

DEFAULT_UUID = 'b389e09c-4e31-40da-a56c-433f507e615a'
UUID_STR = os.getenv('UUID', DEFAULT_UUID).strip()
PORT = int(os.getenv('PORT', '3241'))
WSPATH = os.getenv('WSPATH', UUID_STR[:8])

# 去除 UUID 中的横杠并转换为 bytes
LOCAL_UUID_BYTES = bytes.fromhex(UUID_STR.replace('-', ''))

# 会话池
# key: session_id, value: Session object
xhttp_sessions = {}

# 静默日志
def log(*args):
    pass

# ==========================================
# 2. 会话类定义
# ==========================================

class Session:
    def __init__(self, session_id):
        self.id = session_id
        self.download_resp = None # aiohttp StreamResponse (GET)
        self.post_resp = None     # aiohttp StreamResponse (POST)
        self.target_reader = None # asyncio.StreamReader
        self.target_writer = None # asyncio.StreamWriter
        self.buffer = []          # 缓存下行数据
        self.state = 'IDLE'       # IDLE, CONNECTING, ESTABLISHED
        self.uplink_queue = asyncio.Queue() # 用于处理连接建立前的上行数据积压
        self.closed = False
        self.wait_event = asyncio.Event() # 用于保持长连接挂起

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.state = 'CLOSED'
        
        if self.target_writer:
            try:
                self.target_writer.close()
                await self.target_writer.wait_closed()
            except:
                pass
        
        # 释放所有挂起的 HTTP 连接
        self.wait_event.set()
        
        if self.id in xhttp_sessions:
            del xhttp_sessions[self.id]

# ==========================================
# 3. 辅助函数
# ==========================================

async def resolve_host(host):
    # 强制 IPv4 解析
    try:
        if host == 'ipv6': return host # 特殊处理
        # 简单的 IP 格式检查
        try:
            socket.inet_aton(host)
            return host
        except OSError:
            pass

        info = await asyncio.get_event_loop().getaddrinfo(
            host, None, family=socket.AF_INET
        )
        return info[0][4][0]
    except:
        return host

async def try_write(resp, chunk):
    if not resp or not resp.prepared:
        return False
    try:
        await resp.write(chunk)
        return True
    except:
        return False

async def send_downlink_data(session_id, chunk, is_handshake=False):
    session = xhttp_sessions.get(session_id)
    if not session:
        return

    sent = False
    
    # 1. 握手包全量广播 (Broadcast)
    if is_handshake:
        if await try_write(session.download_resp, chunk):
            sent = True
        if await try_write(session.post_resp, chunk):
            sent = True
    else:
        # 2. 普通数据优先走 GET (Stream-Up/Packet-Up/Auto)
        if await try_write(session.download_resp, chunk):
            sent = True
        # 3. 其次走 POST (Stream-None)
        elif await try_write(session.post_resp, chunk):
            sent = True
            
    # 4. 失败缓冲
    if not sent:
        session.buffer.append((chunk, is_handshake))

async def flush_buffer(session):
    if not session.buffer:
        return
    
    current_buffer = list(session.buffer)
    session.buffer = []
    
    for item in current_buffer:
        chunk, is_handshake = item
        await send_downlink_data(session.id, chunk, is_handshake)

# ==========================================
# 4. 协议处理核心
# ==========================================

async def handle_proxy_protocol(first_chunk, session_id, protocol_type, input_stream=None, ws_response=None):
    host = ""
    port = 0
    initial_payload_cursor = 0
    
    try:
        if protocol_type == 'vless':
            if len(first_chunk) < 17: return False
            req_uuid = first_chunk[1:17]
            if req_uuid != LOCAL_UUID_BYTES: return False
            
            cursor = 17
            opt_len = first_chunk[cursor]
            cursor += 1 + opt_len + 1 
            port = struct.unpack('>H', first_chunk[cursor:cursor+2])[0]
            cursor += 2
            atyp = first_chunk[cursor]
            cursor += 1
            
            if atyp == 1: # IPv4
                host = socket.inet_ntoa(first_chunk[cursor:cursor+4])
                cursor += 4
            elif atyp == 2: # Domain
                domain_len = first_chunk[cursor]
                cursor += 1
                host = first_chunk[cursor:cursor+domain_len].decode('utf-8')
                cursor += domain_len
            elif atyp == 3: # IPv6
                cursor += 16
                host = "ipv6" 
            else:
                return False
            initial_payload_cursor = cursor
            
        elif protocol_type == 'trojan':
            if len(first_chunk) < 58: return False
            # Simplified Trojan Check similar to Node logic
            cursor = 56
            if first_chunk[cursor] == 0x0d and first_chunk[cursor+1] == 0x0a: cursor += 2
            if first_chunk[cursor] != 0x01: return False 
            cursor += 2
            atyp = first_chunk[cursor-1] 
            
            if atyp == 1:
                host = socket.inet_ntoa(first_chunk[cursor:cursor+4])
                cursor += 4
            elif atyp == 3:
                d_len = first_chunk[cursor]
                cursor += 1
                host = first_chunk[cursor:cursor+d_len].decode()
                cursor += d_len
            elif atyp == 4:
                cursor += 16
                host = "ipv6"
            else: return False
            
            port = struct.unpack('>H', first_chunk[cursor:cursor+2])[0]
            if len(first_chunk) >= cursor + 2 and first_chunk[cursor] == 0x0d and first_chunk[cursor+1] == 0x0a:
                cursor += 2
            initial_payload_cursor = cursor
            
        # 连接 Target
        target_ip = await resolve_host(host)
        
        try:
            reader, writer = await asyncio.open_connection(target_ip, port)
        except:
            return False
            
        # 设置 NoDelay
        sock = writer.get_extra_info('socket')
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # 绑定到 Session
        if session_id:
            session = xhttp_sessions.get(session_id)
            if session:
                session.target_reader = reader
                session.target_writer = writer
                session.state = 'ESTABLISHED'

        # 发送握手响应 (00 00)
        if protocol_type == 'vless':
            header = b'\x00\x00'
            if session_id:
                await send_downlink_data(session_id, header, is_handshake=True)
            elif ws_response:
                await ws_response.send_bytes(header)

        # 处理积压的上行数据
        if session_id:
            session = xhttp_sessions.get(session_id)
            if session:
                while not session.uplink_queue.empty():
                    queued_chunk = await session.uplink_queue.get()
                    writer.write(queued_chunk)
                await writer.drain()

        # 发送 Initial Payload
        if initial_payload_cursor < len(first_chunk):
            writer.write(first_chunk[initial_payload_cursor:])
            await writer.drain()

        # 开始双向转发
        async def downlink_loop():
            try:
                while True:
                    data = await reader.read(8192)
                    if not data: break
                    if session_id:
                        await send_downlink_data(session_id, data)
                    elif ws_response:
                        await ws_response.send_bytes(data)
            except:
                pass
            finally:
                if session_id:
                    session = xhttp_sessions.get(session_id)
                    if session: await session.close()
                elif ws_response:
                    await ws_response.close()

        asyncio.create_task(downlink_loop())

        return True

    except Exception as e:
        return False

# ==========================================
# 5. 请求处理 (GET / POST / WS)
# ==========================================

async def handle_xhttp_get(request, session_id):
    resp = web.StreamResponse(status=200, reason='OK')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Content-Type'] = 'application/octet-stream'
    resp.headers['Connection'] = 'keep-alive'
    resp.headers['Pragma'] = 'no-cache'
    
    await resp.prepare(request)
    
    try:
        if request.transport:
            sock = request.transport.get_extra_info('socket')
            if sock: sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except:
        pass

    session = xhttp_sessions.get(session_id)
    if not session:
        session = Session(session_id)
        session.state = 'IDLE'
        xhttp_sessions[session_id] = session
    
    session.download_resp = resp
    await flush_buffer(session)
    
    try:
        await session.wait_event.wait()
    except:
        pass
    finally:
        if session.download_resp == resp:
            session.download_resp = None
    
    return resp

async def handle_xhttp_post(request, session_id):
    has_content_length = request.headers.get('Content-Length') is not None
    
    resp = web.StreamResponse(status=200, reason='OK')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Content-Type'] = 'application/octet-stream'
    resp.headers['Connection'] = 'keep-alive'
    resp.headers['Pragma'] = 'no-cache'
    
    await resp.prepare(request)
    
    try:
        if request.transport:
            sock = request.transport.get_extra_info('socket')
            if sock: sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except:
        pass

    session = xhttp_sessions.get(session_id)
    
    if not session:
        session = Session(session_id)
        session.state = 'CONNECTING'
        xhttp_sessions[session_id] = session
        session.post_resp = resp
        
        try:
            first_chunk = await request.content.read()
            if first_chunk:
                is_vless = len(first_chunk) >= 17 and first_chunk[0] == 0x00
                is_trojan = len(first_chunk) >= 58
                
                success = False
                if is_vless:
                    success = await handle_proxy_protocol(first_chunk, session_id, 'vless')
                elif is_trojan:
                    success = await handle_proxy_protocol(first_chunk, session_id, 'trojan')
                
                if not success:
                    await session.close()
                    return resp
        except:
            await session.close()
            return resp

    else:
        session.post_resp = resp
        await flush_buffer(session)
        
        async for chunk in request.content.iter_chunked(4096):
            if session.state == 'ESTABLISHED' and session.target_writer:
                session.target_writer.write(chunk)
                await session.target_writer.drain()
            elif session.state == 'CONNECTING':
                await session.uplink_queue.put(chunk)
            elif session.state == 'IDLE':
                session.state = 'CONNECTING'
                pass

    # Ack & Divert
    if session and session.download_resp and has_content_length:
        await resp.write_eof()
        return resp

    try:
        await session.wait_event.wait()
    except:
        pass
    finally:
        if session and session.post_resp == resp:
            session.post_resp = None
            
    return resp

async def handle_websocket_full(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    target_writer = None
    
    try:
        first_msg = await ws.receive()
        if first_msg.type != web.WSMsgType.BINARY:
            return ws
        first_chunk = first_msg.data
        
        # 简单协议识别
        is_vless = len(first_chunk) >= 17 and first_chunk[0] == 0x00
        is_trojan = len(first_chunk) >= 58
        
        host = ""
        port = 0
        initial_payload = b""
        
        if is_vless:
            # VLESS Parsing
            if first_chunk[1:17] != LOCAL_UUID_BYTES: return ws
            cursor = 17
            opt_len = first_chunk[cursor]
            cursor += 1 + opt_len + 1
            port = struct.unpack('>H', first_chunk[cursor:cursor+2])[0]
            cursor += 2
            atyp = first_chunk[cursor]
            cursor += 1
            if atyp == 1:
                host = socket.inet_ntoa(first_chunk[cursor:cursor+4])
                cursor += 4
            elif atyp == 2:
                d_len = first_chunk[cursor]
                cursor += 1
                host = first_chunk[cursor:cursor+d_len].decode()
                cursor += d_len
            elif atyp == 3:
                cursor += 16
                host = "ipv6"
            else: return ws
            initial_payload = first_chunk[cursor:]
        elif is_trojan:
            # Simplified Trojan Parsing
            cursor = 56
            if first_chunk[cursor] == 0x0d and first_chunk[cursor+1] == 0x0a: cursor += 2
            if first_chunk[cursor] != 0x01: return ws
            cursor += 2
            atyp = first_chunk[cursor-1]
            if atyp == 1:
                host = socket.inet_ntoa(first_chunk[cursor:cursor+4])
                cursor += 4
            elif atyp == 3:
                d_len = first_chunk[cursor]
                cursor += 1
                host = first_chunk[cursor:cursor+d_len].decode()
                cursor += d_len
            elif atyp == 4:
                cursor += 16
                host = "ipv6"
            else: return ws
            port = struct.unpack('>H', first_chunk[cursor:cursor+2])[0]
            if len(first_chunk) >= cursor + 2 and first_chunk[cursor] == 0x0d and first_chunk[cursor+1] == 0x0a:
                cursor += 2
            initial_payload = first_chunk[cursor:]
        else:
            return ws
        
        # 连接 Target
        target_ip = await resolve_host(host)
        reader, writer = await asyncio.open_connection(target_ip, port)
        target_writer = writer
        
        sock = writer.get_extra_info('socket')
        if sock: sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
        # 握手响应 (仅 VLESS)
        if is_vless:
            await ws.send_bytes(b'\x00\x00')
        
        if initial_payload:
            writer.write(initial_payload)
            await writer.drain()
            
        # 双向转发
        async def uplink():
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type == web.WSMsgType.ERROR:
                    break
            writer.close()

        async def downlink():
            try:
                while True:
                    data = await reader.read(8192)
                    if not data: break
                    await ws.send_bytes(data)
            except:
                pass
            finally:
                await ws.close()

        await asyncio.gather(uplink(), downlink())

    except:
        pass
    finally:
        if target_writer:
            try: target_writer.close()
            except: pass
            
    return ws


# ==========================================
# 6. 主路由
# ==========================================

async def request_handler(request):
    path = request.path
    
    # 【改动】默认页面读取 index.html
    if path == '/' or path == '/index.html':
        index_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
        if os.path.exists(index_file):
            try:
                # 同步读取小文件作为启动页面是可接受的，或者使用 run_in_executor 更好
                # 这里为了简洁使用直接读取
                with open(index_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                return web.Response(text=content, content_type='text/html')
            except:
                return web.Response(text="NodeJS Proxy Server is Running.", content_type='text/plain')
        else:
            return web.Response(text="NodeJS Proxy Server is Running.", content_type='text/plain')

    # WebSocket
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        return await handle_websocket_full(request)

    # XHTTP
    if path.startswith(f'/{WSPATH}'):
        clean_path = path.split('?')[0]
        parts = clean_path.split('/')
        session_id = parts[2] if len(parts) > 2 else None
        
        if not session_id:
            session_id = f"stream-none-{uuid.uuid4()}"
            
        if request.method == 'GET':
            return await handle_xhttp_get(request, session_id)
        elif request.method == 'POST':
            return await handle_xhttp_post(request, session_id)
            
    return web.Response(status=404)

# ==========================================
# 7. 启动入口
# ==========================================

async def main():
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', request_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    
    await site.start()
    
    # 保持运行
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    logging.basicConfig(level=logging.CRITICAL)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass