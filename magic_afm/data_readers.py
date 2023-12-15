"""Magic AFM Data Readers

This module abstracts the different file types that this package supports.
Generally, they have the structure of a BaseForceVolumeFile subclass that has-a
worker class. The File class opens file objects and parses metadata, while
the worker class does the actual reads from the disk. Generally, the File class
asyncifies the worker's disk reads with threads, although this is not a rule.
"""

# Copyright (C) Richard J. Sheridan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
import abc
import mmap
import struct
import threading
from collections.abc import Collection, Iterable
from functools import partial
from subprocess import PIPE
from typing import Protocol, TypeAlias

try:
    from subprocess import STARTF_USESHOWWINDOW, STARTUPINFO
except ImportError:
    STARTUPINFO = lambda *a, **kw: None
    STARTF_USESHOWWINDOW = None

import attrs
import numpy as np
import trio

from . import calculation
from .async_tools import trs


NANOMETER_UNIT_CONVERSION = (
    1e9  # maybe we can intelligently read this from the file someday
)
###############################################
############### Typing stuff ##################
###############################################
Index: TypeAlias = tuple[int, ...]
ZDArrays: TypeAlias = Collection[np.ndarray]
ChanMap: TypeAlias = dict[str, tuple[int, "ARDFVchan"]]


class FVReader(Protocol):
    def get_curve(self, r: int, c: int) -> ZDArrays:
        """Efficiently get a specific curve from disk."""
        ...

    def iter_curves(self) -> Iterable[tuple[Index, ZDArrays]]:
        """Iterate over curves lazily in on-disk order."""
        ...

    def get_all_curves(self) -> ZDArrays:
        """Eagerly load all curves into memory."""
        ...


@attrs.frozen
class ForceVolumeParams:
    k: float
    defl_sens: float
    sync_dist: int


###############################################
################## Helpers ####################
###############################################
CACHED_OPEN_PATHS = {}


def eventually_evict_path(path):
    path_lock = CACHED_OPEN_PATHS[path][-1]
    while path_lock.acquire(timeout=10.0):
        # someone reset our countdown
        pass
    # time's up, kick it out
    del CACHED_OPEN_PATHS[path]
    return


def open_h5(path):
    import h5py

    return h5py.File(path, "r")


def mmap_path_read_only(path):
    import mmap

    with open(path, mode="rb", buffering=0) as file:
        return mmap.mmap(file.fileno(), length=0, access=mmap.ACCESS_READ)


def decode_cstring(cstring: bytes):
    return cstring.rstrip(b"\0").decode("windows-1252")


# noinspection PyUnboundLocalVariable
def parse_nanoscope_header(header_lines):
    """Convert header from a Nanoscope file to a convenient nested dict

    header_lines can be an opened file object or a list of strings or anything
    that iterates the header line-by-line."""

    header = {}
    for line in header_lines:
        assert line.startswith("\\")
        line = line[1:].strip()  # strip leading slash and newline

        if line.startswith("*"):
            # we're starting a new section
            section_name = line[1:]
            if section_name == "File list end":
                break  # THIS IS THE **NORMAL** WAY TO END THE FOR LOOP
            if section_name in header:
                # repeat section name, which we interpret as a list of sections
                if header[section_name] is current_section:
                    header[section_name] = [current_section]
                current_section = {}
                header[section_name].append(current_section)
            else:
                current_section = {}
                header[section_name] = current_section
        else:
            # add key, value pairs for this section
            key, value = line.split(":", maxsplit=1)
            # Colon special case for "groups"
            if key.startswith("@") and key[1].isdigit() and len(key) == 2:
                key2, value = value.split(":", maxsplit=1)
                key = key + ":" + key2

            current_section[key] = value.strip()
    else:
        raise ValueError("File ended too soon")
    if (not header) or ("" in header):
        raise ValueError("File is empty or not a Bruker data file")

    # link headers from [section][index][key] to [Image/FV][Image Data name][key]
    header["Image"] = {}
    for entry in header["Ciao image list"]:
        if type(entry) is str:
            # a single image in this file, rather than a list of images
            name = header["Ciao image list"]["@2:Image Data"].split('"')[1]
            header["Image"][name] = header["Ciao image list"]
            break
        name = entry["@2:Image Data"].split('"')[1]
        # assert name not in header["Image"]  # assume data is redundant for now
        header["Image"][name] = entry
    header["FV"] = {}
    for entry in header["Ciao force image list"]:
        if type(entry) is str:
            # a single force image in this file, rather than a list of images
            name = header["Ciao force image list"]["@4:Image Data"].split('"')[1]
            header["FV"][name] = header["Ciao force image list"]
            break
        name = entry["@4:Image Data"].split('"')[1]
        # assert name not in header["FV"]  # assume data is redundant for now
        header["FV"][name] = entry

    return header


def bruker_bpp_fix(bpp, version):
    if (
        version > "0x09200000"
    ):  # Counting on lexical ordering here, hope zeros don't change...
        return 4
    else:
        return int(bpp)


def parse_AR_note(note: str):
    # The notes have a very regular key-value structure
    # convert to dict for later access
    return dict(
        line.split(":", 1)
        for line in note.split("\n")
        if ":" in line and "@Line:" not in line
    )


async def convert_ardf(
    ardf_path, *, h5file_path=None, conv_path="ARDFtoHDF5.exe", pbar=None
):
    """Turn an ARDF into a corresponding ARH5, returning the path.

    Requires converter executable available from Asylum Research"""
    ardf_path = trio.Path(ardf_path)
    if h5file_path is None:
        h5file_path = ardf_path.with_suffix(".h5")
    else:
        h5file_path = trio.Path(h5file_path)

    if pbar is None:
        # Just display the raw subprocess output
        pipe = None
    else:
        # set up pbar and pipes for custom display
        pbar.set_description_str("Converting " + ardf_path.name)
        pipe = PIPE

    async def reading_stdout():
        """Store up stdout in our own buffer to check for Failed at the end."""
        stdout = bytearray()
        async for bytes_ in proc.stdout:
            stdout.extend(bytes_)
        stdout = stdout.decode()
        if "Failed" in stdout:
            raise RuntimeError(stdout)
        else:
            print(stdout)

    async def reading_stderr():
        """Parse the percent complete display to send to our own progressbar"""
        async for bytes_ in proc.stderr:
            i = bytes_.rfind(b"\x08") + 1  # first thing on right not a backspace
            most_recent_numeric_output = bytes_[i:-1]  # crop % sign
            if most_recent_numeric_output:
                try:
                    n = round(float(most_recent_numeric_output.decode()), 1)
                except ValueError:
                    # I don't know what causes this, but I'd
                    # rather carry on than have a fatal error
                    pass
                else:
                    pbar.update(n - pbar.n)

    try:
        async with trio.open_nursery() as nursery:
            proc = await nursery.start(
                partial(
                    trio.run_process,
                    [conv_path, ardf_path, h5file_path],
                    stderr=pipe,
                    stdout=pipe,
                    # suppress a console on windows
                    startupinfo=STARTUPINFO(dwFlags=STARTF_USESHOWWINDOW),
                )
            )
            if pbar is not None:
                nursery.start_soon(reading_stdout)
                nursery.start_soon(reading_stderr)
    except FileNotFoundError as e:
        raise FileNotFoundError(
            "Please acquire ARDFtoHDF5.exe and "
            "place it in the application's root folder."
        ) from None
    except:
        with trio.CancelScope(shield=True):
            await h5file_path.unlink(missing_ok=True)
        raise

    return h5file_path


