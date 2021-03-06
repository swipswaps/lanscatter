from pathlib import Path, PurePosixPath
from aiohttp import web, ClientSession
from typing import Tuple, Optional, Dict, Iterable
from contextlib import suppress
import aiofiles, os, time, asyncio, aiohttp, platform, subprocess, sys

import lz4.frame
from types import SimpleNamespace

from .common import Defaults, process_multibuffer_io, file_read_producer
from .chunker import FileChunk
from .ratelimiter import RateLimiter

class FileIO:
    """
    Helper for reading and writing chunks from/to files + downloading / uploading them over network.
    Supports rate limiting and checks that file operations stay inside given base directory.
    """
    basedir: Path
    dl_limiter: RateLimiter
    ul_limiter: RateLimiter

    def __init__(self, basedir: Path, dl_rate_limit: float = 99999999, ul_rate_limit: float = 99999999):
        """
        :param basedir: Folder to limit read/write into.
        :param dl_rate_limit: Maximum download rate in Mbit/s
        :param ul_rate_limit: Maximum upload rate in Mbit/s
        """
        assert(isinstance(basedir, Path))
        basedir = basedir.resolve()
        if not basedir.is_dir():
            print(f'Error: Path "{basedir}" is not a directory. Cannot serve from it.', file=sys.stderr)
            sys.exit(1)
        self.basedir = basedir
        self.dl_limiter = RateLimiter(dl_rate_limit * 1024 * 1024 / 8, period=1.0, burst_factor=2.0)
        self.ul_limiter = RateLimiter(ul_rate_limit * 1024 * 1024 / 8, period=1.0, burst_factor=2.0)

    def resolve_and_sanitize(self, relative_path, must_exist=False) -> Path:
        """
        Check that given relative path stays inside basedir and return an absolute path string.
        :return: Absolute Path() object
        """
        assert '\\' not in str(relative_path), f"Non-posix path found: {str(relative_path)}"
        p = (self.basedir / relative_path).resolve()
        if self.basedir not in p.parents:
            raise PermissionError(f'Filename points outside basedir: {str(relative_path)}')
        if must_exist and not p.exists():
            raise FileNotFoundError(f"Required path does not exist: {str(p)}")
        return p

    def open_and_seek(self, rel_path: str, pos: int, for_write: bool = False):
        """
        Helper. Open file for read or write and seek/grow to given size.
        :param path: File to open
        :param pos: File position to seek/grow into
        :param for_write: If true, open for write, otherwise for read
        :return: Aiofiles file handle
        """
        path = self.resolve_and_sanitize(rel_path)
        if for_write:
            mode = ('r+b' if path.is_file() else 'wb')
            os.makedirs(os.path.dirname(path), exist_ok=True)
        else:
            if not path.is_file():
                raise FileNotFoundError(f'Cannot read, no such file: "{str(path)}"')
            mode = 'rb'

        # Return an "async with" compatible object that opens file and seeks
        class _TempAsyncMgr:
            async def __aenter__(self):
                self.f = await aiofiles.open(path, mode=mode)
                await self.f.seek(pos)
                self.f.name = rel_path
                return self.f
            async def __aexit__(self, exc_type, exc, tb):
                if for_write:
                    pass
                    await self.f.flush()
                    await asyncio.get_running_loop().run_in_executor(None, os.fsync, self.f.fileno())
                await self.f.close()
        return _TempAsyncMgr()


    async def upload_chunk(self, chunk: FileChunk, request: web.Request)\
            -> Tuple[web.StreamResponse, Optional[float], Optional[float]]:
        """
        Read given chunk from disk and stream out as a HTTP response.

        :param chunk: Chunk to read
        :param request: HTTP request to answer
        :param use_lz4: Compress with LZ4 if client accepts it
        :return: Tuple(Aiohttp.response, float(seconds the upload took) or None if it no progress was made, compr_ratio)
        """
        use_lz4 = (chunk.cmpratio < 0.95) and ('lz4' in str(request.headers.get('Accept-Encoding')))
        response = None
        upload_size = 0
        start_t = time.time()
        try:
            async with self.open_and_seek(chunk.path, chunk.pos, for_write=False) as inf:
                with lz4.frame.LZ4FrameCompressor() as lz:
                    # Ok, read chunk from file and stream it out
                    response = web.StreamResponse(
                        status=200,
                        reason='OK',
                        headers={'Content-Type': 'application/octet-stream', 'Content-Disposition': 'inline',
                                 'Content-Encoding': 'lz4' if use_lz4 else 'None'})
                    response.enable_compression(False)  # Make sure there's no double compression
                    await response.prepare(request)
                    if use_lz4:
                        await response.write(lz.begin())

                    async def write_http(buff: bytearray):
                        nonlocal upload_size
                        i, cnt = 0, len(buff)
                        while cnt > 0:
                            limited_n = int(await self.ul_limiter.acquire(cnt, Defaults.NETWORK_BUFFER_MIN))
                            raw = memoryview(buff)[i:(i + limited_n)]
                            out = lz.compress(raw) if use_lz4 else raw
                            upload_size += len(out)
                            await response.write(out)
                            self.dl_limiter.unspend(limited_n - len(out))
                            i += limited_n
                            cnt -= limited_n

                    await process_multibuffer_io(
                        producer=file_read_producer(inf, chunk.size), consumer=write_http, timeout=Defaults.TIMEOUT_WHEN_NO_PROGRESS,
                        initial_buffers=[bytearray(Defaults.FILE_BUFFER_SIZE) for i in range(5)])

                    if use_lz4:
                        await response.write(lz.flush())
                    return response, (time.time() - start_t), (upload_size / (chunk.size or 1))

        except asyncio.CancelledError as e:
            # If client disconnected, predict how long upload would have taken
            predicted_time = (time.time() - start_t) / (upload_size or 1) * chunk.size
            return response, predicted_time, None

        except PermissionError as e:
            raise web.HTTPForbidden(reason=str(e))
        except FileNotFoundError as e:
            raise web.HTTPNotFound(reason=str(e))
        except Exception as e:
            # TODO: Write some magic string into chunked response to indicate error and then append message?
            #   (problem: there's no way of signaling the error message to client after Status 200 has been sent.)
            raise web.HTTPInternalServerError(reason=str(e))
        finally:
            if response is not None:
                with suppress(Exception):
                    await response.write_eof()  # Close chunked response despite possible errors


    async def copy_chunk_locally(self, copy_from: FileChunk, copy_to: FileChunk) -> bool:
        """
        Locally copy chunk contents from one file (+position) to another.
        :param copy_from: Where to copy from
        :param copy_to: Where to copy into
        """
        if copy_from.path == copy_to.path and copy_from.pos == copy_to.pos:
            return True
        if copy_from.hash != copy_to.hash:
            raise ValueError(f"From and To chunks must have same hash (was: '{copy_from.hash}' vs '{copy_to.hash}').")

        async with self.open_and_seek(copy_from.path, copy_from.pos, for_write=False) as inf:
            if not inf:
                raise FileNotFoundError(f'Local copy failed, no such file: {str(copy_from.path)}')
            async with self.open_and_seek(copy_to.path, copy_to.pos, for_write=True) as outf:

                async def write_file(buff):
                    await outf.write(buff)

                await process_multibuffer_io(
                    producer=file_read_producer(inf, copy_from.size), consumer=write_file,
                    initial_buffers=[bytearray(Defaults.FILE_BUFFER_SIZE) for i in range(5)])

    async def download_chunk(self, chunk: FileChunk, url: str, http_session: ClientSession,
                             file_size: int= -1, max_rate: float = float('inf'), progr_func = None) -> None:
        """
        Download chunk from given URL and write directly into (the middle of a) file as specified by FileChunk.
        :param chunk: Specs for chunk to get
        :param url: URL to download from
        :param http_session: AioHTTP session to use for GET.
        :param file_size: Size of complete file (optional). File will be truncated to this size.
        :param max_rate: Maximum download rate, mbit/s
        :param progr_func: Progress reporting callback
        """
        with suppress(RuntimeError):  # Avoid dirty exit in aiofiles when Ctrl^C (RuntimeError('Event loop is closed')
            LMIN, LMAX = Defaults.DOWNLOAD_BUFFER_MAX, Defaults.NETWORK_BUFFER_MIN
            session_limiter = RateLimiter(max_rate * 1024 * 1024 / 8, period=1.0, burst_factor=2.0)
            limiters = (self.dl_limiter, session_limiter)

            aio_timeout = aiohttp.ClientTimeout(connect=Defaults.TIMEOUT_WHEN_NO_PROGRESS, sock_connect=Defaults.TIMEOUT_WHEN_NO_PROGRESS)
            async with http_session.get(url, headers={'Accept-Encoding': 'lz4'}, timeout=aio_timeout) as resp:
                if resp.status != 200:  # some error
                    raise IOError(f'HTTP status {resp.status}')
                else:
                    total_dl = 0
                    use_lz4 = 'lz4' in str(resp.headers.get('Content-Encoding'))
                    with lz4.frame.LZ4FrameDecompressor() as lz:
                        async with self.open_and_seek(chunk.path, chunk.pos, for_write=True) as outf:
                            progr_timer = RateLimiter(1.0, 2.0)

                            async def read_http(__):
                                limited_n = min([int(await lmt.acquire(LMIN, LMAX)) for lmt in limiters])
                                new_buff = await resp.content.read(limited_n)
                                for lmt in limiters:
                                    lmt.unspend(limited_n - len(new_buff))
                                return new_buff or None

                            async def write_file(buff):
                                nonlocal total_dl
                                data = lz.decompress(buff) if use_lz4 else buff
                                total_dl += len(data)
                                if progr_timer.try_acquire(1.0):
                                    progr_func(total_dl, file_size)
                                await outf.write(data)

                            await process_multibuffer_io(
                                producer=read_http, consumer=write_file, timeout=Defaults.TIMEOUT_WHEN_NO_PROGRESS,
                                initial_buffers=[True for i in range(5)])
                            progr_func(total_dl, file_size)

                            if file_size >= 0:
                                await outf.truncate(file_size)

    def try_precreate_large_sparse_file(self, path, size: int) -> bool:
        """
        Attempts to create a sparse file 'path' with given size, if it doesn't exist already.
        This is an optimization, so no guarantees are made that the file exists after the call.
        :return: True if complete success, False otherwise
        """
        def shell(command):
            res = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            return res.returncode == 0

        # On Windows, call the 'fsutil' command
        if any(platform.win32_ver()):
            with suppress(PermissionError, FileNotFoundError):
                p = self.resolve_and_sanitize(str(path))
                if not p.exists():
                    return shell(['fsutil', 'file', 'createnew', str(p.absolute()), str(size)]) and \
                           shell(['fsutil', 'sparse', 'setflag', str(p.absolute())]) and \
                           shell(['fsutil', 'sparse', 'setrange', str(p.absolute()), '0', str(size)])
        return False


    async def change_mtime(self, path, mtime):
        path = self.resolve_and_sanitize(path)
        os.utime(str(path), (mtime, mtime))

    async def create_folders(self, path):
        """Create given directory and all parents"""
        path = self.resolve_and_sanitize(path)
        os.makedirs(path, exist_ok=True)

    async def stat(self, path) -> SimpleNamespace:
        path = self.resolve_and_sanitize(path)
        return await aiofiles.os.stat(str(path))

    async def remove_file_and_paths(self, path):
        path = self.resolve_and_sanitize(path)
        if path.exists():
            if path.is_dir():
                with suppress(OSError):
                    path.rmdir()
            else:
                path.unlink()
        for d in path.parents:
            if d == self.basedir and d != '.':
                break
            with suppress(OSError):
                d.rmdir()
