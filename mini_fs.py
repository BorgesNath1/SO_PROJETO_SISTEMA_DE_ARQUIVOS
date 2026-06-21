#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Mini Sistema de Arquivos com FUSE (baseado em i-nodes e blocos)
Montagem: python3 mini_fs.py /ponto/de/montagem
Desmontagem: fusermount -u /ponto/de/montagem
"""

import os
import sys
import time
import stat
import errno
import logging
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn

BLOCK_SIZE = 4096          # bytes por bloco
NUM_BLOCKS = 1024          # total de blocos no disco virtual
MAX_FILE_SIZE = 16 * BLOCK_SIZE  # 64 KB por arquivo

class Block:
    __slots__ = ('id', 'data', 'is_free', 'used_by')
    def __init__(self, block_id):
        self.id = block_id
        self.data = bytearray(BLOCK_SIZE)
        self.is_free = True
        self.used_by = None    # caminho do inode que usa este bloco

    def write(self, data_bytes):
        if len(data_bytes) > BLOCK_SIZE:
            data_bytes = data_bytes[:BLOCK_SIZE]
        self.data[:len(data_bytes)] = data_bytes
        self.is_free = False

    def read(self):
        return bytes(self.data).rstrip(b'\x00')

    def clear(self):
        self.data = bytearray(BLOCK_SIZE)
        self.is_free = True
        self.used_by = None

class INode:
    __slots__ = ('ino', 'mode', 'nlink', 'uid', 'gid', 'size', 'atime', 'mtime', 'ctime',
                 'block_pointers', 'is_dir', 'children', 'parent')
    def __init__(self, ino, mode, uid=0, gid=0):
        self.ino = ino
        self.mode = mode
        self.nlink = 1
        self.uid = uid
        self.gid = gid
        self.size = 0
        now = time.time()
        self.atime = now
        self.mtime = now
        self.ctime = now
        self.block_pointers = []
        self.is_dir = stat.S_ISDIR(mode)
        self.children = {} if self.is_dir else None
        self.parent = None

    def get_size(self):
        return self.size

    def update_times(self, access=True, modify=True):
        now = time.time()
        if access:
            self.atime = now
        if modify:
            self.mtime = now
        self.ctime = now

class MiniFS(LoggingMixIn, Operations):
    def __init__(self, root=None):
        self.root = root
        self.blocks = [Block(i) for i in range(NUM_BLOCKS)]
        self.inodes = {}
        self.next_ino = 1
        self.fd_counter = 0
        root_mode = stat.S_IFDIR | 0o755
        root_inode = INode(self.next_ino, root_mode)
        self.inodes[self.next_ino] = root_inode
        self.next_ino += 1

    def _alloc_blocks(self, data_bytes):
        """Aloca blocos para armazenar dados, retorna lista de IDs e bytes escritos."""
        num_needed = (len(data_bytes) + BLOCK_SIZE - 1) // BLOCK_SIZE
        if num_needed > (MAX_FILE_SIZE // BLOCK_SIZE):
            raise FuseOSError(errno.ENOSPC)

        free_blocks = [b for b in self.blocks if b.is_free]
        if len(free_blocks) < num_needed:
            raise FuseOSError(errno.ENOSPC)

        allocated_ids = []
        for i, block in enumerate(free_blocks[:num_needed]):
            start = i * BLOCK_SIZE
            end = start + BLOCK_SIZE
            block.write(data_bytes[start:end])
            block.used_by = self.next_ino
            allocated_ids.append(block.id)
        return allocated_ids

    def _free_blocks(self, block_ids):
        for bid in block_ids:
            self.blocks[bid].clear()

    def _get_inode(self, ino):
        if ino not in self.inodes:
            raise FuseOSError(errno.ENOENT)
        return self.inodes[ino]

    def _get_path_ino(self, path):
        if path == '/':
            return 1
        parts = path.strip('/').split('/')
        ino = 1
        for part in parts:
            inode = self._get_inode(ino)
            if not inode.is_dir:
                raise FuseOSError(errno.ENOTDIR)
            if part not in inode.children:
                raise FuseOSError(errno.ENOENT)
            ino = inode.children[part]
        return ino

    def _get_parent_ino(self, path):
        if path == '/':
            return (1, '')
        parts = path.strip('/').split('/')
        parent_path = '/' + '/'.join(parts[:-1])
        parent_ino = self._get_path_ino(parent_path)
        return parent_ino, parts[-1]

    def getattr(self, path, fh=None):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        inode.atime = time.time()
        return {
            'st_ino': ino,
            'st_mode': inode.mode,
            'st_nlink': inode.nlink,
            'st_uid': inode.uid,
            'st_gid': inode.gid,
            'st_size': inode.size,
            'st_atime': inode.atime,
            'st_mtime': inode.mtime,
            'st_ctime': inode.ctime,
            'st_blocks': len(inode.block_pointers),
            'st_blksize': BLOCK_SIZE,
        }

    def readdir(self, path, fh):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        if not inode.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        entries = ['.', '..']
        for name in sorted(inode.children.keys()):
            entries.append(name)
        return entries

    def open(self, path, flags):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        if inode.is_dir:
            raise FuseOSError(errno.EISDIR)
        self.fd_counter += 1
        return self.fd_counter

    def read(self, path, size, offset, fh):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        if inode.is_dir:
            raise FuseOSError(errno.EISDIR)
        data = bytearray()
        for bid in inode.block_pointers:
            data.extend(self.blocks[bid].read())
        data = data[:inode.size]
        if offset >= len(data):
            return b''
        return bytes(data[offset:offset+size])

    def write(self, path, data, offset, fh):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        if inode.is_dir:
            raise FuseOSError(errno.EISDIR)
        current_data = bytearray()
        for bid in inode.block_pointers:
            current_data.extend(self.blocks[bid].read())
        current_data = current_data[:inode.size]
        if offset > len(current_data):
            current_data.extend(b'\x00' * (offset - len(current_data)))
        if offset + len(data) > len(current_data):
            current_data.extend(b'\x00' * (offset + len(data) - len(current_data)))
        current_data[offset:offset+len(data)] = data
        self._free_blocks(inode.block_pointers)
        inode.block_pointers.clear()
        new_data = bytes(current_data)
        block_ids = self._alloc_blocks(new_data)
        for bid in block_ids:
            self.blocks[bid].used_by = ino

        inode.block_pointers = block_ids
        inode.size = len(new_data)
        inode.update_times(modify=True)
        return len(data)

    def create(self, path, mode, fi=None):
        try:
            self._get_path_ino(path)
            raise FuseOSError(errno.EEXIST)
        except FuseOSError as e:
            if e.errno != errno.ENOENT:
                raise
        parent_ino, name = self._get_parent_ino(path)
        parent = self._get_inode(parent_ino)
        if not parent.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        file_mode = stat.S_IFREG | (mode & 0o777)
        ino = self.next_ino
        self.next_ino += 1
        inode = INode(ino, file_mode, uid=os.getuid(), gid=os.getgid())
        self.inodes[ino] = inode
        parent.children[name] = ino
        inode.parent = parent_ino
        self.fd_counter += 1
        return self.fd_counter

    def mkdir(self, path, mode):
        parent_ino, name = self._get_parent_ino(path)
        parent = self._get_inode(parent_ino)
        if not parent.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        if name in parent.children:
            raise FuseOSError(errno.EEXIST)
        dir_mode = stat.S_IFDIR | (mode & 0o777)
        ino = self.next_ino
        self.next_ino += 1
        inode = INode(ino, dir_mode, uid=os.getuid(), gid=os.getgid())
        self.inodes[ino] = inode
        parent.children[name] = ino
        inode.parent = parent_ino
        return 0

    def unlink(self, path):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        if inode.is_dir:
            raise FuseOSError(errno.EISDIR)
        parent_ino, name = self._get_parent_ino(path)
        parent = self._get_inode(parent_ino)
        if name in parent.children:
            del parent.children[name]
        else:
            raise FuseOSError(errno.ENOENT)
        self._free_blocks(inode.block_pointers)
        del self.inodes[ino]
        return 0

    def rmdir(self, path):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        if not inode.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        if inode.children:
            raise FuseOSError(errno.ENOTEMPTY)
        parent_ino, name = self._get_parent_ino(path)
        parent = self._get_inode(parent_ino)
        if name in parent.children:
            del parent.children[name]
        else:
            raise FuseOSError(errno.ENOENT)
        del self.inodes[ino]
        return 0

    def rename(self, old, new):
        old_ino = self._get_path_ino(old)
        old_inode = self._get_inode(old_ino)
        try:
            new_ino = self._get_path_ino(new)
            new_inode = self._get_inode(new_ino)
            if new_inode.is_dir and new_inode.children:
                raise FuseOSError(errno.ENOTEMPTY)
            if new_inode.is_dir:
                self.rmdir(new)
            else:
                self.unlink(new)
        except FuseOSError as e:
            if e.errno != errno.ENOENT:
                raise
        old_parent_ino, old_name = self._get_parent_ino(old)
        old_parent = self._get_inode(old_parent_ino)
        if old_name in old_parent.children:
            del old_parent.children[old_name]
        else:
            raise FuseOSError(errno.ENOENT)
        new_parent_ino, new_name = self._get_parent_ino(new)
        new_parent = self._get_inode(new_parent_ino)
        if not new_parent.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        new_parent.children[new_name] = old_ino
        old_inode.parent = new_parent_ino
        old_inode.update_times(modify=True)
        return 0

    def chmod(self, path, mode):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        inode.mode = (inode.mode & stat.S_IFMT) | (mode & 0o777)
        inode.ctime = time.time()
        return 0

    def chown(self, path, uid, gid):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        if uid != -1:
            inode.uid = uid
        if gid != -1:
            inode.gid = gid
        inode.ctime = time.time()
        return 0

    def utimens(self, path, times=None):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        now = time.time()
        atime = times[0] if times and times[0] is not None else now
        mtime = times[1] if times and times[1] is not None else now
        inode.atime = atime
        inode.mtime = mtime
        inode.ctime = now
        return 0

    def truncate(self, path, length):
        ino = self._get_path_ino(path)
        inode = self._get_inode(ino)
        if inode.is_dir:
            raise FuseOSError(errno.EISDIR)
        current_data = bytearray()
        for bid in inode.block_pointers:
            current_data.extend(self.blocks[bid].read())
        current_data = current_data[:inode.size]
        if length < len(current_data):
            new_data = current_data[:length]
        else:
            new_data = current_data + b'\x00' * (length - len(current_data))
        self._free_blocks(inode.block_pointers)
        inode.block_pointers.clear()
        block_ids = self._alloc_blocks(bytes(new_data))
        for bid in block_ids:
            self.blocks[bid].used_by = ino
        inode.block_pointers = block_ids
        inode.size = length
        inode.update_times(modify=True)
        return 0

def main():
    if len(sys.argv) != 2:
        print('Uso: {} <ponto_de_montagem>'.format(sys.argv[0]))
        sys.exit(1)
    mountpoint = sys.argv[1]
    if not os.path.exists(mountpoint):
        print('Erro: diretório de montagem não existe.')
        sys.exit(1)
    logging.basicConfig(level=logging.DEBUG)
    fuse = FUSE(MiniFS(), mountpoint, foreground=True, nothreads=True, allow_other=False)

if __name__ == '__main__':
    main()