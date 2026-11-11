import ctypes
import sys
import types
from typing import Any, List, Optional

import numpy as np
import pandas as pd


class _ArrowSchema(ctypes.Structure):
    pass


_RELEASE_SCHEMA_FN = ctypes.CFUNCTYPE(None, ctypes.POINTER(_ArrowSchema))

_ArrowSchema._fields_ = [
    ("format", ctypes.c_char_p),
    ("name", ctypes.c_char_p),
    ("metadata", ctypes.c_char_p),
    ("flags", ctypes.c_int64),
    ("n_children", ctypes.c_int64),
    ("children", ctypes.POINTER(ctypes.POINTER(_ArrowSchema))),
    ("dictionary", ctypes.POINTER(_ArrowSchema)),
    ("release", _RELEASE_SCHEMA_FN),
    ("private_data", ctypes.c_void_p),
]


class _ArrowArray(ctypes.Structure):
    pass


_RELEASE_ARRAY_FN = ctypes.CFUNCTYPE(None, ctypes.POINTER(_ArrowArray))

_ArrowArray._fields_ = [
    ("length", ctypes.c_int64),
    ("null_count", ctypes.c_int64),
    ("offset", ctypes.c_int64),
    ("n_buffers", ctypes.c_int64),
    ("n_children", ctypes.c_int64),
    ("buffers", ctypes.POINTER(ctypes.c_void_p)),
    ("children", ctypes.POINTER(ctypes.POINTER(_ArrowArray))),
    ("dictionary", ctypes.POINTER(_ArrowArray)),
    ("release", _RELEASE_ARRAY_FN),
    ("private_data", ctypes.c_void_p),
]


def _read_validity_mask(buf_ptr: Optional[int], length: int, offset: int, null_count: int) -> Optional[np.ndarray]:
    if not buf_ptr or null_count == 0 or length == 0:
        return None
    total_bits = offset + length
    n_bytes = (total_bits + 7) // 8
    raw = (ctypes.c_uint8 * n_bytes).from_address(buf_ptr)
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little")
    return bits[offset : offset + length].astype(bool)


