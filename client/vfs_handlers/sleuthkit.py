#!/usr/bin/env python

# Copyright 2010 Google Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implement low level disk access using the sleuthkit."""



import stat

import pytsk3

from grr.client import client_utils
from grr.client import vfs
from grr.lib import utils
from grr.proto import jobs_pb2


# Cache the filesystems as parsed by TSK for a limited time.
DEVICE_CACHE = utils.TimeBasedCache()


class CachedFilesystem(object):
  """A container for the filesystem and image."""

  def __init__(self, fs, img):
    self.fs = fs
    self.img = img


class MyImgInfo(pytsk3.Img_Info):
  """An Img_Info class using the regular python file handling."""

  def __init__(self, fd=None):
    pytsk3.Img_Info.__init__(self)
    self.fd = fd

  def read(self, offset, length):
    self.fd.seek(offset)
    return self.fd.read(length)

  def get_size(self):
    # Windows is unable to report the true size of the raw device and allows
    # arbitrary reading past the end - so we lie here to force tsk to read it
    # anyway
    return long(1e12)


class TSKFile(vfs.VFSHandler):
  """Read a regular file."""

  supported_pathtype = jobs_pb2.Path.TSK
  auto_register = True

  # A mapping to encode TSK types to a stat.st_mode
  FILE_TYPE_LOOKUP = {
      pytsk3.TSK_FS_NAME_TYPE_UNDEF: 0,
      pytsk3.TSK_FS_NAME_TYPE_FIFO: stat.S_IFIFO,
      pytsk3.TSK_FS_NAME_TYPE_CHR: stat.S_IFCHR,
      pytsk3.TSK_FS_NAME_TYPE_DIR: stat.S_IFDIR,
      pytsk3.TSK_FS_NAME_TYPE_BLK: stat.S_IFBLK,
      pytsk3.TSK_FS_NAME_TYPE_REG: stat.S_IFREG,
      pytsk3.TSK_FS_NAME_TYPE_LNK: stat.S_IFLNK,
      pytsk3.TSK_FS_NAME_TYPE_SOCK: stat.S_IFSOCK,
      }

  META_TYPE_LOOKUP = {
      pytsk3.TSK_FS_META_TYPE_BLK: 0,
      pytsk3.TSK_FS_META_TYPE_CHR: stat.S_IFCHR,
      pytsk3.TSK_FS_META_TYPE_DIR: stat.S_IFDIR,
      pytsk3.TSK_FS_META_TYPE_FIFO: stat.S_IFIFO,
      pytsk3.TSK_FS_META_TYPE_LNK: stat.S_IFLNK,
      pytsk3.TSK_FS_META_TYPE_REG: stat.S_IFREG,
      pytsk3.TSK_FS_META_TYPE_SOCK: stat.S_IFSOCK,
      }

  # Files we won't return in directories.
  BLACKLIST_FILES = ["$OrphanFiles"  # Special TSK dir that invokes processing.
                    ]

  # The file like object we read our image from
  tsk_raw_device = None

  def __init__(self, base_fd, pathspec=None):
    """Use TSK to read the pathspec.

    Args:
      base_fd: The file like object we read this component from.
      pathspec: An optional pathspec to open directly.
    Raises:
      IOError: If the file can not be opened.
    """
    super(TSKFile, self).__init__(base_fd, pathspec=pathspec)
    if self.base_fd is None:
      raise IOError("TSK driver must have a file base.")

    # If our base is another tsk driver - borrow the reference to the raw
    # device, and replace the last pathspec component with this one after
    # extending its path.
    elif isinstance(base_fd, TSKFile) and self.base_fd.IsDirectory():
      self.tsk_raw_device = self.base_fd.tsk_raw_device
      last_path = utils.JoinPath(self.pathspec.last.path, pathspec.path)

      # Replace the last component with this one.
      self.pathspec.Pop(-1)
      self.pathspec.Append(pathspec)
      self.pathspec.last.path = last_path

    # Use the base fd as a base to parse the filesystem only if its file like.
    elif not self.base_fd.IsDirectory():
      self.tsk_raw_device = self.base_fd
      self.pathspec.Append(pathspec)
    else:
      # If we get here we have a directory from a non sleuthkit driver - dont
      # know what to do with it.
      raise IOError("Unable to parse base using Sleuthkit.")

    # This is the path we try to open.
    self.tsk_path = self.pathspec.last.path

    # If we are successful in opening this path below the path casing is
    # correct.
    self.pathspec.last.path_options = jobs_pb2.Path.CASE_LITERAL

    fd_hash = self.tsk_raw_device.path
    if hasattr(self.tsk_raw_device, "image_offset"):
      fd_hash += ":" + str(self.tsk_raw_device.image_offset)

    # Cache the filesystem using the path of the raw device
    try:
      self.filesystem = DEVICE_CACHE.Get(fd_hash)
      self.fs = self.filesystem.fs
    except KeyError:
      self.img = MyImgInfo(fd=self.tsk_raw_device)

      offset = 0
      try:
        offset = self.tsk_raw_device.image_offset
      except AttributeError:
        pass

      self.fs = pytsk3.FS_Info(self.img, offset)
      self.filesystem = CachedFilesystem(self.fs, self.img)

      DEVICE_CACHE.Put(fd_hash, self.filesystem)

    # We prefer to open the file based on the inode because that is more
    # efficient.
    if pathspec.HasField("inode"):
      self.fd = self.fs.open_meta(pathspec.inode)
      attribute = self.GetAttribute(pathspec.ntfs_type, pathspec.ntfs_id)
      if attribute:
        self.size = attribute.info.size
      else:
        self.size = self.fd.info.meta.size

    else:
      # Does the filename exist in the image?
      self.fd = self.fs.open(utils.SmartStr(self.pathspec.last.path))
      self.size = self.fd.info.meta.size
      self.pathspec.last.inode = self.fd.info.meta.addr

  def GetAttribute(self, ntfs_type, ntfs_id):
    for attribute in self.fd:
      if attribute.info.type == ntfs_type and attribute.info.id == ntfs_id:
        return attribute

    return None

  def ListNames(self):
    directory_handle = self.fd.as_directory()
    for f in directory_handle:
      # TSK only deals with utf8 strings, but path components are always unicode
      # objects - so we convert to unicode as soon as we receive data from
      # TSK. Prefer to compare unicode objects to guarantee they are normalized.
      yield utils.SmartUnicode(f.info.name.name)

  def MakeStatResponse(self, tsk_file, tsk_attribute=None, append_name=False):
    """Given a TSK info object make a StatResponse."""
    info = tsk_file.info
    response = jobs_pb2.StatResponse()
    meta = info.meta
    if meta:
      response.st_ino = meta.addr
      for attribute in "mode nlink uid gid size atime mtime ctime".split():
        try:
          value = int(getattr(meta, attribute))
          if value < 0: value &= 0xFFFFFFFF

          setattr(response, "st_%s" % attribute, value)
        except AttributeError:
          pass

    name = info.name
    child_pathspec = self.pathspec.Copy()

    if append_name:
      # Append the name to the most inner pathspec
      child_pathspec.last.path = utils.JoinPath(child_pathspec.last.path,
                                                utils.SmartUnicode(append_name))

    child_pathspec.last.inode = meta.addr
    if tsk_attribute is not None:
      child_pathspec.last.ntfs_type = int(tsk_attribute.info.type)
      child_pathspec.last.ntfs_id = int(tsk_attribute.info.id)
      if tsk_attribute.info.name is not None:
        child_pathspec.last.path += ":" + tsk_attribute.info.name

    if name:
      # Encode the type onto the st_mode response
      response.st_mode |= self.FILE_TYPE_LOOKUP.get(int(name.type), 0)

    if meta:
      # What if the types are different? What to do here?
      response.st_mode |= self.META_TYPE_LOOKUP.get(int(meta.type), 0)

    # Write the pathspec on the response.
    child_pathspec.ToProto(response.pathspec)
    return response

  def Read(self, length):
    """Read from the file."""
    if not self.IsFile():
      raise IOError("%s is not a file." % self.pathspec.last.path)

    available = min(self.size - self.offset, length)
    if available > 0:
      # This raises a RuntimeError in some situations.
      try:
        data = self.fd.read_random(self.offset, available,
                                   self.pathspec.last.ntfs_type,
                                   self.pathspec.last.ntfs_id)
      except RuntimeError, e:
        raise IOError(e)

      self.offset += len(data)

      return data
    return ""

  def Stat(self):
    """Return a stat of the file."""
    return self.MakeStatResponse(self.fd, None)

  def ListFiles(self):
    """List all the files in the directory."""
    if self.IsDirectory():
      dir_fd = self.fd.as_directory()
      for f in dir_fd:
        try:
          name = f.info.name.name
          # Drop these useless entries.
          if name in [".", ".."] or name in self.BLACKLIST_FILES:
            continue

          # First we yield a standard response using the default attributes.
          yield self.MakeStatResponse(f, append_name=name)

          # Now send back additional named attributes for the ADS.
          for attribute in f:
            if attribute.info.type in [pytsk3.TSK_FS_ATTR_TYPE_NTFS_DATA,
                                       pytsk3.TSK_FS_ATTR_TYPE_DEFAULT]:
              if attribute.info.name:
                yield self.MakeStatResponse(f, attribute, append_name=name)
        except AttributeError:
          pass
    else:
      raise IOError("%s is not a directory" % self.fd.info.name.name)

  def IsDirectory(self):
    return self.fd.info.meta.type == pytsk3.TSK_FS_META_TYPE_DIR

  def IsFile(self):
    return self.fd.info.meta.type == pytsk3.TSK_FS_META_TYPE_REG

  @classmethod
  def Open(cls, fd, component, pathspec):
    # A Pathspec which starts with TSK means we need to resolve the mount point
    # at runtime.
    if fd is None and component.pathtype == jobs_pb2.Path.TSK:
      # We are the top level handler. This means we need to check the system
      # mounts to work out the exact mount point and device we need to
      # open. We then modify the pathspec so we get nested in the raw
      # pathspec.
      raw_pathspec, corrected_path = client_utils.GetRawDevice(component.path)

      # Insert the raw device before the component in the pathspec and correct
      # the path
      component.path = corrected_path
      pathspec.Insert(0, raw_pathspec, component)

      # Allow incoming pathspec to be given in the local system path
      # conventions.
      for component in pathspec:
        if component.path:
          component.path = client_utils.LocalPathToCanonicalPath(
              component.path)

      # We have not actually opened anything in this iteration, but modified the
      # pathspec. Next time we should be able to open it properly.
      return fd

    # If an inode is specified, just use it directly.
    elif component.inode:
      return TSKFile(fd, component)

    # Otherwise do the usual case folding.
    else:
      return vfs.VFSHandler.Open(fd, component, pathspec)