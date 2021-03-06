from aiohttp import web, WSMsgType
from pathlib import Path
from typing import Callable, Optional, Awaitable, Dict
from json.decoder import JSONDecodeError
import asyncio, traceback, html, time, threading
import concurrent.futures
import packaging.version
from types import SimpleNamespace
from contextlib import suppress
from datetime import datetime

from . import planner
from .fileio import FileIO
from .fileserver import FileServer
from .chunker import scan_dir
from .common import make_human_cli_status_func, json_status_func, Defaults, parse_cli_args, HashableBase


class PeerSession(HashableBase):
    address: str
    sendq: asyncio.Queue
    node: planner.Node
    version: str

    def name(self):
        return self.node.name if self.node else self.address


# For integration testing / debugging: threadsafe way send test messages to peers (to impersonate master)
_DEBUG_PEER_INJECTOR_CALLBACKS: Dict[int, Callable] = {}
_DEBUG_PIC_LOCK = threading.RLock()
def DEBUG_INJECT_MSG_TO_PEER(peer_idx, msg):
    with _DEBUG_PIC_LOCK:
        tuple(_DEBUG_PEER_INJECTOR_CALLBACKS.values())[peer_idx](msg)


# Server that plans p2p distribution of sync directory files to connected clients,
# and also works as a seed node.

