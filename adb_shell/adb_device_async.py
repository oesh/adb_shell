# Copyright (c) 2020 Jeff Irion and contributors
#
# This file is part of the adb-shell package.  It incorporates work
# covered by the following license notice:
#
#
#   Copyright 2014 Google Inc. All rights reserved.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Implement the :class:`AdbDeviceAsync` class, which can connect to a device and run ADB shell commands.

* :class:`AdbDeviceAsync`

    * :meth:`AdbDeviceAsync._close`
    * :meth:`AdbDeviceAsync._filesync_flush`
    * :meth:`AdbDeviceAsync._filesync_read`
    * :meth:`AdbDeviceAsync._filesync_read_buffered`
    * :meth:`AdbDeviceAsync._filesync_read_until`
    * :meth:`AdbDeviceAsync._filesync_send`
    * :meth:`AdbDeviceAsync._handle_progress`
    * :meth:`AdbDeviceAsync._okay`
    * :meth:`AdbDeviceAsync._open`
    * :meth:`AdbDeviceAsync._pull`
    * :meth:`AdbDeviceAsync._push`
    * :meth:`AdbDeviceAsync._read`
    * :meth:`AdbDeviceAsync._read_until`
    * :meth:`AdbDeviceAsync._read_until_close`
    * :meth:`AdbDeviceAsync._send`
    * :meth:`AdbDeviceAsync._service`
    * :meth:`AdbDeviceAsync._streaming_command`
    * :meth:`AdbDeviceAsync._streaming_service`
    * :meth:`AdbDeviceAsync._write`
    * :attr:`AdbDeviceAsync.available`
    * :meth:`AdbDeviceAsync.close`
    * :meth:`AdbDeviceAsync.connect`
    * :meth:`AdbDeviceAsync.list`
    * :meth:`AdbDeviceAsync.pull`
    * :meth:`AdbDeviceAsync.push`
    * :meth:`AdbDeviceAsync.root`
    * :meth:`AdbDeviceAsync.shell`
    * :meth:`AdbDeviceAsync.stat`
    * :meth:`AdbDeviceAsync.streaming_shell`

* :class:`AdbDeviceTcpAsync`