def _import_arrow_from_c(array_ptr: int, schema_ptr: int) -> pd.Series:
    arr = _ArrowArray.from_address(array_ptr)
    schema = _ArrowSchema.from_address(schema_ptr)

    fmt = (schema.format or b"").decode("ascii", errors="ignore")
    length = int(arr.length)
    offset = int(arr.offset)
    null_count = int(arr.null_count)
    n_buffers = int(arr.n_buffers)

    try:
        if length == 0 or fmt == "n":
            return pd.Series([None] * length, dtype=object)

        buf0 = arr.buffers[0] if n_buffers > 0 else None
        valid_mask = _read_validity_mask(buf0, length, offset, null_count)

        # Numeric primitives: f (float32), g (float64), i (int32), I (uint32), l (int64), L (uint64)
        dtype_map = {
            "f": np.float32,
            "g": np.float64,
            "i": np.int32,
            "I": np.uint32,
            "l": np.int64,
            "L": np.uint64,
            "c": np.int8,
            "C": np.uint8,
            "s": np.int16,
            "S": np.uint16,
        }
        if fmt in dtype_map:
            np_dtype = dtype_map[fmt]
            buf1 = arr.buffers[1]
            elem_size = np.dtype(np_dtype).itemsize
            data_addr = buf1 + offset * elem_size
            c_arr = (ctypes.c_uint8 * (length * elem_size)).from_address(data_addr)
            vals = np.frombuffer(c_arr, dtype=np_dtype).copy()
            if valid_mask is not None:
                if fmt == "L":
                    out = [int(v) if m else None for v, m in zip(vals, valid_mask)]
                    return pd.Series(out, dtype=object)
                vals_f = vals.astype(np.float64)
                vals_f[~valid_mask] = np.nan
                return pd.Series(vals_f)
            return pd.Series(vals)

        # Boolean ("b")
        if fmt == "b":
            buf1 = arr.buffers[1]
            total_bits = offset + length
            n_bytes = (total_bits + 7) // 8
            raw = (ctypes.c_uint8 * n_bytes).from_address(buf1)
            bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little")[offset : offset + length].astype(bool)
            if valid_mask is not None:
                out = [bool(b) if m else None for b, m in zip(bits, valid_mask)]
                return pd.Series(out, dtype=object)
            return pd.Series(bits, dtype=bool)

        # Utf8 ("u") or LargeUtf8 ("U")
        if fmt in ("u", "U"):
            off_dtype = np.int32 if fmt == "u" else np.int64
            off_size = 4 if fmt == "u" else 8
            buf_offsets = arr.buffers[1] + offset * off_size
            c_offs = (ctypes.c_uint8 * ((length + 1) * off_size)).from_address(buf_offsets)
            offsets = np.frombuffer(c_offs, dtype=off_dtype)
            buf_data = arr.buffers[2]
            out_strs: List[Optional[str]] = []
            for idx in range(length):
                if valid_mask is not None and not valid_mask[idx]:
                    out_strs.append(None)
                    continue
                start_b = int(offsets[idx])
                end_b = int(offsets[idx + 1])
                slen = end_b - start_b
                if slen <= 0:
                    out_strs.append("")
                else:
                    raw_b = ctypes.string_at(buf_data + start_b, slen)
                    out_strs.append(raw_b.decode("utf-8", errors="replace"))
            return pd.Series(out_strs, dtype=object)

        # Utf8View / BinaryView ("vu" or "vz") used by modern Polars
        if fmt in ("vu", "vz"):
            buf_views = arr.buffers[1] + offset * 16
            out_strs = []
            for idx in range(length):
                if valid_mask is not None and not valid_mask[idx]:
                    out_strs.append(None)
                    continue
                view_addr = buf_views + idx * 16
                slen = ctypes.c_int32.from_address(view_addr).value
                if slen <= 0:
                    out_strs.append("")
                elif slen <= 12:
                    raw_b = ctypes.string_at(view_addr + 4, slen)
                    out_strs.append(raw_b.decode("utf-8", errors="replace"))
                else:
                    buf_idx = ctypes.c_int32.from_address(view_addr + 8).value
                    byte_offset = ctypes.c_int32.from_address(view_addr + 12).value
                    data_buf_ptr = arr.buffers[2 + buf_idx]
                    raw_b = ctypes.string_at(data_buf_ptr + byte_offset, slen)
                    out_strs.append(raw_b.decode("utf-8", errors="replace"))
            return pd.Series(out_strs, dtype=object)

        return pd.Series([None] * length, dtype=object)
    finally:
        if bool(arr.release):
            arr.release(ctypes.byref(arr))
        if bool(schema.release):
            schema.release(ctypes.byref(schema))


class _FakePolarsDataFrame:
    def __init__(self, series_list: List[pd.Series]) -> None:
        self._series_list = list(series_list)
        self.columns: List[str] = [f"col_{i}" for i in range(len(self._series_list))]

    def to_pandas(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        data = {}
        for col_name, ser in zip(self.columns, self._series_list):
            data[col_name] = ser
        return pd.DataFrame(data)


def install_arrow_polars_shim() -> None:
    """Install lightweight C-Arrow -> pandas bridge if pyarrow or polars are not installed."""
    try:
        import pyarrow  # noqa: F401
        import polars  # noqa: F401
        return
    except ImportError:
        pass

    pa_mod = types.ModuleType("pyarrow")

    class _ArrayNamespace:
        @staticmethod
        def _import_from_c(array_ptr: int, schema_ptr: int) -> pd.Series:
            return _import_arrow_from_c(array_ptr, schema_ptr)

    pa_mod.Array = _ArrayNamespace  # type: ignore[attr-defined]
    sys.modules["pyarrow"] = pa_mod

    pl_mod = types.ModuleType("polars")
    pl_mod.from_arrow = lambda s: s  # type: ignore[attr-defined]
    pl_mod.DataFrame = _FakePolarsDataFrame  # type: ignore[attr-defined]
    sys.modules["polars"] = pl_mod