class MasterNode:

    def __init__(self, status_func: Callable, chunk_size: int):
        self.chunk_size = chunk_size
        self.seed_node = None
        self.status_func = status_func
        self.replan_trigger = asyncio.Event()
        self.status_page_cache_html = None
        self.status_page_cache_timestamp = time.time()

        dummy_lm = planner.LinkMapper()
        self.swarm = planner.SwarmCoordinator(link_mapper=dummy_lm)

        async def __on_upload_finished():
            # Let planner know how many free upload slots masternode's file server has
            self.seed_node.set_active_transfers(downloads={}, n_uploads=self.file_server.active_uploads)
            self.seed_node.update_transfer_speed(self.file_server.upload_times)
            self.file_server.upload_times.clear()

        self.file_server = FileServer(status_func, upload_finished_func=__on_upload_finished)


    def __make_batch_msg(self):
        return {'action': 'new_batch', 'data': self.file_server.batch.to_dict()}

    async def replace_sync_batch(self, new_batch):
        if new_batch != self.file_server.batch:
            self.status_func(log_info='Sync batch changed. Updating planner and notifying clients.')
            self.file_server.set_batch(new_batch)

            # Update planner and send new list to clients
            self.swarm.reset_hashes((c.hash for c in self.file_server.batch.chunks))
            self.seed_node.add_hashes(self.swarm.all_hashes, clear_first=True)
            fl_msg = self.__make_batch_msg()
            for n in self.swarm.nodes:
                if n != self.seed_node:
                    await n.client.send_queue.put(fl_msg)
            self.replan_trigger.set()
        else:
            self.status_func(log_info='No changes in sync dir.')

    def start_master_server(self, base_dir: str, port: int,
                            ul_limit: float, concurrent_uploads: int,
                            https_cert: Optional[str], https_key: Optional[str]) -> Awaitable:
        async def handle_client_msg(peer: PeerSession, msg) -> None:
            """
            Handle received messages from clients.

            :param peer: Peer session
            :param msg: Message from client in a dict
            """
            client_name = (peer.node.name if peer.node else peer.address)
            self.status_func(log_debug=f'[{peer.address}] Msg from {client_name}: {str(msg)}')

            async def error(txt, fatal=False):
                self.status_func(log_error=('FATAL - ' if fatal else '') +
                                           f'[{peer.address}] Sending error to {client_name}: {txt}')
                await peer.sendq.put({'action': 'fatal' if fatal else 'error', 'orig_msg': msg, 'message': txt})

            async def ok(txt):
                await peer.sendq.put({'action': 'ok', 'message': txt})

            try:
                action = msg.get('action')
                if not action:
                    return await error(f"Messages must have 'action'. Bad protocol. Goodbye.", fatal=True)

                # ---------------------------------------------------
                # Initial version check
                # ---------------------------------------------------
                if action == 'version':
                    here = packaging.version.parse(Defaults.PROTOCOL_VERSION)
                    try:
                        there = packaging.version.parse(msg.get('protocol') or 'MISSING')
                        if not there.release:
                            raise packaging.version.InvalidVersion
                        if there.release[0] != here.release[0]:
                            await peer.sendq.put({'action': 'fatal', 'message':
                                                  f'Incompatible protocol. Server has {Defaults.PROTOCOL_VERSION}, '
                                                  f'yours is {msg.get("protocol")}'})
                        else:
                            await ok({'message': 'Version accepted.'})
                            peer.version = there
                    except packaging.version.InvalidVersion:
                        return await error(f'Invalid protocol version {str(msg.get("protocol"))}.', fatal=True)
                    self.status_func(log_info=f'Client "{client_name}" uses protocol version {msg.get("protocol")}, '+
                                              f'app version {msg.get("app") or "MISSING"}')

                # ---------------------------------------------------
                # Client is ready to sync
                # ---------------------------------------------------
                elif action == 'join_swarm':
                    dl_slots = msg.get('concurrent_transfers') or 2

                    initial_hashes = msg.get('hashes')
                    if not isinstance(initial_hashes, list):
                        return await error("'hashes' argument missing or invalid.")

                    dl_url = msg.get('dl_url')
                    if not dl_url or 'http' not in dl_url:
                        return await error("'dl_url' argument missing or invalid.")
                    if '{hash}' not in dl_url:
                        return await error("'dl_url' must contain placeholder '{hash}'.")

                    if not peer.version:
                        return await error("Protocol error: must check version before joining")

                    if peer.node:  # rejoin = destroy old and create new
                        peer.node.destroy()
                        self.status_func(log_info=f'[{peer.address}] Rejoining "{client_name}".')

                    peer.node = self.swarm.node_join(initial_hashes, dl_slots, dl_slots)
                    peer.node.name = msg.get('nick') or self.file_server.hostname
                    peer.node.client = SimpleNamespace(
                        dl_url=dl_url,
                        send_queue=peer.sendq)

                    self.status_func(log_info=f'[{peer.address}] Client "{client_name}" joined swarm as "{peer.node.name}".'
                                              f' URL: {peer.node.client.dl_url}')
                    await ok('Joined swarm.')
                    await peer.sendq.put(self.__make_batch_msg())

                    self.replan_trigger.set()

                # ---------------------------------------------------
                # Client's got (new) chunks
                # ---------------------------------------------------
                elif action == 'set_hashes' or action == 'add_hashes':
                    if not isinstance(msg.get('hashes'), list):
                        return await error("Must have list in arg 'hashes'")
                    if not peer.node:
                        return await error("Join the swarm first.")

                    unknown_hashes = peer.node.add_hashes(msg.get('hashes'), clear_first=(action == 'set_hashes'))

                    self.replan_trigger.set()
                    await ok('Hashes updated')

                    if unknown_hashes:
                        self.status_func(log_info=f'Client "{client_name}" had unknown hashes: {str(unknown_hashes)}')
                        await peer.sendq.put({
                            'action': 'rehash',
                            'message': 'You reported hashes not belonging to the swarm. You need to rehash files.',
                            'unknown_hashes': tuple(unknown_hashes)})

                # ---------------------------------------------------
                # Client reports current downloads, uploads and speed
                # ---------------------------------------------------
                elif action == 'report_transfers':
                    dls, ul_count = msg.get('dls'), msg.get('ul_count')
                    upload_times = msg.get('ul_times')

                    if None in (dls, ul_count, upload_times):
                        return await error(f"Missing args.")
                    if not peer.node:
                        return await error("Join the swarm first.")

                    cur_downloads = {}
                    for dl in dls:
                        chunk_id, url, mbps_limit = dl.get('hash'), dl.get('url'), dl.get('mbps_limit')
                        if not isinstance(dl, dict) or None in (chunk_id, url, mbps_limit):
                            return await error(f"Malformed 'dl' entry")
                        for n in self.swarm.nodes:
                            if n.client.dl_url.replace('{hash}', '') in dl['url']:
                                cur_downloads[(chunk_id, n)] = mbps_limit
                                break
                    if len(cur_downloads) != len(dls):
                        self.status_func(log_error=f'PROTOCOL ERROR? Could not find from_nodes for all download URLS.')

                    peer.node.set_active_transfers(downloads=cur_downloads, n_uploads=ul_count)
                    peer.node.update_transfer_speed(upload_times)

                    if ul_count < peer.node.max_concurrent_uls or len(dls) < peer.node.max_concurrent_dls:
                        self.replan_trigger.set()

                    await ok('Transfer status updated')

                elif action == 'error':
                    self.status_func(log_info=f'Error msg from client ({client_name}): {str(msg)}')
                else:
                    return await error(f"Unknown action '{str(action)}'. Bad protocol. Goodbye.", fatal=True)

            except Exception as e:
                await error('Exception raised: '+str(e))
                self.status_func(log_error=f'Error while handling client ({client_name}) message "{str(msg)}": \n' +
                                           traceback.format_exc(), popup=True)

        async def http_handler__status(request):
            """
            Show a HTML formatted status report.
            """
            self.status_func(log_debug=f"[{request.remote}] GET {request.path_qs}")
            if not self.status_page_cache_html or time.time() > self.status_page_cache_timestamp + 3.0:
                colors = {1: 'black', 0.5: 'green', 0: 'lightgray'}
                time_str = str(datetime.now().isoformat(' ', 'seconds'))
                st = self.swarm.get_status_table()
                th = '<th>{txt}</th>\n'
                res = '<html ><head><meta http-equiv="refresh" content="4"></head>\n'\
                      '<body style="font-family: sans-serif; text-align: left;">\n'\
                      f"<h1>{Defaults.APP_NAME} {Defaults.APP_VERSION} – swarm status</h1><p>{time_str}</p>\n"
                if st['all_hashes']:
                    style = 'transform: scale(0.7); transform-origin: top left; white-space:nowrap; align: left;'
                    res += f'<table style="{style}"><tr>' + th.format(txt='Node') + \
                           th.format(txt='')*len(st["all_hashes"]) +\
                           th.format(txt='↓') + th.format(txt='↑') +\
                           th.format(txt='⧖') + '</tr>\n'
                    for n in st['nodes']:
                        res += f'\n<tr><td>{html.escape(n["name"])}</td>'
                        res += ''.join(['<td style="background: {color}">&nbsp;</td>\n'.format(
                            color=colors[c]) for c in n['hashes']])
                        transfer_avg = ('%.1f s' % n['avg_ul_time']) if n['avg_ul_time']>=0 else '–'
                        res += ''.join(f'<td>{v}</td>' for v in (int(n['dls']), int(n['uls']), transfer_avg))
                        res += "</tr>\n"
                    res += '</table><p>↓ = active downloads, ↑ = active uploads, ⧖ = average upload time</p>\n'
                else:
                    res += "(No data. Master is probably still hashing. Try again later.)"
                res += '</body></html>'

                self.status_page_cache_timestamp = time.time()
                self.status_page_cache_html = res

            return web.Response(text=self.status_page_cache_html, content_type='text/html')


        async def http_handler__start_websocket(request):
            self.status_func(log_info=f"[{request.remote}] HTTP GET {request.path_qs}. Upgrading to websocket.")
            ws = web.WebSocketResponse(heartbeat=20, autoping=True, receive_timeout=60)
            await ws.prepare(request)
            peer = PeerSession(node=None, version=None, sendq=asyncio.Queue(), address=str(request.remote))

            with _DEBUG_PIC_LOCK:
                loop = asyncio.get_running_loop()
                _DEBUG_PEER_INJECTOR_CALLBACKS[id(peer)] = lambda m: loop.call_soon_threadsafe(peer.sendq.put_nowait, m)

            # Read send_queue and pass them to websocket
            async def send_loop():
                while not ws.closed:
                    with suppress(asyncio.TimeoutError):
                        msg = await asyncio.wait_for(peer.sendq.get(), timeout=1)
                        if msg and not ws.closed:
                            await ws.send_json(msg, compress=9)
                        if msg and msg.get('action') == 'fatal':
                            self.status_func(log_info=f'Sent fatal error to client. Kicking them out: {str(msg)}"')
                            await ws.close()

            send_task = asyncio.create_task(send_loop())

            async def welcome():
                while not self.file_server.batch:
                    await peer.sendq.put({'action': 'ok', 'message': "Hold on. Master is doing initial file scan."})
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.replan_trigger.wait(), timeout=5)

                welcome = self.__make_batch_msg()
                welcome['action'] = 'initial_batch'
                welcome['message'] = 'Welcome. Hash your files against this and join_swarm when ready to sync.'
                await peer.sendq.put(welcome)

            welcome_task = asyncio.create_task(welcome())

            try:
                # Read messages from websocket and handle them
                while True:
                    try:
                        msg = await ws.__anext__()
                    except StopAsyncIteration:
                        break
                    except asyncio.CancelledError:
                        self.status_func(log_info=f'Websocket heartbeat lost on "{peer.name()}"')
                        await ws.close()
                        continue

                    if msg.type == WSMsgType.TEXT:
                        try:
                            if welcome_task.done():
                                await handle_client_msg(peer, msg.json())
                            else:
                                await peer.sendq.put(
                                    {'action': 'ok', 'message': "Hold on. Master is still doing initial file scan."})
                        except JSONDecodeError:
                            await peer.sendq.put({'action': 'fatal', 'message': 'Protocol error. Bad JSON. Goodbye.'})
                        except Exception as e:
                            self.status_func(log_error=f'Error ("{str(e)}") handling client msg: {msg.data}'
                                              'traceback: ' + traceback.format_exc())
                            await peer.sendq.put({'action': 'error', 'orig_msg': msg.data,
                                                  'message': 'Exception: ' + str(e)})
                    elif msg.type == WSMsgType.ERROR:
                        self.status_func(log_error=f'Connection for client "{peer.name()}" '
                                                    'closed with err: %s' % ws.exception())

                self.status_func(log_info=f'Websocket closed from "{peer.name()}"')

            except Exception as e:
                self.status_func(log_info=f'Exception while reading from ws:\n"{traceback.format_exc()}"')
                raise e
            finally:
                with _DEBUG_PIC_LOCK:
                    _DEBUG_PEER_INJECTOR_CALLBACKS.pop(id(peer), None)
                if peer.node:
                    self.status_func(log_info=f'Destroying node "{peer.name()}"')
                    peer.node.destroy()

            send_task.cancel()
            return ws

        # Start serving blobs over HTTP and accepting client connections on websocket endpoint
        file_io = FileIO(Path(base_dir), 0, ul_limit)
        server = self.file_server.create_http_server(
            port, file_io, https_cert, https_key,
            extra_routes=[
                web.get('/join', http_handler__start_websocket),
                web.get('/', http_handler__status)
            ])

        # Register this server as seed node
        self.seed_node = self.swarm.node_join(self.swarm.all_hashes, 0, concurrent_uploads, master_node=True)
        self.seed_node.name = 'MASTER'
        self.seed_node.client = SimpleNamespace(
            dl_url=self.file_server.base_url + '/blob/{hash}',
            send_queue=None)

        return server


    async def planner_loop(self):
        self.status_func(log_info=f'Planner loop starting.')
        while True:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.replan_trigger.wait(), timeout=2)
            self.replan_trigger.clear()

            # Track seed file server upload performance
            self.seed_node.set_active_transfers(downloads={}, n_uploads=self.file_server.active_uploads)
            self.seed_node.update_transfer_speed(self.file_server.upload_times)
            self.file_server.upload_times.clear()

            for t in self.swarm.plan_transfers():
                self.status_func(log_debug=f'Scheduling dl of {t.hash} from {t.from_node.name} to '
                                           f'{t.to_node.name}, timeout {t.timeout_secs}')
                await t.to_node.client.send_queue.put({
                    'action': 'download',
                    'hash': t.hash,
                    'timeout': t.timeout_secs,
                    'max_rate': t.max_bandwidth,
                    'url': t.from_node.client.dl_url.format(hash=t.hash)})