###############################################
################## FVFiles ####################
###############################################
class BaseForceVolumeFile(metaclass=abc.ABCMeta):
    """Consistent interface across filetypes for Magic AFM GUI

    I would not recommend re-using this or its subclasses for an external application.
    Prefer wrapping a worker class."""

    _basic_units_map = {}
    _default_heightmap_names = ()

    def __init__(self, path):
        self.scansize = None
        self.path = path
        self._units_map = self._basic_units_map.copy()
        self._image_cache = {}
        self._file_image_names = set()
        self._trace = None
        self._worker = None
        self.k = None
        self.defl_sens = None
        self.t_step = None
        self.sync_dist = 0

    @property
    def trace(self):
        return self._trace

    @property
    def image_names(self):
        return self._image_cache.keys() | self._file_image_names

    @property
    def initial_image_name(self):
        for name in self._file_image_names.intersection(self._default_heightmap_names):
            return name
        else:
            return None

    @property
    def parameters(self):
        return ForceVolumeParams(
            k=self.k,
            defl_sens=self.defl_sens,
            sync_dist=self.sync_dist,
        )

    @abc.abstractmethod
    async def ainitialize(self):
        raise NotImplementedError

    @abc.abstractmethod
    async def aclose(self):
        raise NotImplementedError

    async def __aenter__(self):
        await self.ainitialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    def get_image_units(self, image_name):
        image_name = self.strip_trace(image_name)
        return self._units_map.get(image_name, "V")

    def add_image(self, image_name, units, image):
        self._image_cache[image_name] = image
        image_name = self.strip_trace(image_name)
        self._units_map[image_name] = units

    @staticmethod
    def strip_trace(image_name):
        for suffix in ("trace", "Trace", "retrace", "Retrace"):
            image_name = image_name.removesuffix(suffix)
        return image_name

    async def get_force_curve(self, r, c):
        return await trs(self.get_force_curve_sync, r, c)

    @abc.abstractmethod
    def get_force_curve_sync(self, r, c):
        raise NotImplementedError

    async def get_image(self, image_name):
        if image_name in self._image_cache:
            await trio.sleep(0)
            image = self._image_cache[image_name]
        else:
            image = await trs(self.get_image_sync, image_name)
            self._image_cache[image_name] = image
        return image

    @abc.abstractmethod
    def get_image_sync(self, image_name):
        raise NotImplementedError