"""


import io
import logging
import os
import socket
import struct
import time

from . import constants
from . import exceptions
from .adb_message import AdbMessage, checksum, unpack
from .handle.base_handle_async import BaseHandleAsync
from .handle.tcp_handle_async import TcpHandleAsync
from .hidden_helpers import FILE_TYPES, DeviceFile, _AdbTransactionInfo, _FileSyncTransactionInfo, _open


_LOGGER = logging.getLogger(__name__)


class AdbDeviceAsync(object):
    """A class with methods for connecting to a device and executing ADB commands.

    Parameters
    ----------
    handle : BaseHandleAsync
        A user-provided handle for communicating with the device; must be an instance of a subclass of :class:`~adb_shell.handle.base_handle_async.BaseHandleAsync`
    banner : str, bytes, None
        The hostname of the machine where the Python interpreter is currently running; if
        it is not provided, it will be determined via ``socket.gethostname()``

    Raises
    ------
    adb_shell.exceptions.InvalidHandleError
        The passed ``handle`` is not an instance of a subclass of :class:`~adb_shell.handle.base_handle_async.BaseHandleAsync`

    Attributes
    ----------
    _available : bool
        Whether an ADB connection to the device has been established
    _banner : bytearray, bytes
        The hostname of the machine where the Python interpreter is currently running
    _handle : BaseHandleAsync
        The handle that is used to connect to the device; must be a subclass of :class:`~adb_shell.handle.base_handle_async.BaseHandleAsync`

    """

    def __init__(self, handle, banner=None):
        if banner:
            if not isinstance(banner, (bytes, bytearray)):
                self._banner = bytearray(banner, 'utf-8')
            else:
                self._banner = banner
        else:
            try:
                # TODO: make this async / don't do I/O
                self._banner = bytearray(socket.gethostname(), 'utf-8')
            except:  # noqa pylint: disable=bare-except
                self._banner = bytearray('unknown', 'utf-8')

        if not isinstance(handle, BaseHandleAsync):
            raise exceptions.InvalidHandleError("`handle` must be an instance of a subclass of `BaseHandleAsync`")

        self._handle = handle

        self._available = False

    @property
    def available(self):
        """Whether or not an ADB connection to the device has been established.

        Returns
        -------
        bool
            ``self._available``

        """
        return self._available

    async def close(self):
        """Close the connection via the provided handle's ``close()`` method.

        """
        self._available = False
        await self._handle.close()

    async def connect(self, rsa_keys=None, timeout_s=None, auth_timeout_s=constants.DEFAULT_AUTH_TIMEOUT_S, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S, auth_callback=None):
        """Establish an ADB connection to the device.

        1. Use the handle to establish a connection
        2. Send a ``b'CNXN'`` message
        3. Unpack the ``cmd``, ``arg0``, ``arg1``, and ``banner`` fields from the response
        4. If ``cmd`` is not ``b'AUTH'``, then authentication is not necesary and so we are done
        5. If no ``rsa_keys`` are provided, raise an exception
        6. Loop through our keys, signing the last ``banner`` that we received

            1. If the last ``arg0`` was not :const:`adb_shell.constants.AUTH_TOKEN`, raise an exception
            2. Sign the last ``banner`` and send it in an ``b'AUTH'`` message
            3. Unpack the ``cmd``, ``arg0``, and ``banner`` fields from the response via :func:`adb_shell.adb_message.unpack`
            4. If ``cmd`` is ``b'CNXN'``, return ``banner``

        7. None of the keys worked, so send ``rsa_keys[0]``'s public key; if the response does not time out, we must have connected successfully


        Parameters
        ----------
        rsa_keys : list, None
            A list of signers of type :class:`~adb_shell.auth.sign_cryptography.CryptographySigner`,
            :class:`~adb_shell.auth.sign_pycryptodome.PycryptodomeAuthSigner`, or :class:`~adb_shell.auth.sign_pythonrsa.PythonRSASigner`
        timeout_s : float, None
            Timeout in seconds for sending and receiving packets, or ``None``; see :meth:`BaseHandleAsync.bulk_read() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_read>`
            and :meth:`BaseHandleAsync.bulk_write() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_write>`
        auth_timeout_s : float, None
            The time in seconds to wait for a ``b'CNXN'`` authentication response
        total_timeout_s : float
            The total time in seconds to wait for expected commands in :meth:`AdbDeviceAsync._read`
        auth_callback : function, None
            Function callback invoked when the connection needs to be accepted on the device

        Returns
        -------
        bool
            Whether the connection was established (:attr:`AdbDeviceAsync.available`)

        Raises
        ------
        adb_shell.exceptions.DeviceAuthError
            Device authentication required, no keys available
        adb_shell.exceptions.InvalidResponseError
            Invalid auth response from the device

        """
        # 1. Use the handle to establish a connection
        await self._handle.close()
        await self._handle.connect(timeout_s)

        # 2. Send a ``b'CNXN'`` message
        msg = AdbMessage(constants.CNXN, constants.VERSION, constants.MAX_ADB_DATA, b'host::%s\0' % self._banner)
        adb_info = _AdbTransactionInfo(None, None, timeout_s, total_timeout_s)
        await self._send(msg, adb_info)

        # 3. Unpack the ``cmd``, ``arg0``, ``arg1``, and ``banner`` fields from the response
        cmd, arg0, arg1, banner = await self._read([constants.AUTH, constants.CNXN], adb_info)

        # 4. If ``cmd`` is not ``b'AUTH'``, then authentication is not necesary and so we are done
        if cmd != constants.AUTH:
            self._available = True
            return True  # return banner

        # 5. If no ``rsa_keys`` are provided, raise an exception
        if not rsa_keys:
            await self._handle.close()
            raise exceptions.DeviceAuthError('Device authentication required, no keys available.')

        # 6. Loop through our keys, signing the last ``banner`` that we received
        for rsa_key in rsa_keys:
            # 6.1. If the last ``arg0`` was not :const:`adb_shell.constants.AUTH_TOKEN`, raise an exception
            if arg0 != constants.AUTH_TOKEN:
                await self._handle.close()
                raise exceptions.InvalidResponseError('Unknown AUTH response: %s %s %s' % (arg0, arg1, banner))

            # 6.2. Sign the last ``banner`` and send it in an ``b'AUTH'`` message
            signed_token = rsa_key.Sign(banner)
            msg = AdbMessage(constants.AUTH, constants.AUTH_SIGNATURE, 0, signed_token)
            await self._send(msg, adb_info)

            # 6.3. Unpack the ``cmd``, ``arg0``, and ``banner`` fields from the response via :func:`adb_shell.adb_message.unpack`
            cmd, arg0, _, banner = await self._read([constants.CNXN, constants.AUTH], adb_info)

            # 6.4. If ``cmd`` is ``b'CNXN'``, return ``banner``
            if cmd == constants.CNXN:
                self._available = True
                return True  # return banner

        # 7. None of the keys worked, so send ``rsa_keys[0]``'s public key; if the response does not time out, we must have connected successfully
        pubkey = rsa_keys[0].GetPublicKey()
        if not isinstance(pubkey, (bytes, bytearray)):
            pubkey = bytearray(pubkey, 'utf-8')

        if auth_callback is not None:
            auth_callback(self)

        msg = AdbMessage(constants.AUTH, constants.AUTH_RSAPUBLICKEY, 0, pubkey + b'\0')
        await self._send(msg, adb_info)

        adb_info.timeout_s = auth_timeout_s
        cmd, arg0, _, banner = await self._read([constants.CNXN], adb_info)
        self._available = True
        return True  # return banner

    # ======================================================================= #
    #                                                                         #
    #                                 Services                                #
    #                                                                         #
    # ======================================================================= #
    async def _service(self, service, command, timeout_s=None, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S, decode=True):
        """Send an ADB command to the device.

        Parameters
        ----------
        service : bytes
            The ADB service to talk to (e.g., ``b'shell'``)
        command : bytes
            The command that will be sent
        timeout_s : float, None
            Timeout in seconds for sending and receiving packets, or ``None``; see :meth:`BaseHandleAsync.bulk_read() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_read>`
            and :meth:`BaseHandleAsync.bulk_write() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_write>`
        total_timeout_s : float
            The total time in seconds to wait for a ``b'CLSE'`` or ``b'OKAY'`` command in :meth:`AdbDeviceAsync._read`
        decode : bool
            Whether to decode the output to utf8 before returning

        Returns
        -------
        bytes, str
            The output of the ADB command as a string if ``decode`` is True, otherwise as bytes.

        """
        adb_info = _AdbTransactionInfo(None, None, timeout_s, total_timeout_s)
        if decode:
            return b''.join([x async for x in self._streaming_command(service, command, adb_info)]).decode('utf8')
        return b''.join([x async for x in self._streaming_command(service, command, adb_info)])

    async def _streaming_service(self, service, command, timeout_s=None, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S, decode=True):
        """Send an ADB command to the device, yielding each line of output.

        Parameters
        ----------
        service : bytes
            The ADB service to talk to (e.g., ``b'shell'``)
        command : bytes
            The command that will be sent
        timeout_s : float, None
            Timeout in seconds for sending and receiving packets, or ``None``; see :meth:`BaseHandleAsync.bulk_read() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_read>`
            and :meth:`BaseHandleAsync.bulk_write() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_write>`
        total_timeout_s : float
            The total time in seconds to wait for a ``b'CLSE'`` or ``b'OKAY'`` command in :meth:`AdbDeviceAsync._read`
        decode : bool
            Whether to decode the output to utf8 before returning

        Yields
        -------
        bytes, str
            The line-by-line output of the ADB command as a string if ``decode`` is True, otherwise as bytes.

        """
        adb_info = _AdbTransactionInfo(None, None, timeout_s, total_timeout_s)
        stream = self._streaming_command(service, command, adb_info)
        if decode:
            async for line in (stream_line.decode('utf8') async for stream_line in stream):
                yield line
        else:
            async for line in stream:
                yield line

    async def root(self, timeout_s=None, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S):
        """Gain root access.

        The device must be rooted in order for this to work.

        Parameters
        ----------
        timeout_s : float, None
            Timeout in seconds for sending and receiving packets, or ``None``; see :meth:`BaseHandleAsync.bulk_read() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_read>`
            and :meth:`BaseHandleAsync.bulk_write() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_write>`
        total_timeout_s : float
            The total time in seconds to wait for a ``b'CLSE'`` or ``b'OKAY'`` command in :meth:`AdbDeviceAsync._read`

        """
        if not self.available:
            raise exceptions.AdbConnectionError("ADB command not sent because a connection to the device has not been established.  (Did you call `AdbDeviceAsync.connect()`?)")

        await self._service(b'root', b'', timeout_s, total_timeout_s, False)

    async def shell(self, command, timeout_s=None, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S, decode=True):
        """Send an ADB shell command to the device.

        Parameters
        ----------
        command : str
            The shell command that will be sent
        timeout_s : float, None
            Timeout in seconds for sending and receiving packets, or ``None``; see :meth:`BaseHandleAsync.bulk_read() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_read>`
            and :meth:`BaseHandleAsync.bulk_write() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_write>`
        total_timeout_s : float
            The total time in seconds to wait for a ``b'CLSE'`` or ``b'OKAY'`` command in :meth:`AdbDeviceAsync._read`
        decode : bool
            Whether to decode the output to utf8 before returning

        Returns
        -------
        bytes, str
            The output of the ADB shell command as a string if ``decode`` is True, otherwise as bytes.

        """
        if not self.available:
            raise exceptions.AdbConnectionError("ADB command not sent because a connection to the device has not been established.  (Did you call `AdbDeviceAsync.connect()`?)")

        return await self._service(b'shell', command.encode('utf8'), timeout_s, total_timeout_s, decode)

    async def streaming_shell(self, command, timeout_s=None, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S, decode=True):
        """Send an ADB shell command to the device, yielding each line of output.

        Parameters
        ----------
        command : str
            The shell command that will be sent
        timeout_s : float, None
            Timeout in seconds for sending and receiving packets, or ``None``; see :meth:`BaseHandleAsync.bulk_read() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_read>`
            and :meth:`BaseHandleAsync.bulk_write() <adb_shell.handle.base_handle_async.BaseHandleAsync.bulk_write>`
        total_timeout_s : float
            The total time in seconds to wait for a ``b'CLSE'`` or ``b'OKAY'`` command in :meth:`AdbDeviceAsync._read`
        decode : bool
            Whether to decode the output to utf8 before returning

        Yields
        -------
        bytes, str
            The line-by-line output of the ADB shell command as a string if ``decode`` is True, otherwise as bytes.

        """
        if not self.available:
            raise exceptions.AdbConnectionError("ADB command not sent because a connection to the device has not been established.  (Did you call `AdbDeviceAsync.connect()`?)")

        async for line in self._streaming_service(b'shell', command.encode('utf8'), timeout_s, total_timeout_s, decode):
            yield line

    # ======================================================================= #
    #                                                                         #
    #                                 FileSync                                #
    #                                                                         #
    # ======================================================================= #
    async def list(self, device_path, timeout_s=None, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S):
        """Return a directory listing of the given path.

        Parameters
        ----------
        device_path : str
            Directory to list.
        timeout_s : float, None
            Expected timeout for any part of the pull.
        total_timeout_s : float
            The total time in seconds to wait for a ``b'CLSE'`` or ``b'OKAY'`` command in :meth:`AdbDeviceAsync._read`

        Returns
        -------
        files : list[DeviceFile]
            Filename, mode, size, and mtime info for the files in the directory

        """
        if not self.available:
            raise exceptions.AdbConnectionError("ADB command not sent because a connection to the device has not been established.  (Did you call `AdbDeviceAsync.connect()`?)")

        adb_info = _AdbTransactionInfo(None, None, timeout_s, total_timeout_s)
        filesync_info = _FileSyncTransactionInfo(constants.FILESYNC_LIST_FORMAT)
        await self._open(b'sync:', adb_info)

        await self._filesync_send(constants.LIST, adb_info, filesync_info, data=device_path)
        files = []

        async for cmd_id, header, filename in self._filesync_read_until([constants.DENT], [constants.DONE], adb_info, filesync_info):
            if cmd_id == constants.DONE:
                break

            mode, size, mtime = header
            files.append(DeviceFile(filename, mode, size, mtime))

        await self._close(adb_info)

        return files

    async def pull(self, device_filename, dest_file=None, progress_callback=None, timeout_s=None, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S):
        """Pull a file from the device.

        Parameters
        ----------
        device_filename : str
            Filename on the device to pull.
        dest_file : str, file, io.IOBase, None
            If set, a filename or writable file-like object.
        progress_callback : function, None
            Callback method that accepts filename, bytes_written and total_bytes, total_bytes will be -1 for file-like
            objects
        timeout_s : float, None
            Expected timeout for any part of the pull.
        total_timeout_s : float
            The total time in seconds to wait for a ``b'CLSE'`` or ``b'OKAY'`` command in :meth:`AdbDeviceAsync._read`

        Returns
        -------
        bytes, bool
            The file data if ``dest_file`` is not set. Otherwise, ``True`` if the destination file exists

        Raises
        ------
        ValueError
            If ``dest_file`` is of unknown type.

        """
        if not self.available:
            raise exceptions.AdbConnectionError("ADB command not sent because a connection to the device has not been established.  (Did you call `AdbDeviceAsync.connect()`?)")

        if not dest_file:
            dest_file = io.BytesIO()

        if not isinstance(dest_file, FILE_TYPES + (str,)):
            raise ValueError("dest_file is of unknown type")

        adb_info = _AdbTransactionInfo(None, None, timeout_s, total_timeout_s)
        filesync_info = _FileSyncTransactionInfo(constants.FILESYNC_PULL_FORMAT)

        with _open(dest_file, 'wb') as dest:
            await self._open(b'sync:', adb_info)
            await self._pull(device_filename, dest, progress_callback, adb_info, filesync_info)
            await self._close(adb_info)

            if isinstance(dest, io.BytesIO):
                return dest.getvalue()

            if hasattr(dest, 'name'):
                return os.path.exists(dest.name)

            # We don't know what the path is, so we just assume it exists.
            return True

    async def _pull(self, filename, dest, progress_callback, adb_info, filesync_info):
        """Pull a file from the device into the file-like ``dest_file``.

        Parameters
        ----------
        filename : str
            The file to be pulled
        dest : _io.BytesIO
            File-like object for writing to
        progress_callback : function, None
            Callback method that accepts ``filename``, ``bytes_written``, and ``total_bytes``
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction
        filesync_info : _FileSyncTransactionInfo
            Data and storage for this FileSync transaction

        """
        if progress_callback:
            total_bytes = await self.stat(filename)[1]
            progress = self._handle_progress(lambda current: progress_callback(filename, current, total_bytes))
            next(progress)

        await self._filesync_send(constants.RECV, adb_info, filesync_info, data=filename)
        async for cmd_id, _, data in self._filesync_read_until([constants.DATA], [constants.DONE], adb_info, filesync_info):
            if cmd_id == constants.DONE:
                break

            dest.write(data)
            if progress_callback:
                progress.send(len(data))

    async def push(self, source_file, device_filename, st_mode=constants.DEFAULT_PUSH_MODE, mtime=0, progress_callback=None, timeout_s=None, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S):
        """Push a file or directory to the device.

        Parameters
        ----------
        source_file : str
            Either a filename, a directory or file-like object to push to the device.
        device_filename : str
            Destination on the device to write to.
        st_mode : int
            Stat mode for filename
        mtime : int
            Modification time to set on the file.
        progress_callback : function, None
            Callback method that accepts filename, bytes_written and total_bytes, total_bytes will be -1 for file-like
            objects
        timeout_s : float, None
            Expected timeout for any part of the push.
        total_timeout_s : float
            The total time in seconds to wait for a ``b'CLSE'`` or ``b'OKAY'`` command in :meth:`AdbDeviceAsync._read`

        """
        if not self.available:
            raise exceptions.AdbConnectionError("ADB command not sent because a connection to the device has not been established.  (Did you call `AdbDeviceAsync.connect()`?)")

        if isinstance(source_file, str):
            if os.path.isdir(source_file):
                await self.shell("mkdir " + device_filename, timeout_s, total_timeout_s)
                for f in os.listdir(source_file):
                    await self.push(os.path.join(source_file, f), device_filename + '/' + f, progress_callback=progress_callback)
                return

        adb_info = _AdbTransactionInfo(None, None, timeout_s, total_timeout_s)
        filesync_info = _FileSyncTransactionInfo(constants.FILESYNC_PUSH_FORMAT)

        with _open(source_file, 'rb') as source:
            await self._open(b'sync:', adb_info)
            await self._push(source, device_filename, st_mode, mtime, progress_callback, adb_info, filesync_info)

        await self._close(adb_info)

    async def _push(self, datafile, filename, st_mode, mtime, progress_callback, adb_info, filesync_info):
        """Push a file-like object to the device.

        Parameters
        ----------
        datafile : _io.BytesIO
            File-like object for reading from
        filename : str
            Filename to push to
        st_mode : int
            Stat mode for filename
        mtime : int
            Modification time
        progress_callback : function, None
            Callback method that accepts ``filename``, ``bytes_written``, and ``total_bytes``
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        Raises
        ------
        PushFailedError
            Raised on push failure.

        """
        fileinfo = ('{},{}'.format(filename, int(st_mode))).encode('utf-8')

        await self._filesync_send(constants.SEND, adb_info, filesync_info, data=fileinfo)

        if progress_callback:
            total_bytes = os.fstat(datafile.fileno()).st_size if isinstance(datafile, FILE_TYPES) else -1
            progress = self._handle_progress(lambda current: progress_callback(filename, current, total_bytes))
            next(progress)

        while True:
            data = datafile.read(constants.MAX_PUSH_DATA)
            if data:
                await self._filesync_send(constants.DATA, adb_info, filesync_info, data=data)

                if progress_callback:
                    progress.send(len(data))
            else:
                break

        if mtime == 0:
            mtime = int(time.time())

        # DONE doesn't send data, but it hides the last bit of data in the size field.
        await self._filesync_send(constants.DONE, adb_info, filesync_info, size=mtime)
        async for cmd_id, _, data in self._filesync_read_until([], [constants.OKAY, constants.FAIL], adb_info, filesync_info):
            if cmd_id == constants.OKAY:
                return

            raise exceptions.PushFailedError(data)

    async def stat(self, device_filename, timeout_s=None, total_timeout_s=constants.DEFAULT_TOTAL_TIMEOUT_S):
        """Get a file's ``stat()`` information.

        Parameters
        ----------
        device_filename : str
            The file on the device for which we will get information.
        timeout_s : float, None
            Expected timeout for any part of the pull.
        total_timeout_s : float
            The total time in seconds to wait for a ``b'CLSE'`` or ``b'OKAY'`` command in :meth:`AdbDeviceAsync._read`

        Returns
        -------
        mode : int
            The octal permissions for the file
        size : int
            The size of the file
        mtime : int
            The last modified time for the file

        """
        if not self.available:
            raise exceptions.AdbConnectionError("ADB command not sent because a connection to the device has not been established.  (Did you call `AdbDeviceAsync.connect()`?)")

        adb_info = _AdbTransactionInfo(None, None, timeout_s, total_timeout_s)
        await self._open(b'sync:', adb_info)

        filesync_info = _FileSyncTransactionInfo(constants.FILESYNC_STAT_FORMAT)
        await self._filesync_send(constants.STAT, adb_info, filesync_info, data=device_filename)
        _, (mode, size, mtime), _ = await self._filesync_read([constants.STAT], adb_info, filesync_info, read_data=False)
        await self._close(adb_info)

        return mode, size, mtime

    # ======================================================================= #
    #                                                                         #
    #                              Hidden Methods                             #
    #                                                                         #
    # ======================================================================= #
    async def _close(self, adb_info):
        """Send a ``b'CLSE'`` message.

        .. warning::

           This is not to be confused with the :meth:`AdbDeviceAsync.close` method!


        Parameters
        ----------
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        """
        msg = AdbMessage(constants.CLSE, adb_info.local_id, adb_info.remote_id)
        await self._send(msg, adb_info)
        await self._read_until([constants.CLSE], adb_info)

    async def _okay(self, adb_info):
        """Send an ``b'OKAY'`` mesage.

        Parameters
        ----------
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        """
        msg = AdbMessage(constants.OKAY, adb_info.local_id, adb_info.remote_id)
        await self._send(msg, adb_info)

    async def _open(self, destination, adb_info):
        """Opens a new connection to the device via an ``b'OPEN'`` message.

        1. :meth:`~AdbDeviceAsync._send` an ``b'OPEN'`` command to the device that specifies the ``local_id``
        2. :meth:`~AdbDeviceAsync._read` a response from the device that includes a command, another local ID (``their_local_id``), and ``remote_id``

            * If ``local_id`` and ``their_local_id`` do not match, raise an exception.
            * If the received command is ``b'CLSE'``, :meth:`~AdbDeviceAsync._read` another response from the device
            * If the received command is not ``b'OKAY'``, raise an exception
            * Set the ``adb_info.local_id`` and ``adb_info.remote_id`` attributes


        Parameters
        ----------
        destination : bytes
            ``b'SERVICE:COMMAND'``
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        Raises
        ------
        adb_shell.exceptions.InvalidResponseError
            Wrong local_id sent to us.

        """
        adb_info.local_id = 1
        msg = AdbMessage(constants.OPEN, adb_info.local_id, 0, destination + b'\0')
        await self._send(msg, adb_info)
        _, adb_info.remote_id, their_local_id, _ = await self._read([constants.OKAY], adb_info)

        if adb_info.local_id != their_local_id:
            raise exceptions.InvalidResponseError('Expected the local_id to be {}, got {}'.format(adb_info.local_id, their_local_id))

    async def _read(self, expected_cmds, adb_info):
        """Receive a response from the device.

        1. Read a message from the device and unpack the ``cmd``, ``arg0``, ``arg1``, ``data_length``, and ``data_checksum`` fields
        2. If ``cmd`` is not a recognized command in :const:`adb_shell.constants.WIRE_TO_ID`, raise an exception
        3. If the time has exceeded ``total_timeout_s``, raise an exception
        4. Read ``data_length`` bytes from the device
        5. If the checksum of the read data does not match ``data_checksum``, raise an exception
        6. Return ``command``, ``arg0``, ``arg1``, and ``bytes(data)``


        Parameters
        ----------
        expected_cmds : list[bytes]
            We will read packets until we encounter one whose "command" field is in ``expected_cmds``
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        Returns
        -------
        command : bytes
            The received command, which is in :const:`adb_shell.constants.WIRE_TO_ID` and must be in ``expected_cmds``
        arg0 : int
            TODO
        arg1 : int
            TODO
        bytes
            The data that was read

        Raises
        ------
        adb_shell.exceptions.InvalidCommandError
            Unknown command *or* never got one of the expected responses.
        adb_shell.exceptions.InvalidChecksumError
            Received checksum does not match the expected checksum.

        """
        start = time.time()

        while True:
            msg = await self._handle.bulk_read(constants.MESSAGE_SIZE, adb_info.timeout_s)
            _LOGGER.debug("bulk_read(%d): %s", constants.MESSAGE_SIZE, repr(msg))
            cmd, arg0, arg1, data_length, data_checksum = unpack(msg)
            command = constants.WIRE_TO_ID.get(cmd)

            if not command:
                raise exceptions.InvalidCommandError('Unknown command: %x' % cmd, cmd, (arg0, arg1))

            if command in expected_cmds:
                break

            if time.time() - start > adb_info.total_timeout_s:
                raise exceptions.InvalidCommandError('Never got one of the expected responses (%s)' % expected_cmds, cmd, (adb_info.timeout_s, adb_info.total_timeout_s))

        if data_length > 0:
            data = bytearray()
            while data_length > 0:
                temp = await self._handle.bulk_read(data_length, adb_info.timeout_s)
                _LOGGER.debug("bulk_read(%d): %s", data_length, repr(temp))

                data += temp
                data_length -= len(temp)

            actual_checksum = checksum(data)
            if actual_checksum != data_checksum:
                raise exceptions.InvalidChecksumError('Received checksum {0} != {1}'.format(actual_checksum, data_checksum))

        else:
            data = bytearray()

        return command, arg0, arg1, bytes(data)

    async def _read_until(self, expected_cmds, adb_info):
        """Read a packet, acknowledging any write packets.

        1. Read data via :meth:`AdbDeviceAsync._read`
        2. If a ``b'WRTE'`` packet is received, send an ``b'OKAY'`` packet via :meth:`AdbDeviceAsync._okay`
        3. Return the ``cmd`` and ``data`` that were read by :meth:`AdbDeviceAsync._read`


        Parameters
        ----------
        expected_cmds : list[bytes]
            :meth:`AdbDeviceAsync._read` with look for a packet whose command is in ``expected_cmds``
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        Returns
        -------
        cmd : bytes
            The command that was received by :meth:`AdbDeviceAsync._read`, which is in :const:`adb_shell.constants.WIRE_TO_ID` and must be in ``expected_cmds``
        data : bytes
            The data that was received by :meth:`AdbDeviceAsync._read`

        Raises
        ------
        adb_shell.exceptions.InterleavedDataError
            We don't support multiple streams...
        adb_shell.exceptions.InvalidResponseError
            Incorrect remote id.
        adb_shell.exceptions.InvalidCommandError
            Never got one of the expected responses.

        """
        start = time.time()

        while True:
            cmd, remote_id2, local_id2, data = await self._read(expected_cmds, adb_info)

            if local_id2 not in (0, adb_info.local_id):
                raise exceptions.InterleavedDataError("We don't support multiple streams...")

            if remote_id2 in (0, adb_info.remote_id):
                break

            if time.time() - start > adb_info.total_timeout_s:
                raise exceptions.InvalidCommandError('Never got one of the expected responses (%s)' % expected_cmds, cmd, (adb_info.timeout_s, adb_info.total_timeout_s))

            # Ignore CLSE responses to previous commands
            # https://github.com/JeffLIrion/adb_shell/pull/14
            if cmd != constants.CLSE:
                raise exceptions.InvalidResponseError('Incorrect remote id, expected {0} got {1}'.format(adb_info.remote_id, remote_id2))

        # Acknowledge write packets
        if cmd == constants.WRTE:
            await self._okay(adb_info)

        return cmd, data

    async def _read_until_close(self, adb_info):
        """Yield packets until a ``b'CLSE'`` packet is received.

        1. Read the ``cmd`` and ``data`` fields from a ``b'CLSE'`` or ``b'WRTE'`` packet via :meth:`AdbDeviceAsync._read_until`
        2. If ``cmd`` is ``b'CLSE'``, then send a ``b'CLSE'`` message and stop
        3. If ``cmd`` is not ``b'WRTE'``, raise an exception

            * If ``cmd`` is ``b'FAIL'``, raise :class:`~adb_shell.exceptions.AdbCommandFailureException`
            * Otherwise, raise :class:`~~adb_shell.exceptions.InvalidCommandError`

        4. Yield ``data`` and repeat


        Parameters
        ----------
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        Yields
        ------
        data : bytes
            The data that was read by :meth:`AdbDeviceAsync._read_until`

        """
        while True:
            cmd, data = await self._read_until([constants.CLSE, constants.WRTE], adb_info)

            if cmd == constants.CLSE:
                msg = AdbMessage(constants.CLSE, adb_info.local_id, adb_info.remote_id)
                await self._send(msg, adb_info)
                break

            yield data

    async def _send(self, msg, adb_info):
        """Send a message to the device.

        1. Send the message header (:meth:`adb_shell.adb_message.AdbMessage.pack <AdbMessage.pack>`)
        2. Send the message data


        Parameters
        ----------
        msg : AdbMessage
            The data that will be sent
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        """
        _LOGGER.debug("bulk_write: %s", repr(msg.pack()))
        await self._handle.bulk_write(msg.pack(), adb_info.timeout_s)
        _LOGGER.debug("bulk_write: %s", repr(msg.data))
        await self._handle.bulk_write(msg.data, adb_info.timeout_s)

    async def _streaming_command(self, service, command, adb_info):
        """One complete set of USB packets for a single command.

        1. :meth:`~AdbDeviceAsync._open` a new connection to the device, where the ``destination`` parameter is ``service:command``
        2. Read the response data via :meth:`AdbDeviceAsync._read_until_close`


        .. note::

           All the data is held in memory, and thus large responses will be slow and can fill up memory.


        Parameters
        ----------
        service : bytes
            The ADB service (e.g., ``b'shell'``, as used by :meth:`AdbDeviceAsync.shell`)
        command : bytes
            The service command
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        Yields
        ------
        bytes
            The responses from the service.

        """
        await self._open(b'%s:%s' % (service, command), adb_info)

        async for data in self._read_until_close(adb_info):
            yield data

    async def _write(self, data, adb_info):
        """Write a packet and expect an Ack.

        Parameters
        ----------
        data : bytes
            The data that will be sent
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction

        """
        msg = AdbMessage(constants.WRTE, adb_info.local_id, adb_info.remote_id, data)
        await self._send(msg, adb_info)

        # Expect an ack in response.
        await self._read_until([constants.OKAY], adb_info)

    # ======================================================================= #
    #                                                                         #
    #                         FileSync Hidden Methods                         #
    #                                                                         #
    # ======================================================================= #
    async def _filesync_flush(self, adb_info, filesync_info):
        """Write the data in the buffer up to ``filesync_info.send_idx``, then set ``filesync_info.send_idx`` to 0.

        Parameters
        ----------
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction
        filesync_info : _FileSyncTransactionInfo
            Data and storage for this FileSync transaction

        """
        await self._write(filesync_info.send_buffer[:filesync_info.send_idx], adb_info)
        filesync_info.send_idx = 0

    async def _filesync_read(self, expected_ids, adb_info, filesync_info, read_data=True):
        """Read ADB messages and return FileSync packets.

        Parameters
        ----------
        expected_ids : tuple[bytes]
            If the received header ID is not in ``expected_ids``, an exception will be raised
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction
        filesync_info : _FileSyncTransactionInfo
            Data and storage for this FileSync transaction
        read_data : bool
            Whether to read the received data

        Returns
        -------
        command_id : bytes
            The received header ID
        tuple
            The contents of the header
        data : bytearray, None
            The received data, or ``None`` if ``read_data`` is False

        Raises
        ------
        adb_shell.exceptions.AdbCommandFailureException
            Command failed
        adb_shell.exceptions.InvalidResponseError
            Received response was not in ``expected_ids``

        """
        if filesync_info.send_idx:
            await self._filesync_flush(adb_info, filesync_info)

        # Read one filesync packet off the recv buffer.
        header_data = await self._filesync_read_buffered(filesync_info.recv_message_size, adb_info, filesync_info)
        header = struct.unpack(filesync_info.recv_message_format, header_data)
        # Header is (ID, ...).
        command_id = constants.FILESYNC_WIRE_TO_ID[header[0]]

        if command_id not in expected_ids:
            if command_id == constants.FAIL:
                reason = ''
                if filesync_info.recv_buffer:
                    reason = filesync_info.recv_buffer.decode('utf-8', errors='ignore')

                raise exceptions.AdbCommandFailureException('Command failed: {}'.format(reason))

            raise exceptions.InvalidResponseError('Expected one of %s, got %s' % (expected_ids, command_id))

        if not read_data:
            return command_id, header[1:], None

        # Header is (ID, ..., size).
        size = header[-1]
        data = await self._filesync_read_buffered(size, adb_info, filesync_info)

        return command_id, header[1:-1], data

    async def _filesync_read_buffered(self, size, adb_info, filesync_info):
        """Read ``size`` bytes of data from ``self.recv_buffer``.

        Parameters
        ----------
        size : int
            The amount of data to read
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction
        filesync_info : _FileSyncTransactionInfo
            Data and storage for this FileSync transaction

        Returns
        -------
        result : bytearray
            The read data

        """
        # Ensure recv buffer has enough data.
        while len(filesync_info.recv_buffer) < size:
            _, data = await self._read_until([constants.WRTE], adb_info)
            filesync_info.recv_buffer += data

        result = filesync_info.recv_buffer[:size]
        filesync_info.recv_buffer = filesync_info.recv_buffer[size:]
        return result

    async def _filesync_read_until(self, expected_ids, finish_ids, adb_info, filesync_info):
        """Useful wrapper around :meth:`AdbDeviceAsync._filesync_read`.

        Parameters
        ----------
        expected_ids : tuple[bytes]
            If the received header ID is not in ``expected_ids``, an exception will be raised
        finish_ids : tuple[bytes]
            We will read until we find a header ID that is in ``finish_ids``
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction
        filesync_info : _FileSyncTransactionInfo
            Data and storage for this FileSync transaction

        Yields
        ------
        cmd_id : bytes
            The received header ID
        header : tuple
            TODO
        data : bytearray
            The received data

        """
        while True:
            cmd_id, header, data = await self._filesync_read(expected_ids + finish_ids, adb_info, filesync_info)
            yield cmd_id, header, data

            # These lines are not reachable because whenever this method is called and `cmd_id` is in `finish_ids`, the code
            # either breaks (`list` and `_pull`), returns (`_push`), or raises an exception (`_push`)
            if cmd_id in finish_ids:  # pragma: no cover
                break

    async def _filesync_send(self, command_id, adb_info, filesync_info, data=b'', size=0):
        """Send/buffer FileSync packets.

        Packets are buffered and only flushed when this connection is read from. All
        messages have a response from the device, so this will always get flushed.

        Parameters
        ----------
        command_id : bytes
            Command to send.
        adb_info : _AdbTransactionInfo
            Info and settings for this ADB transaction
        filesync_info : _FileSyncTransactionInfo
            Data and storage for this FileSync transaction
        data : str, bytes
            Optional data to send, must set data or size.
        size : int
            Optionally override size from len(data).

        """
        if data:
            if not isinstance(data, bytes):
                data = data.encode('utf8')
            size = len(data)

        if not filesync_info.can_add_to_send_buffer(len(data)):
            await self._filesync_flush(adb_info, filesync_info)

        buf = struct.pack(b'<2I', constants.FILESYNC_ID_TO_WIRE[command_id], size) + data
        filesync_info.send_buffer[filesync_info.send_idx:filesync_info.send_idx + len(buf)] = buf
        filesync_info.send_idx += len(buf)

    @staticmethod
    def _handle_progress(progress_callback):
        """Calls the callback with the current progress and total bytes written/received.

        Parameters
        ----------
        progress_callback : function
            Callback method that accepts ``filename``, ``bytes_written``, and ``total_bytes``; total_bytes will be -1 for file-like
            objects.

        """
        current = 0
        while True:
            current += yield
            try:
                progress_callback(current)
            except Exception:  # pylint: disable=broad-except
                continue


class AdbDeviceTcpAsync(AdbDeviceAsync):
    """A class with methods for connecting to a device via TCP and executing ADB commands.

    Parameters
    ----------
    host : str
        The address of the device; may be an IP address or a host name
    port : int
        The device port to which we are connecting (default is 5555)
    default_timeout_s : float, None
        Default timeout in seconds for TCP packets, or ``None``
    banner : str, bytes, None
        The hostname of the machine where the Python interpreter is currently running; if
        it is not provided, it will be determined via ``socket.gethostname()``

    Attributes
    ----------
    _available : bool
        Whether an ADB connection to the device has been established
    _banner : bytearray, bytes
        The hostname of the machine where the Python interpreter is currently running
    _handle : TcpHandleAsync
        The handle that is used to connect to the device

    """

    def __init__(self, host, port=5555, default_timeout_s=None, banner=None):
        handle = TcpHandleAsync(host, port, default_timeout_s)
        super(AdbDeviceTcpAsync, self).__init__(handle, banner)