# ---------------------------------------------------------------------------------------------------


async def run_master_server(base_dir: str,
                            port: int = Defaults.TCP_PORT_MASTER,
                            dir_scan_interval: float = Defaults.DIR_SCAN_INTERVAL_MASTER,
                            status_func=None,
                            ul_limit: float = Defaults.BANDWIDTH_LIMIT_MBITS_PER_SEC,
                            concurrent_uploads: int = Defaults.CONCURRENT_TRANSFERS_MASTER,
                            chunk_size=Defaults.CHUNK_SIZE, disable_lz4=False,
                            max_workers=Defaults.MAX_WORKERS,
                            https_cert=None, https_key=None):

    # Mute asyncio task exceptions on KeyboardInterrupt / thread CancelledError
    kb_exit, loop = False, asyncio.get_event_loop()
    loop.set_exception_handler(lambda l, c: loop.default_exception_handler(c) if not kb_exit else None)
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=max_workers))

    server = MasterNode(status_func=status_func, chunk_size=chunk_size)

    async def dir_scanner_loop():
        """Periodically scan sync directory for changes"""
        def progress_func_adapter(cur_filename, file_progress, total_progress):
            status_func(progress=total_progress,
                        cur_status=f'Hashing ({cur_filename} / at {int(file_progress * 100 + 0.5)}%)')

        fio = FileIO(Path(base_dir))
        while True:
            # TODO: integrate with inotify (watchdog package) to avoid frequent rescans
            def scandir_blocking():
                return asyncio.run(scan_dir(
                    fio, max_chunk_size=chunk_size, old_batch=server.file_server.batch,
                    max_sub_chunk_size=int(chunk_size / Defaults.HASH_TASKS_PER_CHUNK),
                    progress_func=progress_func_adapter, test_compress=(not disable_lz4)))
            try:
                new_batch, errors = await loop.run_in_executor(None, scandir_blocking)
                for i, e in enumerate(errors):
                    status_func(log_error=f'- Dir scan error #{i}: {e}')
                if new_batch != server.file_server.batch:
                    status_func(cur_status=f'New file batch. Serving as master.')
                    await server.replace_sync_batch(new_batch)
            except FileNotFoundError as e:
                status_func(log_info=f'NOTE: Dir scan failed as file suddenly vanished (trying again in a bit): {e}')

            await asyncio.sleep(dir_scan_interval)

    try:
        await server.start_master_server(
            base_dir=base_dir, port=port, ul_limit=ul_limit,
            concurrent_uploads=concurrent_uploads, https_cert=https_cert, https_key=https_key)
        await asyncio.wait([
            dir_scanner_loop(),
            server.planner_loop(),
        ], return_when=asyncio.FIRST_COMPLETED)

    except (KeyboardInterrupt, concurrent.futures.CancelledError):
        status_func(log_info='User exit.')
        kb_exit = True
    except Exception as e:
        status_func(log_error='MasterNode error:\n' + traceback.format_exc(), popup=True)


async def async_main():
    args = parse_cli_args(is_master=True)
    status_func = json_status_func if args.json else make_human_cli_status_func(log_level_debug=args.debug)
    await run_master_server(
        base_dir=args.dir, port=args.port, ul_limit=args.ul_limit, concurrent_uploads=args.ct,
        dir_scan_interval=args.rescan_interval,  # https_cert=args.sslcert, https_key=args.sslkey,
        disable_lz4=args.no_compress, max_workers=args.max_workers,
        chunk_size=args.chunksize, status_func=status_func)

def main():
    with suppress(KeyboardInterrupt):
        asyncio.run(async_main())

if __name__ == "__main__":  # pragma: no cover
    main()