class DemoForceVolumeFile(BaseForceVolumeFile):
    def __init__(self, path):
        path = trio.Path(path)
        super().__init__(path)
        self._file_image_names.add("Demo")
        self._default_heightmap_names = ("Demo",)
        self.scansize = 100, 100
        self.k = 10
        self.defl_sens = 5
        self.t_step = 5e-6

    async def ainitialize(self):
        await trio.sleep(0)
        t = np.linspace(0, np.pi * 2, 1000, endpoint=False)
        self.delta = -15 * (np.cos(t) + 0.5)

    async def aclose(self):
        pass

    def get_force_curve_sync(self, r, c):
        gen = np.random.default_rng(seed=(r, c))
        parms = (1, 10, 0.1, -2, 1, 0, 0, 1)
        deltaext = self.delta[: self.delta.size // 2]
        deltaret = self.delta[self.delta.size // 2 :]
        fext = calculation.force_curve(calculation.red_extend, deltaext, *parms)
        fret = calculation.force_curve(calculation.red_retract, deltaret, *parms)
        dext = fext / self.k + gen.normal(scale=0.1, size=fext.size)
        dret = fret / self.k + gen.normal(scale=0.1, size=fret.size)
        zext = deltaext + dext + gen.normal(scale=0.01, size=fext.size)
        zret = deltaret + dret + gen.normal(scale=0.01, size=fret.size)
        return (zext, zret), (dext, dret)

    def get_image_sync(self, image_name):
        return np.zeros((64, 64), dtype=np.float32)


###############################################
################### Asylum ####################
###############################################


class ForceMapWorker:
    def __init__(self, h5data):
        self.force_curves = h5data["ForceMap"]["0"]
        # ForceMap Segments can contain 3 or 4 endpoint indices for each indent array
        self.segments = self.force_curves["Segments"][:, :, :]  # XXX Read h5data
        im_r, im_c, num_segments = self.segments.shape

        # Generally segments are [Ext, Dwell, Ret, Away] or [Ext, Ret, Away]
        # for magic, we don't dwell. new converter ensures this assertion
        assert num_segments == 3

        # this is all necessary because the arrays are not of uniform length
        # We will cut all arrays down to the length of the smallest
        self.extlens = self.segments[:, :, 0]
        self.minext = self.split = np.min(self.extlens)
        self.extretlens = self.segments[:, :, 1]
        self.minret = np.min(self.extretlens - self.extlens)
        self.npts = self.minext + self.minret

        # We only care about 2 channels, Defl and ZSnsr
        # Convert channels array to a map that can be used to index into ForceMap data by name
        # chanmap should always be {'Defl':1,'ZSnsr':2} but it's cheap to calculate
        chanmap = {
            key: index for index, key in enumerate(self.force_curves.attrs["Channels"])
        }
        # We could slice with "1:" if chanmap were constant but I'm not sure if it is
        self.defl_zsnsr_row_slice = [chanmap["Defl"], chanmap["ZSnsr"]]

    def _shared_get_part(self, curve, s):
        # Index into the data and grab the Defl and Zsnsr ext and ret arrays as one 2D array
        defl_zsnsr = curve[self.defl_zsnsr_row_slice, :]  # XXX Read h5data

        # we are happy to throw away data far from the surface to square up the data
        # Also reverse axis zero so data is ordered zsnsr,defl like we did for FFM
        return (
            defl_zsnsr[::-1, (s - self.minext) : (s + self.minret)]
            * NANOMETER_UNIT_CONVERSION
        )

    def get_force_curve(self, r, c):
        # Because of the nonuniform arrays, each indent gets its own dataset
        # indexed by 'row:column' e.g. '1:1'.
        curve = self.force_curves[f"{r}:{c}"]  # XXX Read h5data
        split = self.extlens[r, c]

        z, d = self._shared_get_part(curve, split)
        split = self.minext
        return (z[:split], z[split:]), (d[:split], d[split:])

    def get_all_curves(self, cancel_poller=bool):
        im_r, im_c, num_segments = self.segments.shape
        x = np.empty((im_r, im_c, 2, self.minext + self.minret), dtype=np.float32)
        for index, curve in self.force_curves.items():
            # Unfortunately they threw in segments here too, so we skip over it
            if index == "Segments":
                continue
            cancel_poller()
            # Because of the nonuniform arrays, each indent gets its own dataset
            # indexed by 'row:column' e.g. '1:1'. We could start with the shape and index
            # manually, but the string munging is easier for me to think about
            r, c = index.split(":")
            r, c = int(r), int(c)
            split = self.extlens[r, c]

            x[r, c, :, :] = self._shared_get_part(curve, split)
        return x.reshape(x.shape[:-1] + (2, -1))


class FFMSingleWorker:
    def __init__(self, raw, defl):
        self.raw = raw
        self.defl = defl
        self.npts = raw.shape[-1]
        self.split = self.npts // 2

    def get_force_curve(self, r, c):
        z = self.raw[r, c].reshape((2, -1))
        d = self.defl[r, c].reshape((2, -1))
        return z * NANOMETER_UNIT_CONVERSION, d * NANOMETER_UNIT_CONVERSION

    def get_all_curves(self, cancel_poller=bool):
        cancel_poller()
        z = self.raw[:] * NANOMETER_UNIT_CONVERSION
        cancel_poller()
        d = self.defl[:] * NANOMETER_UNIT_CONVERSION
        cancel_poller()
        return z, d


class FFMTraceRetraceWorker:
    def __init__(self, raw_trace, defl_trace, raw_retrace, defl_retrace):
        self.raw_trace = raw_trace
        self.defl_trace = defl_trace
        self.raw_retrace = raw_retrace
        self.defl_retrace = defl_retrace
        self.trace = True
        self.npts = raw_trace.shape[-1]
        self.split = self.npts // 2

    def get_force_curve(self, r, c):
        if self.trace:
            z = self.raw_trace[r, c].reshape((2, -1))
            d = self.defl_trace[r, c].reshape((2, -1))
        else:
            z = self.raw_retrace[r, c].reshape((2, -1))
            d = self.defl_retrace[r, c].reshape((2, -1))
        return z * NANOMETER_UNIT_CONVERSION, d * NANOMETER_UNIT_CONVERSION

    def get_all_curves(self, cancel_poller=bool):
        cancel_poller()
        if self.trace:
            z = self.raw_trace[:] * NANOMETER_UNIT_CONVERSION
            cancel_poller()
            d = self.defl_trace[:] * NANOMETER_UNIT_CONVERSION
        else:
            z = self.raw_retrace[:] * NANOMETER_UNIT_CONVERSION
            cancel_poller()
            d = self.defl_retrace[:] * NANOMETER_UNIT_CONVERSION
        cancel_poller()
        return z, d


class ARH5File(BaseForceVolumeFile):
    _basic_units_map = {
        "Adhesion": "N",
        "Height": "m",
        "IndentationHertz": "m",
        "YoungsHertz": "Pa",
        "YoungsJKR": "Pa",
        "YoungsDMT": "Pa",
        "ZSensor": "m",
        "MapAdhesion": "N",
        "MapHeight": "m",
        "Force": "N",
    }
    _default_heightmap_names = ("MapHeight", "ZSensorTrace", "ZSensorRetrace")

    @BaseForceVolumeFile.trace.setter
    def trace(self, trace):
        self._worker.trace = trace
        self._trace = trace

    def __getstate__(self):
        state = self.__dict__.copy()
        del state["_h5data"]
        del state["_worker"]
        del state["_images"]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        try:
            h5data, images, worker, path_lock = CACHED_OPEN_PATHS[self.path]
        except KeyError:
            h5data = open_h5(self.path)
            images = h5data["Image"]
            worker = self._choose_worker(h5data)
            path_lock = threading.Lock()
            path_lock.acquire()
            CACHED_OPEN_PATHS[self.path] = h5data, images, worker, path_lock
            threading.Thread(
                target=eventually_evict_path, args=(self.path,), daemon=True
            ).start()
        else:
            # reset thread countdown
            try:
                path_lock.release()
            except RuntimeError:
                pass  # no problem if unreleased
        self._h5data = h5data
        self._images = images
        self._worker = worker

    async def ainitialize(self):
        self._h5data = h5data = await trio.to_thread.run_sync(open_h5, self.path)
        self.notes = await trs(lambda: parse_AR_note(h5data.attrs["Note"]))
        worker = await trs(self._choose_worker, h5data)
        images, image_names = await trs(
            lambda: (h5data["Image"], set(h5data["Image"].keys()))
        )
        self._worker = worker
        self._images = images
        self._file_image_names.update(image_names)

        self.k = float(self.notes["SpringConstant"])
        self.scansize = (
            float(self.notes["FastScanSize"]) * NANOMETER_UNIT_CONVERSION,
            float(self.notes["SlowScanSize"]) * NANOMETER_UNIT_CONVERSION,
        )
        # NOTE: aspect is redundant to scansize
        # self.aspect = float(self.notes["SlowRatio"]) / float(self.notes["FastRatio"])
        self.defl_sens = self._defl_sens_orig = (
            float(self.notes["InvOLS"]) * NANOMETER_UNIT_CONVERSION
        )
        self.rate = float(self.notes["FastMapZRate"])
        self.npts, self.split = worker.npts, worker.split
        self.t_step = 1 / self.rate / self.npts

    async def aclose(self):
        with trio.CancelScope(shield=True):
            await trio.to_thread.run_sync(self._h5data.close)

    def _choose_worker(self, h5data):
        if "FFM" in h5data:
            # self.scandown = bool(self.notes["ScanDown"])
            if "1" in h5data["FFM"]:
                worker = FFMTraceRetraceWorker(
                    h5data["FFM"]["0"]["Raw"],
                    h5data["FFM"]["0"]["Defl"],
                    h5data["FFM"]["1"]["Raw"],
                    h5data["FFM"]["1"]["Defl"],
                )
                self._trace = True
            elif "0" in h5data["FFM"]:
                worker = FFMSingleWorker(
                    h5data["FFM"]["0"]["Raw"], h5data["FFM"]["0"]["Defl"]
                )
            else:
                worker = FFMSingleWorker(h5data["FFM"]["Raw"], h5data["FFM"]["Defl"])
        else:
            # self.scandown = bool(self.notes["FMapScanDown"])
            worker = ForceMapWorker(h5data)
        return worker

    def get_force_curve_sync(self, r, c):
        out = zxr, dxr = self._worker.get_force_curve(r, c)
        if self.defl_sens != self._defl_sens_orig:
            dxr *= self.defl_sens / self._defl_sens_orig
        return out

    def get_image_sync(self, image_name):
        return self._images[image_name][:]


@attrs.frozen
class ARDFHeader:
    data: mmap
    offset: int
    crc: int = attrs.field(repr=hex)
    size: int
    name: bytes
    flags: int = attrs.field(repr=hex)

    @classmethod
    def unpack(cls, data: mmap, offset: int):
        return cls(data, offset, *struct.unpack_from("<LL4sL", data, offset))

    def validate(self):
        # ImHex poly 0x4c11db7 init 0xffffffff xor out 0xffffffff reflect in and out
        import zlib

        crc = zlib.crc32(
            memoryview(self.data)[self.offset + 4 : self.offset + self.size]
        )
        if self.crc != crc:
            raise ValueError(
                f"Invalid section. Expected {self.crc:X}, got {crc:X}.", self
            )
        return True


@attrs.frozen
class ARDFTableOfContents:
    data: mmap
    offset: int
    size: int
    entries: list[tuple[ARDFHeader, int]]

    @classmethod
    def unpack(cls, header: ARDFHeader):
        if header.size != 32:
            raise ValueError("Malformed table of contents", header)
        header.validate()
        data = header.data
        offset = header.offset
        size, nentries, stride = struct.unpack_from("<QLL", data, offset + 16)
        assert stride == 24
        assert size - 32 == nentries * stride, (size, nentries, stride)
        entries = []
        for toc_offset in range(offset + 32, offset + size, stride):
            *header, pointer = struct.unpack_from("<LL4sLQ", data, toc_offset)
            if not pointer:
                break  # rest is null padding
            entry_header = ARDFHeader(data, toc_offset, *header)
            if entry_header.name not in {b"IMAG", b"VOLM", b"NEXT", b"THMB", b"NSET"}:
                raise ValueError("Malformed table of contents.", entry_header)
            entry_header.validate()
            entries.append((entry_header, pointer))
        return cls(data, offset, size, entries)


@attrs.frozen
class ARDFTextTableOfContents:
    data: mmap
    offset: int
    size: int
    entries: list[tuple[ARDFHeader, int]]

    @classmethod
    def unpack(cls, header: ARDFHeader):
        if header.size != 32 or header.name != b"TTOC":
            raise ValueError("Malformed text table of contents.", header)
        header.validate()
        offset = header.offset
        data = header.data
        size, nentries, stride = struct.unpack_from("<QLL", data, offset + 16)
        assert stride == 32, stride
        assert size - 32 == nentries * stride, (size, nentries, stride)
        entries = []
        for toc_offset in range(offset + 32, offset + size, stride):
            *header, _, pointer = struct.unpack_from("<LL4sLQQ", data, toc_offset)
            if not pointer:
                break  # rest is null padding
            entry_header = ARDFHeader(data, toc_offset, *header)
            if entry_header.name != b"TOFF":
                raise ValueError("Malformed text table entry.", entry_header)
            entry_header.validate()
            entries.append((entry_header, pointer))
        return cls(data, offset, size, entries)

    def decode_entry(self, index: int):
        entry_header, pointer = self.entries[index]
        *header, i, text_len = struct.unpack_from("<LL4sLLL", self.data, pointer)
        text_header = ARDFHeader(self.data, pointer, *header)
        if text_header.name != b"TEXT":
            raise ValueError("Malformed text section.", text_header)
        text_header.validate()
        assert i == index, (i, index)
        offset = text_header.offset + 24
        assert text_len < text_header.size - 24, (text_len, text_header)
        self.data.seek(offset)
        text = self.data.read(text_len)
        # self.data.seek(0)  # seems unneeded currently
        return text.replace(b"\r", b"\n").decode("windows-1252")


@attrs.frozen
class ARDFVolumeTableOfContents:
    offset: int
    size: int
    lines: np.ndarray
    points: np.ndarray
    pointers: np.ndarray

    @classmethod
    def unpack(cls, header: ARDFHeader):
        # cant reuse exact ARDFTableOfContents for VTOC, but structure is similar
        data = header.data
        offset = header.offset
        if header.size != 32 or header.name != b"VTOC":
            raise ValueError("Malformed volume table of contents.", header)
        size, nentries, stride = struct.unpack_from("<QLL", data, offset + 16)
        assert stride == 40, stride
        assert size - 32 == nentries * stride, (size, nentries, stride)
        vtoc_arr = np.zeros(
            nentries,
            dtype=[
                ("force_index", "L"),
                ("line", "L"),
                ("point", "Q"),
                ("pointer", "Q"),
            ],
        )
        for i, toc_offset in enumerate(
            range(header.offset + 32, header.offset + size, stride)
        ):
            entry_header = ARDFHeader.unpack(data, toc_offset)
            if not entry_header.crc:
                # scan was interrupted, and vtoc is zero-filled
                vtoc_arr = vtoc_arr[:i].copy()
                break
            if entry_header.name != b"VOFF":
                raise ValueError("Malformed volume table entry.", entry_header)
            entry_header.validate()
            vtoc_arr[i] = struct.unpack_from("<LLQQ", data, toc_offset + 16)
        return cls(
            offset, size, vtoc_arr["line"], vtoc_arr["point"], vtoc_arr["pointer"]
        )


@attrs.frozen
class ARDFVchan:
    name: str
    unit: str

    @classmethod
    def unpack(cls, header: ARDFHeader):
        if header.size != 80 or header.name != b"VCHN":
            raise ValueError("Malformed channel definition.", header)
        header.validate()
        format = "<32s32s"
        name, unit = struct.unpack_from(format, header.data, header.offset + 16)
        return cls(decode_cstring(name), decode_cstring(unit))


@attrs.frozen
class ARDFXdef:
    offset: int
    size: int
    xdef: list[str]

    @classmethod
    def unpack(cls, header: ARDFHeader):
        if header.size != 96 or header.name != b"XDEF":
            raise ValueError("Malformed experiment definition.", header)
        header.validate()
        # Experiment definition is just a string
        _, nchars = struct.unpack_from("<LL", header.data, header.offset + 16)
        assert _ == 0, _
        if nchars > header.size:
            raise ValueError("Experiment definition too long.", header, nchars)
        header.data.seek(header.offset + 24)
        xdef = header.data.read(nchars).decode("windows-1252").split(";")[:-1]
        # data.seek(0)  # seems unneeded currently
        return cls(header.offset, header.size, xdef)


@attrs.frozen
class ARDFVset:
    data: mmap
    offset: int
    size: int
    force_index: int
    line: int
    point: int
    # vtype seems to differ between different FV modes.
    # FMAP with ext;ret;dwell shows 0b10 = 2 everywhere.
    # FFM with just trace or just retrace shows 0b101 = 5 everywhere.
    # FFM storing both shows 0b1010 = 10 for trace
    # and 0b1011 = 11 for retrace.
    vtype: int = attrs.field(repr=bin)
    prev_vset_offset: int
    next_vset_offset: int

    @classmethod
    def unpack(cls, vset_header: ARDFHeader):
        if vset_header.name != b"VSET":
            raise ValueError("malformed VSET header", vset_header)
        vset_header.validate()
        data = vset_header.data
        offset = vset_header.offset
        vset_format = "<LLLLQQ"
        size = 48  # 16 + struct.calcsize(vset_format)  # constant
        return cls(
            data, offset, size, *struct.unpack_from(vset_format, data, offset + 16)
        )


@attrs.frozen
class ARDFVdata:
    data: mmap
    offset: int
    size: int
    force_index: int
    line: int
    point: int
    nfloats: int
    channel: int
    seg_offsets: tuple[int, ...]

    @classmethod
    def unpack(cls, header: ARDFHeader):
        data = header.data
        offset = header.offset
        vdat_format = "<10L"
        size = 56  # struct.calcsize(vdat_format) + 16  # constant
        (force_index, line, point, nfloats, channel, *seg_offsets) = struct.unpack_from(
            vdat_format, data, offset + 16
        )
        return cls(
            data, offset, size, force_index, line, point, nfloats, channel, seg_offsets
        )

    @property
    def array_offset(self):
        return self.offset + self.size

    @property
    def next_offset(self):
        return self.array_offset + self.nfloats * 4

    def get_ndarray(self):
        return (
            np.ndarray(
                shape=self.nfloats,
                dtype=np.float32,
                buffer=self.data,
                offset=self.array_offset,
            )
            * NANOMETER_UNIT_CONVERSION
        )


@attrs.frozen
class ARDFImage:
    data: mmap
    ibox_offset: int
    name: str
    units: str
    x_step: float
    y_step: float
    x_units: str
    y_units: str

    @classmethod
    def parse_imag(cls, imag_header: ARDFHeader):
        if imag_header.name != b"IMAG":
            raise ValueError("Malformed image header.", imag_header)
        imag_toc = ARDFTableOfContents.unpack(imag_header)

        # don't use NEXT or THMB data, step over
        ttoc_header = ARDFHeader.unpack(imag_toc.data, imag_toc.offset + imag_toc.size)
        ttoc = ARDFTextTableOfContents.unpack(ttoc_header)

        # don't use TTOC or TOFF, step over
        idef_header = ARDFHeader.unpack(ttoc.data, ttoc.offset + ttoc.size)
        idef_format = "<LLQQdd32s32s32s32s"
        if (
            idef_header.name != b"IDEF"
            or idef_header.size != struct.calcsize(idef_format) + 16
        ):
            raise ValueError("Malformed image definition.", idef_header)
        idef_header.validate()
        points, lines, _, __, x_step, y_step, *cstrings = struct.unpack_from(
            idef_format,
            idef_header.data,
            idef_header.offset + 16,
        )
        assert not (_ or __), (_, __)
        x_units, y_units, name, units = list(map(decode_cstring, cstrings))

        return cls(
            imag_header.data,
            idef_header.offset + idef_header.size,
            name,
            units,
            x_step,
            y_step,
            x_units,
            y_units,
        )

    def get_ndarray(self):
        ibox_header = ARDFHeader.unpack(self.data, self.ibox_offset)
        if ibox_header.size != 32 or ibox_header.name != b"IBOX":
            raise ValueError("Malformed image layout.", ibox_header)
        ibox_header.validate()
        data_offset = ibox_header.offset + ibox_header.size + 16  # past IDAT header
        ibox_size, lines, stride = struct.unpack_from(
            "<QLL", ibox_header.data, ibox_header.offset + 16
        )  # TODO: invert lines? it's just a negative sign on stride
        points = (stride - 16) // 4  # less IDAT header
        # elide image data validation and map into an array directly
        arr = np.ndarray(
            shape=(lines, points),
            dtype=np.float32,
            buffer=self.data,
            offset=data_offset,
            strides=(stride, 4),
        ).copy()
        gami_header = ARDFHeader.unpack(
            ibox_header.data, ibox_header.offset + ibox_size
        )
        if gami_header.size != 16 or gami_header.name != b"GAMI":
            raise ValueError("Malformed image layout.", gami_header)
        gami_header.validate()
        return arr


@attrs.frozen
class ARDFFFMReader:
    data: mmap  # keep checking our mmap is open so array_view cannot segfault
    array_view: np.ndarray = attrs.field(repr=False)
    array_offset: int  # hard to recover from views
    channels: list[int]  # [z, d]
    # seg_offsets is weird. you'd think it would contain the starting index
    # for each segment. However, it always has a trailing value of 1-nfloats,
    # and nonexistent segments get a zero. For regular/FFM data, we'll just
    # assume that the second offset maps to our "split" concept.
    seg_offsets: tuple
    scandown: bool
    trace: bool

    @classmethod
    def parse(cls, first_vset_header: ARDFHeader, points: int, lines: int, channels):
        data = first_vset_header.data
        # just walk past these first headers to find our data_offset
        first_vset = ARDFVset.unpack(first_vset_header)
        vset_stride = first_vset.next_vset_offset - first_vset_header.offset

        first_vnam_header = ARDFHeader.unpack(
            data, first_vset_header.offset + first_vset_header.size
        )
        if first_vnam_header.name != b"VNAM":
            raise ValueError("Malformed volume name", first_vnam_header)
        first_vnam_header.validate()
        first_vdat_header = ARDFHeader.unpack(
            data, first_vnam_header.offset + first_vnam_header.size
        )
        if first_vdat_header.name != b"VDAT":
            raise ValueError("Malformed volume data")
        first_vdat = ARDFVdata.unpack(first_vdat_header)
        return cls(
            data=data,
            array_view=np.ndarray(
                shape=(lines, points, len(channels), first_vdat.nfloats),
                dtype=np.float32,
                buffer=data,
                offset=first_vdat.array_offset,
                strides=(
                    vset_stride * points,
                    vset_stride,
                    first_vdat_header.size,
                    4,
                ),
            ),
            array_offset=first_vdat.array_offset,
            channels=[channels["Raw"][0], channels["Defl"][0]],
            seg_offsets=first_vdat.seg_offsets,
            scandown=first_vdat.line != 0,
            trace=first_vdat.point == 0,
        )

    def get_curve(self, r, c):
        """Efficiently get a specific curve from disk."""
        assert not self.data.closed
        return (
            self.array_view[
                r,
                c,
                self.channels,
            ]
            * NANOMETER_UNIT_CONVERSION
        ).reshape((len(self.channels), 2, -1))

    def iter_curves(self) -> Iterable[tuple[Index, ZDArrays]]:
        """Iterate over curves lazily in on-disk order."""
        # For now assume our strides don't bounce around
        # TODO: cleverly use np.nditer?
        for point in np.ndindex(self.array_view.shape[:2]):
            yield point, self.get_curve(*point)

    def get_all_curves(self) -> ZDArrays:
        """Eagerly load all curves into memory."""
        assert not self.data.closed
        # advanced indexing triggers a copy
        loaded_data = self.array_view[:, :, self.channels, :]
        # avoid a second copy with inplace op
        loaded_data *= NANOMETER_UNIT_CONVERSION
        # reshape assuming equal points on extend and retract
        return loaded_data.reshape(self.array_view.shape[:-1] + (2, -1))


@attrs.frozen
class ARDFForceMapReader:
    data: mmap
    vtoc: ARDFVolumeTableOfContents
    lines: int
    points: int
    vtype: int
    channels: ChanMap
    _seen_vsets: dict[Index, ARDFVset] = attrs.field(init=False, factory=dict)

    @property
    def zname(self):
        return "Raw" if "Raw" in self.channels else "ZSnsr"

    def traverse_vsets(self, pointer: int):
        while True:
            header = ARDFHeader.unpack(self.data, pointer)
            if header.name != b"VSET":
                break
            vset = ARDFVset.unpack(header)
            index = (vset.line, vset.point, vset.vtype)
            if index not in self._seen_vsets:
                self._seen_vsets[index] = vset
            yield vset  # .line, vset.point, vset.
            pointer = vset.next_vset_offset

    def traverse_vdats(self, pointer):
        vnam_header = ARDFHeader.unpack(self.data, pointer)
        if vnam_header.name != b"VNAM":
            raise ValueError("Malformed volume name", vnam_header)
        vnam_header.validate()
        pointer = vnam_header.offset + vnam_header.size
        while True:
            vdat_header = ARDFHeader.unpack(self.data, pointer)
            if vdat_header.name != b"VDAT":
                break
            vdat_header.validate()  # opportunity to read data with gil released
            yield ARDFVdata.unpack(vdat_header)
            pointer = vdat_header.offset + vdat_header.size

    def get_curve(self, r: int, c: int) -> ZDArrays:
        """Efficiently get a specific curve from disk."""
        if not (0 <= r < self.lines and 0 <= c < self.points):
            raise ValueError("Invalid index:", (self.lines, self.points), (r, c))

        curve = r, c, self.vtype
        if curve not in self._seen_vsets:
            # bisect row pointer
            if self.vtoc.lines[0] > self.vtoc.lines[-1]:
                # probably reversed
                sl = np.s_[::-1]
            else:
                sl = np.s_[:]
            i = self.vtoc.lines[sl].searchsorted(r)
            if i >= len(self.vtoc.lines) or r != int(self.vtoc.lines[sl][i]):
                return np.full(shape=(2, 2, 100), fill_value=np.nan, dtype=np.float32)
            # read entire line of the vtoc
            for vset in self.traverse_vsets(int(self.vtoc.pointers[sl][i])):
                if vset.line != r:
                    break
                # traverse_vsets implicitly fills in seen_vsets

        vset = self._seen_vsets[curve]

        for vdat in self.traverse_vdats(vset.offset + vset.size):
            s = vdat.seg_offsets
            if vdat.channel == self.channels[self.zname][0]:
                z = vdat.get_ndarray()
                zxr = z[: s[1]], z[s[1] : s[2]]
            elif vdat.channel == self.channels["Defl"][0]:
                d = vdat.get_ndarray()
                dxr = d[: s[1]], d[s[1] : s[2]]
        return zxr, dxr

    def iter_curves(self) -> Iterable[tuple[Index, ZDArrays]]:
        """Iterate over curves lazily in on-disk order."""
        zname = self.zname
        for vset in self.traverse_vsets(int(self.vtoc.pointers[0])):
            if vset.vtype != self.vtype:
                continue
            for vdat in self.traverse_vdats(vset.offset + vset.size):
                s = vdat.seg_offsets
                if vdat.channel == self.channels[zname][0]:
                    z = vdat.get_ndarray()
                    zxr = z[: s[1]], z[s[1] : s[2]]
                elif vdat.channel == self.channels["Defl"][0]:
                    d = vdat.get_ndarray()
                    dxr = d[: s[1]], d[s[1] : s[2]]
            yield (vset.line, vset.point), (zxr, dxr)

    def get_all_curves(self) -> ZDArrays:
        """Eagerly load all curves into memory."""
        minext = 0xFFFFFFFF
        vdats = {}
        for vset in self.traverse_vsets(int(self.vtoc.pointers[0])):
            if vset.vtype != self.vtype:
                continue
            for vdat in self.traverse_vdats(vset.offset + vset.size):
                if vdat.channel == self.channels["ZSnsr"][0]:
                    zvdat = vdat
                elif vdat.channel == self.channels["Defl"][0]:
                    dvdat = vdat
            vdats[vset.line, vset.point] = zvdat, dvdat
            minext = min(minext, vdat.seg_offsets[1])
        del vset, vdat
        minfloats = 2 * minext
        x = np.empty((self.lines, self.points, 2, minfloats), dtype=np.float32)
        for (r, c), (zvdat, dvdat) in vdats.items():
            # code elsewhere assumes split is halfway through
            floats = zvdat.seg_offsets[1] * 2
            halfextra = (floats - minfloats) // 2  # even - even -> even
            sl = np.s_[halfextra : minfloats + halfextra]
            # TODO: verify turnaround point against iter_curves
            x[r, c, :, :] = zvdat.get_ndarray()[sl], dvdat.get_ndarray()[sl]
        return x.reshape(x.shape[:-1] + (2, -1))


@attrs.frozen
class ARDFVolume:
    volm_offset: int
    reader: FVReader
    shape: Index
    x_step: float
    y_step: float
    t_step: float
    x_units: str
    y_units: str
    t_units: str
    xdef: ARDFXdef

    @classmethod
    def parse_volm(cls, volm_header: ARDFHeader):
        data = volm_header.data
        if volm_header.size != 32 or volm_header.name != b"VOLM":
            raise ValueError("Malformed volume header.", volm_header)
        # the next headers look a lot like VSET is a table of contents, but I've only
        # seen NEXT and NSET headers. NEXT shows up if "both" trace and retrace data
        # are inside. I'll assume the last entry is always NSET, the number of VSETs.
        volm_toc = ARDFTableOfContents.unpack(volm_header)
        nset_header, nsets = volm_toc.entries[-1]
        nset_header.validate()
        ttoc_header = ARDFHeader.unpack(data, volm_toc.offset + volm_toc.size)
        ttoc = ARDFTextTableOfContents.unpack(ttoc_header)

        # don't use TTOC or TOFF, step over

        # cls essentially represents VDEF plus its linkage down to VSET
        # so this unpacking is intentionally inlined here.
        vdef_header = ARDFHeader.unpack(data, ttoc.offset + ttoc.size)
        vdef_format = "<LL24sddd32s32s32s32sQ"
        if (
            vdef_header.name != b"VDEF"
            or vdef_header.size != struct.calcsize(vdef_format) + 16
        ):
            raise ValueError(
                "Malformed volume definition.",
                vdef_header,
                struct.calcsize(vdef_format),
            )
        vdef_header.validate()
        points, lines, _, x_step, y_step, t_step, *cstrings, nseg = struct.unpack_from(
            vdef_format,
            vdef_header.data,
            vdef_header.offset + 16,
        )
        assert sum(_) == 0, _
        complete = points * lines == nsets
        x_units, y_units, t_units, seg_names = list(map(decode_cstring, cstrings))
        seg_names = seg_names.split(";")[:-1]
        assert nseg == len(seg_names)

        # Implicit table of channels here smh
        offset = vdef_header.offset + vdef_header.size
        channels: ChanMap = {}
        for i in range(5):
            header = ARDFHeader.unpack(data, offset)
            if header.name != b"VCHN":
                break
            vchn = ARDFVchan.unpack(header)
            offset = header.offset + header.size
            # TODO: apply NANOMETER_UNIT_CONVERSION depending on this
            assert vchn.unit == "m"
            channels[vchn.name] = (i, vchn)
        else:
            raise RuntimeError("Got too many channels.", channels)

        xdef = ARDFXdef.unpack(header)
        vtoc_header = ARDFHeader.unpack(data, xdef.offset + xdef.size)
        vtoc = ARDFVolumeTableOfContents.unpack(vtoc_header)

        mlov_header = ARDFHeader.unpack(data, vtoc.offset + vtoc.size)
        if mlov_header.size != 16 or mlov_header.name != b"MLOV":
            raise ValueError("Malformed volume table of contents.", mlov_header)
        mlov_header.validate()

        # Check if each offset is regularly spaced
        # optimize for LARGE regular case (FMaps are SMALL)
        first_vset_header = ARDFHeader.unpack(data, int(vtoc.pointers[0]))
        if (
            0
            and complete
            and not np.any(np.diff(np.diff(vtoc.pointers.astype(np.uint64))))
        ):
            assert False, "not yet reliable on scan up vs down"
            reader = ARDFFFMReader.parse(first_vset_header, points, lines, channels)
        else:
            first_vset = ARDFVset.unpack(first_vset_header)
            reader = ARDFForceMapReader(
                data,
                vtoc,
                lines,
                points,
                first_vset.vtype,
                channels,
            )

        return cls(
            volm_header.offset,
            reader,
            (points, lines),
            x_step,
            y_step,
            t_step,
            x_units,
            y_units,
            t_units,
            xdef,
        )


@attrs.define
class ARDFWorker:
    notes: dict[str, str] = attrs.field(repr=False)
    images: dict[str, ARDFImage]
    volumes: list[ARDFVolume]

    @classmethod
    def parse_ardf(cls, ardf_mmap: mmap):
        file_header = ARDFHeader.unpack(ardf_mmap, 0)
        if file_header.size != 16 or file_header.name != b"ARDF":
            raise ValueError("Not an ARDF file.", file_header)
        file_header.validate()
        ftoc_header = ARDFHeader.unpack(ardf_mmap, offset=file_header.size)
        if ftoc_header.name != b"FTOC":
            raise ValueError("Malformed ARDF file table of contents.", ftoc_header)
        ftoc = ARDFTableOfContents.unpack(ftoc_header)
        ttoc_header = ARDFHeader.unpack(ardf_mmap, offset=ftoc.offset + ftoc.size)
        ttoc = ARDFTextTableOfContents.unpack(ttoc_header)
        assert len(ttoc.entries) == 1
        notes = parse_AR_note(ttoc.decode_entry(0))
        images = {}
        volumes = []
        for item, pointer in ftoc.entries:
            item.validate()
            item = ARDFHeader.unpack(ardf_mmap, pointer)
            if item.name == b"IMAG":
                item = ARDFImage.parse_imag(item)
                images[item.name] = item
                # assert isinstance(item.get_ndarray(), np.ndarray)
            elif item.name == b"VOLM":
                volumes.append(ARDFVolume.parse_volm(item))
            else:
                raise RuntimeError(f"Unknown TOC entry {item.name}.", item)
        return cls(notes, images, volumes)


class ARDFFile(BaseForceVolumeFile):
    _basic_units_map = {
        "Adhesion": "N",
        "Height": "m",
        "IndentationHertz": "m",
        "YoungsHertz": "Pa",
        "YoungsJKR": "Pa",
        "YoungsDMT": "Pa",
        "ZSensor": "m",
        "MapAdhesion": "N",
        "MapHeight": "m",
        "Force": "N",
    }
    _default_heightmap_names = ("MapHeight", "ZSensorTrace", "ZSensorRetrace")
    _worker: ARDFWorker | None = None
    _mm = None

    def __getstate__(self):
        state = self.__dict__.copy()
        del state["_mm"]
        del state["_worker"]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        try:
            mm, worker, path_lock = CACHED_OPEN_PATHS[self.path]
        except KeyError:
            self._mm = mm = mmap_path_read_only(self.path)
            worker = ARDFWorker.parse_ardf(mm)
            path_lock = threading.Lock()
            path_lock.acquire()
            CACHED_OPEN_PATHS[self.path] = mm, worker, path_lock
            threading.Thread(
                target=eventually_evict_path, args=(self.path,), daemon=True
            ).start()
        else:
            # reset thread countdown
            try:
                path_lock.release()
            except RuntimeError:
                pass  # no problem if unreleased
        self._mm = mm
        self._worker = worker

    async def ainitialize(self):
        self._mm = await trio.to_thread.run_sync(mmap_path_read_only, self.path)
        self._worker = await trio.to_thread.run_sync(ARDFWorker.parse_ardf, self._mm)
        self._file_image_names.update(self._worker.images.keys())

        self.k = float(self._worker.notes["SpringConstant"])
        # slight numerical differences here
        # lines, points = self._worker.volumes[0].shape
        # xsize = lines * self._worker.volumes[0].x_step
        # ysize = points * self._worker.volumes[0].y_step
        self.scansize = (
            float(self._worker.notes["FastScanSize"]) * NANOMETER_UNIT_CONVERSION,
            float(self._worker.notes["SlowScanSize"]) * NANOMETER_UNIT_CONVERSION,
        )
        # NOTE: aspect is redundant to scansize
        # self.aspect = float(self.notes["SlowRatio"]) / float(self.notes["FastRatio"])
        self.defl_sens = self._defl_sens_orig = (
            float(self._worker.notes["InvOLS"]) * NANOMETER_UNIT_CONVERSION
        )
        self.t_step = self._worker.volumes[0].t_step

    async def aclose(self):
        if self._mm is not None:
            with trio.CancelScope(shield=True):
                await trio.to_thread.run_sync(self._mm.close)

    def get_force_curve_sync(self, r, c):
        out = zxr, dxr = self._worker.volumes[0].reader.get_curve(r, c)
        if self.defl_sens != self._defl_sens_orig:
            dxr *= self.defl_sens / self._defl_sens_orig
        return out

    def get_image_sync(self, image_name):
        return self._worker.images[image_name].get_ndarray()


###############################################
################### Bruker ####################
###############################################


class BrukerWorkerBase(metaclass=abc.ABCMeta):
    def __init__(self, header, mm, s):
        self.header = header  # get_image
        self.mm = mm  # get_image
        self.split = s  # get_force_curve
        self.version = header["Force file list"]["Version"].strip()  # get_image

    @abc.abstractmethod
    def get_force_curve(self, r, c, defl_sens, sync_dist):
        raise NotImplementedError

    def get_image(self, image_name):
        h = self.header["Image"][image_name]

        value = h["@2:Z scale"]
        bpp = bruker_bpp_fix(h["Bytes/pixel"], self.version)
        hard_scale = float(value.split()[-2]) / (2 ** (bpp * 8))
        hard_offset = float(h["@2:Z offset"].split()[-2]) / (2 ** (bpp * 8))
        soft_scale_name = "@" + value[1 + value.find("[") : value.find("]")]
        try:
            soft_scale_string = self.header["Ciao scan list"][soft_scale_name]
        except KeyError:
            soft_scale_string = self.header["Scanner list"][soft_scale_name]
        soft_scale = float(soft_scale_string.split()[1]) / NANOMETER_UNIT_CONVERSION
        scale = np.float32(hard_scale * soft_scale)
        data_length = int(h["Data length"])
        offset = int(h["Data offset"])
        r = int(h["Number of lines"])
        c = int(h["Samps/line"])
        assert data_length == r * c * bpp
        # scandown = h["Frame direction"] == "Down"
        z_ints = np.ndarray(
            shape=(r, c), dtype=f"i{bpp}", buffer=self.mm, offset=offset
        )
        z_floats = z_ints * scale + np.float32(hard_offset * soft_scale)
        return z_floats


class FFVWorker(BrukerWorkerBase):
    def __init__(self, header, mm, s):
        super().__init__(header, mm, s)
        arbitrary_image = next(iter(header["Image"].values()))
        r = int(arbitrary_image["Number of lines"])
        c = int(arbitrary_image["Samps/line"])
        data_name = header["Ciao force list"]["@4:Image Data"].split('"')[1]
        for name in [data_name, "Height Sensor"]:
            subheader = header["FV"][name]
            offset = int(subheader["Data offset"])
            bpp = bruker_bpp_fix(subheader["Bytes/pixel"], self.version)
            length = int(subheader["Data length"])
            npts = length // (r * c * bpp)
            data = np.ndarray(
                shape=(r, c, npts), dtype=f"i{bpp}", buffer=mm, offset=offset
            )
            if name == "Height Sensor":
                self.z_ints = data
                value = subheader["@4:Z scale"]
                soft_scale = float(
                    header["Ciao scan list"]["@Sens. ZsensSens"].split()[1]
                )
                hard_scale = float(
                    value[1 + value.find("(") : value.find(")")].split()[0]
                )
                self.z_scale = np.float32(soft_scale * hard_scale)
            else:
                self.d_ints = data
                value = subheader["@4:Z scale"]
                self.defl_hard_scale = float(
                    value[1 + value.find("(") : value.find(")")].split()[0]
                )

    def get_force_curve(self, r, c, defl_sens, sync_dist):
        s = self.split
        defl_scale = np.float32(defl_sens * self.defl_hard_scale)

        d = self.d_ints[r, c] * defl_scale
        d[:s] = d[s - 1 :: -1]

        if sync_dist:
            d = np.roll(d, -sync_dist)

        z = self.z_ints[r, c] * self.z_scale
        return (z[s - 1 :: -1], z[s:]), (d[:s], d[s:])


class QNMWorker(BrukerWorkerBase):
    def __init__(self, header, mm, s):
        super().__init__(header, mm, s)
        arbitrary_image = next(iter(header["Image"].values()))
        r = int(arbitrary_image["Number of lines"])
        c = int(arbitrary_image["Samps/line"])
        data_name = header["Ciao force list"]["@4:Image Data"].split('"')[1]
        subheader = header["FV"][data_name]
        bpp = bruker_bpp_fix(subheader["Bytes/pixel"], self.version)
        length = int(subheader["Data length"])
        offset = int(subheader["Data offset"])
        npts = length // (r * c * bpp)

        self.d_ints = np.ndarray(
            shape=(r, c, npts), dtype=f"i{bpp}", buffer=mm, offset=offset
        )
        value = subheader["@4:Z scale"]
        self.defl_hard_scale = float(
            value[1 + value.find("(") : value.find(")")].split()[0]
        )

        try:
            image = self.get_image("Height Sensor")
        except KeyError:
            image = self.get_image("Height")
        image *= NANOMETER_UNIT_CONVERSION
        self.height_for_z = image
        amp = np.float32(header["Ciao scan list"]["Peak Force Amplitude"])
        phase = s / npts * 2 * np.pi
        self.z_basis = amp * np.cos(
            np.linspace(
                phase, phase + 2 * np.pi, npts, endpoint=False, dtype=np.float32
            )
        )

    def get_force_curve(self, r, c, defl_sens, sync_dist):
        s = self.split
        defl_scale = np.float32(defl_sens * self.defl_hard_scale)

        d = self.d_ints[r, c] * defl_scale
        d[:s] = d[s - 1 :: -1]
        # remove blip
        if d[0] == -32768 * defl_scale:
            d[0] = d[1]
        d = np.roll(d, s - sync_dist)  # TODO roll across two adjacent indents

        # need to infer z from amp/height
        z = self.z_basis + self.height_for_z[r, c]

        return (z[:s], z[s:]), (d[:s], d[s:])


class NanoscopeFile(BaseForceVolumeFile):
    _basic_units_map = {
        "Height Sensor": "m",
        "Height": "m",
    }
    _default_heightmap_names = ("Height Sensor", "Height")

    def __getstate__(self):
        state = self.__dict__.copy()
        del state["_mm"]
        del state["_worker"]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        try:
            mm, worker, path_lock = CACHED_OPEN_PATHS[self.path]
        except KeyError:
            self._mm = mm = mmap_path_read_only(self.path)
            worker, _ = self._choose_worker(self.header)
            path_lock = threading.Lock()
            path_lock.acquire()
            CACHED_OPEN_PATHS[self.path] = mm, worker, path_lock
            threading.Thread(
                target=eventually_evict_path, args=(self.path,), daemon=True
            ).start()
        else:
            # reset thread countdown
            try:
                path_lock.release()
            except RuntimeError:
                pass  # no problem if unreleased
        self._mm = mm
        self._worker = worker

    async def ainitialize(self):
        self._mm = await trio.to_thread.run_sync(mmap_path_read_only, self.path)

        # End of header is demarcated by a SUB byte (26 = 0x1A)
        # Longest header so far was 80 kB, stop there to avoid searching gigabytes before fail
        header_end_pos = await trs(self._mm.find, b"\x1A", 0, 80960)
        if header_end_pos < 0:
            raise ValueError(
                "No stop byte found, are you sure this is a Nanoscope file?"
            )
        self.header = header = parse_nanoscope_header(
            self._mm[:header_end_pos]  # will be cached from find call
            .decode("windows-1252")
            .splitlines()
        )

        # Header items that I should be reading someday
        # \Frame direction: Down
        # \Line Direction: Retrace
        # \Z direction: Retract

        # and in the image lists
        # \Aspect Ratio: 1:1
        # \Scan Size: 800 800 nm

        self._file_image_names.update(header["Image"].keys())

        data_name = header["Ciao force list"]["@4:Image Data"].split('"')[1]

        data_header = header["FV"][data_name]
        self.split, *_ = map(int, data_header["Samps/line"].split())
        self.npts = int(header["Ciao force list"]["force/line"].split()[0])
        rate, unit = header["Ciao scan list"]["PFT Freq"].split()
        assert unit.lower() == "khz"
        self.rate = float(rate) * 1000
        self.t_step = 1 / self.rate / self.npts

        scansize, units = header["Ciao scan list"]["Scan Size"].split()
        if units == "nm":
            factor = 1.0
        elif units == "pm":
            factor = 0.001
        elif units == "~m":  # microns?!
            factor = 1000.0
        else:
            raise ValueError("unknown units:", units)

        # TODO: tuple(map(float,header[""Ciao scan list""]["Aspect Ratio"].split(":")))
        fastpx = int(header["Ciao scan list"]["Samps/line"])
        slowpx = int(header["Ciao scan list"]["Lines"])
        ratio = float(scansize) * factor / max(fastpx, slowpx)
        self.scansize = (fastpx * ratio, slowpx * ratio)

        # self.scandown = {"Down": True, "Up": False}[
        #     header["FV"]["Deflection Error"]["Frame direction"]
        # ]

        self.k = float(data_header["Spring Constant"])
        self.defl_sens = float(header["Ciao scan list"]["@Sens. DeflSens"].split()[1])
        value = data_header["@4:Z scale"]
        self.defl_hard_scale = float(
            value[1 + value.find("(") : value.find(")")].split()[0]
        )

        self._worker, self.sync_dist = await trs(self._choose_worker, header)

    def _choose_worker(self, header):
        if "Height Sensor" in header["FV"]:
            return FFVWorker(header, self._mm, self.split), 0
        else:
            return (
                QNMWorker(header, self._mm, self.split),
                int(
                    round(
                        float(
                            header["Ciao scan list"]["Sync Distance QNM"]
                            if "Sync Distance QNM" in header["Ciao scan list"]
                            else header["Ciao scan list"]["Sync Distance"]
                        )
                    )
                ),
            )

    async def aclose(self):
        self._worker = None
        with trio.CancelScope(shield=True):
            await trio.to_thread.run_sync(self._mm.close)

    def get_force_curve_sync(self, r, c):
        return self._worker.get_force_curve(r, c, self.defl_sens, self.sync_dist)

    def get_image_sync(self, image_name):
        return self._worker.get_image(image_name)


SUFFIX_FVFILE_MAP = {
    ".ardf": ARDFFile,
    ".h5": ARH5File,
    ".spm": NanoscopeFile,
    ".pfc": NanoscopeFile,
}
